"""核销门禁红测：先复现「已核销仍可补入回流」与「核销/补入对撞」两类失败。

本文件在 late_svc / ingest_svc / audit_svc 写路径落地前先见红；
写路径只允许在本文件两条主红测齐备后改动。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import app, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "gate.db")
    init_db()
    yield


def _auth(c: TestClient, username: str = "admin", password: str = "123456") -> dict:
    tok = c.post(
        "/api/login", json={"username": username, "password": password}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _close(c: TestClient, h: dict, wid: int) -> None:
    r = c.post(f"/api/windows/{wid}/close", headers=h)
    assert r.status_code == 200


def _make_late(
    c: TestClient, h: dict, biz_key: str, amount: float = 4.0
) -> int:
    """关 W-1102 后入一笔迟到，返回 late_bucket.id。"""
    _close(c, h, 2)
    r = c.post(
        "/api/events",
        headers=h,
        json={
            "window_name": "W-1102",
            "biz_key": biz_key,
            "event_ts": "2026-11-02T22:00:00",
            "amount": amount,
        },
    )
    assert r.json()["disposition"] == "迟到桶"
    items = c.get("/api/late-bucket", headers=h).json()["items"]
    return next(x["id"] for x in items if x["biz_key"] == biz_key)


def _late_row(c: TestClient, h: dict, late_id: int) -> dict:
    items = c.get("/api/late-bucket", headers=h).json()["items"]
    return next(x for x in items if x["id"] == late_id)


def _promotable_ids(c: TestClient, h: dict) -> set[int]:
    body = c.get("/api/late-bucket/promotable", headers=h).json()
    return {x["late_id"] for x in body["items"]}


def _formal_count(c: TestClient, h: dict, wid: int) -> int:
    items = c.get("/api/events", headers=h).json()["items"]
    return sum(1 for e in items if e.get("window_id") == wid and e["disposition"] == "窗内")


def _audits(c: TestClient, h: dict, action: str) -> list[dict]:
    items = c.get("/api/audit?limit=200", headers=h).json()["items"]
    return [a for a in items if a["action"] == action]


# ---------------------------------------------------------------------------
# 主红测一：核销后仍能补入 → 已核销金额回流正式窗
# ---------------------------------------------------------------------------
def test_writeoff_then_promote_must_not_backflow():
    c = TestClient(app)
    h = _auth(c)
    late_id = _make_late(c, h, "BACKFLOW-1", 7.5)
    before = _formal_count(c, h, 2)

    # 夹具：先核销
    wo = c.post(f"/api/late-bucket/{late_id}/writeoff", headers=h)
    assert wo.status_code == 200, wo.text

    # 核销态、可补入队列、正式事件、审计四处必须同态
    assert _late_row(c, h, late_id)["status"] == "已核销"
    assert late_id not in _promotable_ids(c, h), "已核销笔仍出现在可补入队列"

    # 补入必须整笔拒补，正式窗零增
    r = c.post(
        "/api/late-bucket/promote",
        headers=h,
        json={"late_id": late_id, "target_window_name": "W-1102"},
    )
    assert r.status_code == 409, r.text
    assert _formal_count(c, h, 2) == before, "已核销金额回流正式窗"
    assert _late_row(c, h, late_id)["status"] == "已核销"

    assert _audits(c, h, "late_writeoff"), "核销缺审计"
    refused = _audits(c, h, "late_promote_refused")
    assert any(a["target"] == str(late_id) for a in refused), "拒补缺审计"


# ---------------------------------------------------------------------------
# 复现夹具：模拟库中已存在「核销态 + 未清索引」的脏状态
# 无论索引是否残留，队列不得露出、补入不得放行
# ---------------------------------------------------------------------------
def test_seeded_stale_writeoff_index_never_reaches_queue_or_formal():
    c = TestClient(app)
    h = _auth(c)
    late_id = _make_late(c, h, "STALE-1", 8.0)
    before = _formal_count(c, h, 2)

    import db

    with db.connect() as conn:
        # 夹具：直接落成线上事故形态——状态已核销，可补入索引仍在
        conn.execute(
            "UPDATE late_bucket SET status='已核销', written_off_at='2026-11-20T10:00:00', written_off_by='admin' WHERE id=?",
            (late_id,),
        )
        assert conn.execute(
            "SELECT COUNT(*) c FROM late_promotable WHERE late_id=?", (late_id,)
        ).fetchone()["c"] == 1
        conn.commit()

    assert late_id not in _promotable_ids(c, h), "脏索引仍把已核销笔露在可补入队列"

    r = c.post(
        "/api/late-bucket/promote",
        headers=h,
        json={"late_id": late_id, "target_window_name": "W-1102"},
    )
    assert r.status_code == 409, r.text
    assert _formal_count(c, h, 2) == before, "脏索引放行了已核销金额回流"

    body = c.get("/api/audit/consistency", headers=h).json()
    assert body["ok"] is False
    assert any(m["late_id"] == late_id for m in body["mismatches"])


# ---------------------------------------------------------------------------
# 主红测二：核销与补入对同一 late_id 对撞须占同一桶锁，后到只记争用
# ---------------------------------------------------------------------------
def test_writeoff_promote_collision_same_late_id():
    c = TestClient(app)
    h1 = _auth(c)
    h2 = _auth(c, "closer", "close123")
    late_id = _make_late(c, h1, "COLLIDE-1", 5.0)
    before = _formal_count(c, h1, 2)
    lock_key = f"late-bucket:{late_id}"

    # 夹具：核销先到并持有桶锁（模拟核销事务进行中）
    assert (
        c.post("/api/locks/acquire", headers=h1, json={"lock_key": lock_key}).status_code
        == 200
    )

    # 后到的补入只能记争用，业务零写
    r = c.post(
        "/api/late-bucket/promote",
        headers=h2,
        json={"late_id": late_id, "target_window_name": "W-1102"},
    )
    assert r.status_code == 409, r.text
    assert "争用" in r.json()["detail"]
    assert _formal_count(c, h1, 2) == before, "争用后仍写正式事件"
    assert _late_row(c, h1, late_id)["status"] == "可补入"
    assert late_id in _promotable_ids(c, h1)

    cont = c.get("/api/contention", headers=h1).json()["items"]
    assert any(
        x["lock_key"] == lock_key and x["actor"] == "closer" for x in cont
    ), "后到争用未落争用台账"


# ---------------------------------------------------------------------------
# 原子性：核销清索引失败时状态与索引同回滚
# ---------------------------------------------------------------------------
def test_writeoff_rolls_back_status_when_index_delete_fails(monkeypatch):
    c = TestClient(app, raise_server_exceptions=False)
    h = _auth(c)
    late_id = _make_late(c, h, "ROLLBACK-1", 2.0)

    import late_svc

    def _boom(*a, **k):
        raise RuntimeError("模拟索引删除失败")

    monkeypatch.setattr(late_svc, "_drop_promotable", _boom)
    r = c.post(f"/api/late-bucket/{late_id}/writeoff", headers=h)
    assert r.status_code == 500, r.text
    assert _late_row(c, h, late_id)["status"] == "可补入", "状态未随索引回滚"
    assert late_id in _promotable_ids(c, h), "索引未随状态保留"


def test_promote_rolls_back_formal_event_when_index_delete_fails(monkeypatch):
    c = TestClient(app, raise_server_exceptions=False)
    h = _auth(c)
    late_id = _make_late(c, h, "PROMOTE-ROLLBACK", 3.0)
    before = _formal_count(c, h, 2)

    import late_svc

    def _boom(*a, **k):
        raise RuntimeError("模拟索引删除失败")

    monkeypatch.setattr(late_svc, "_drop_promotable", _boom)
    r = c.post(
        "/api/late-bucket/promote",
        headers=h,
        json={"late_id": late_id, "target_window_name": "W-1102"},
    )
    assert r.status_code == 500, r.text
    assert _formal_count(c, h, 2) == before, "补入失败仍留下正式事件"
    assert _late_row(c, h, late_id)["status"] == "可补入"
    assert late_id in _promotable_ids(c, h)


# ---------------------------------------------------------------------------
# 门禁册：偏离冻结路径拒写并记「门禁偏了」
# ---------------------------------------------------------------------------
def test_gate_book_deviation_refuses_write_and_audits():
    c = TestClient(app)
    h = _auth(c)
    late_id = _make_late(c, h, "DEVIATE-1", 1.0)

    import db

    with db.connect() as conn:
        conn.execute("UPDATE writeoff_gate_book SET path='乙'")
        conn.commit()

    r = c.post(f"/api/late-bucket/{late_id}/writeoff", headers=h)
    assert r.status_code == 409, r.text
    assert "门禁偏了" in r.json()["detail"]
    assert _late_row(c, h, late_id)["status"] == "可补入"
    assert late_id in _promotable_ids(c, h)

    dev = _audits(c, h, "gate_deviation")
    assert any("门禁偏了" in (a.get("payload") or "") for a in dev), "偏离未记门禁偏了"


# ---------------------------------------------------------------------------
# 五处同态
# ---------------------------------------------------------------------------
def test_five_way_consistency_after_writeoff_and_promote():
    c = TestClient(app)
    h = _auth(c)
    written = _make_late(c, h, "W-OFF", 4.0)
    promoted = _make_late(c, h, "P-ROMOTE", 6.0)

    assert c.post(f"/api/late-bucket/{written}/writeoff", headers=h).status_code == 200
    assert (
        c.post(
            "/api/late-bucket/promote",
            headers=h,
            json={"late_id": promoted, "target_window_name": "W-1102"},
        ).status_code
        == 200
    )

    body = c.get("/api/audit/consistency", headers=h).json()
    assert body["ok"] is True, body
    assert body["mismatches"] == []
    assert _late_row(c, h, promoted)["status"] == "已补入"
    assert promoted not in _promotable_ids(c, h)


# ---------------------------------------------------------------------------
# ingest 迟到入桶与可补入索引同事务：索引失败整笔回滚
# ---------------------------------------------------------------------------
def test_ingest_late_index_failure_rolls_back_bucket(monkeypatch):
    c = TestClient(app, raise_server_exceptions=False)
    h = _auth(c)
    _close(c, h, 2)

    import ingest_svc

    def _boom(*a, **k):
        raise RuntimeError("模拟可补入索引写入失败")

    monkeypatch.setattr(ingest_svc, "_add_promotable", _boom)
    r = c.post(
        "/api/events",
        headers=h,
        json={
            "window_name": "W-1102",
            "biz_key": "INDEX-FAIL",
            "event_ts": "2026-11-02T22:30:00",
            "amount": 9.9,
        },
    )
    assert r.status_code == 500, r.text
    items = c.get("/api/late-bucket", headers=h).json()["items"]
    assert not any(x["biz_key"] == "INDEX-FAIL" for x in items), "索引失败仍留下迟到桶行"
    events = c.get("/api/events", headers=h).json()["items"]
    assert not any(x["biz_key"] == "INDEX-FAIL" for x in events)
