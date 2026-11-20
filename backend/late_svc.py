"""迟到桶处置：核销（甲路径）与补入，共用同一桶锁与核销门禁。

冻结口径见 docs/核销门禁册.md：
- 核销：状态切「已核销」与清可补入索引同事务，任一失败同回滚；
- 补入：先验核销态 + 索引，再写正式事件并切「已补入」、清索引，同事务；
- 核销与补入对同一 late_id 共用锁键 late-bucket:{late_id}，后到只记争用。
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from auth import AuthHeader, require_user
from db import connect, write_audit
from gate_book import assert_frozen
from locks import release, require_lock

router = APIRouter(tags=["late"])

PROMOTABLE = "可补入"
WRITTEN_OFF = "已核销"
PROMOTED = "已补入"


class LatePromoteIn(BaseModel):
    late_id: int
    target_window_name: str


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _add_promotable(conn, late_id: int, biz_key: str, event_ts: str, amount: float) -> None:
    conn.execute(
        """INSERT INTO late_promotable(late_id,biz_key,event_ts,amount,created_at)
           VALUES (?,?,?,?,?)""",
        (late_id, biz_key, event_ts, amount, _now()),
    )


def _drop_promotable(conn, late_id: int) -> None:
    conn.execute("DELETE FROM late_promotable WHERE late_id=?", (late_id,))


def _lock_key(late_id: int) -> str:
    return f"late-bucket:{late_id}"


@router.get("/api/late-bucket")
def list_late(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM late_bucket ORDER BY id DESC LIMIT 200"
        ).fetchall()
        return {"items": [dict(r) for r in rows], "count": len(rows)}


@router.get("/api/late-bucket/promotable")
def list_promotable(authorization: AuthHeader = None):
    """可补入队列：只信 late_promotable，且必须仍为可补入态。"""
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            """SELECT p.* FROM late_promotable p
               JOIN late_bucket b ON b.id=p.late_id
               WHERE b.status='可补入'
               ORDER BY p.late_id DESC"""
        ).fetchall()
        return {"items": [dict(r) for r in rows], "count": len(rows)}


@router.get("/api/late-bucket/summary")
def late_summary(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(amount),0) s FROM late_bucket"
        ).fetchone()
        by_window = conn.execute(
            """SELECT window_name, COUNT(*) c, COALESCE(SUM(amount),0) s
               FROM late_bucket GROUP BY window_name ORDER BY window_name"""
        ).fetchall()
        by_status = conn.execute(
            """SELECT status, COUNT(*) c, COALESCE(SUM(amount),0) s
               FROM late_bucket GROUP BY status ORDER BY status"""
        ).fetchall()
        return {
            "count": row["c"],
            "amount": row["s"],
            "by_window": [dict(r) for r in by_window],
            "by_status": [dict(r) for r in by_status],
        }


@router.post("/api/late-bucket/{late_id}/writeoff")
def writeoff(late_id: int, authorization: AuthHeader = None):
    """甲路径核销：同事务切核销态 + 清可补入索引，失败同回滚。"""
    user = require_user(authorization)
    lock_key = _lock_key(late_id)
    with connect() as conn:
        require_lock(conn, lock_key, user)
        try:
            book = assert_frozen(conn, user, "late_writeoff")
            late = conn.execute(
                "SELECT * FROM late_bucket WHERE id=?", (late_id,)
            ).fetchone()
            if not late:
                raise HTTPException(404, "迟到记录不存在")
            if late["status"] == WRITTEN_OFF:
                conn.commit()
                return {"ok": True, "already": True, "status": WRITTEN_OFF}
            if late["status"] != PROMOTABLE:
                raise HTTPException(409, f"当前状态 {late['status']} 不可核销")

            now = _now()
            conn.execute(
                """UPDATE late_bucket
                   SET status=?, written_off_at=?, written_off_by=?
                   WHERE id=? AND status=?""",
                (WRITTEN_OFF, now, user, late_id, PROMOTABLE),
            )
            _drop_promotable(conn, late_id)
            write_audit(
                conn,
                user,
                "late_writeoff",
                str(late_id),
                {
                    "biz_key": late["biz_key"],
                    "amount": late["amount"],
                    "path": book["path"],
                    "gate_frozen_on": book["frozen_on"],
                },
            )
            conn.commit()
            return {"ok": True, "status": WRITTEN_OFF, "path": book["path"]}
        except HTTPException:
            raise
        except Exception as exc:
            conn.rollback()
            raise HTTPException(500, f"核销失败已回滚：{exc}") from exc
        finally:
            release(conn, lock_key, user)
            conn.commit()


@router.post("/api/late-bucket/promote")
def promote(body: LatePromoteIn, authorization: AuthHeader = None):
    """补入：核销态为前置不变量，验索引通过才写正式事件；整笔回滚。"""
    user = require_user(authorization)
    late_id = body.late_id
    lock_key = _lock_key(late_id)
    with connect() as conn:
        require_lock(conn, lock_key, user)
        try:
            book = assert_frozen(conn, user, "late_promote")
            late = conn.execute(
                "SELECT * FROM late_bucket WHERE id=?", (late_id,)
            ).fetchone()
            if not late:
                raise HTTPException(404, "迟到记录不存在")
            w = conn.execute(
                "SELECT * FROM windows WHERE name=?", (body.target_window_name,)
            ).fetchone()
            if not w:
                raise HTTPException(404, "目标窗口不存在")

            idx = conn.execute(
                "SELECT * FROM late_promotable WHERE late_id=?", (late_id,)
            ).fetchone()
            reason = None
            if late["status"] != PROMOTABLE:
                reason = f"桶状态={late['status']}，非可补入"
            elif idx is None:
                reason = "可补入索引缺失"
            elif (
                idx["biz_key"] != late["biz_key"]
                or idx["event_ts"] != late["event_ts"]
                or abs(idx["amount"] - late["amount"]) > 1e-9
            ):
                reason = "可补入索引与桶行不一致"
            if reason:
                write_audit(
                    conn,
                    user,
                    "late_promote_refused",
                    str(late_id),
                    {"reason": reason, "path": book["path"]},
                )
                conn.commit()
                raise HTTPException(409, f"整笔拒补：{reason}")

            now = _now()
            cur = conn.execute(
                """INSERT INTO events(window_id,biz_key,event_ts,amount,disposition,created_at)
                   VALUES (?,?,?,?,?,?)""",
                (w["id"], late["biz_key"], late["event_ts"], late["amount"], "窗内", now),
            )
            event_id = cur.lastrowid
            conn.execute(
                """UPDATE late_bucket
                   SET status=?, promoted_event_id=?, promoted_at=?
                   WHERE id=? AND status=?""",
                (PROMOTED, event_id, now, late_id, PROMOTABLE),
            )
            _drop_promotable(conn, late_id)
            write_audit(
                conn,
                user,
                "late_promote",
                str(late_id),
                {
                    "event_id": event_id,
                    "window_id": w["id"],
                    "amount": late["amount"],
                    "path": book["path"],
                },
            )
            conn.commit()
            return {
                "ok": True,
                "status": PROMOTED,
                "event_id": event_id,
                "window_id": w["id"],
            }
        except HTTPException:
            raise
        except Exception as exc:
            conn.rollback()
            raise HTTPException(500, f"补入失败已回滚：{exc}") from exc
        finally:
            release(conn, lock_key, user)
            conn.commit()


@router.post("/api/late-bucket/{late_id}/discard-stub")
def discard_stub(late_id: int, authorization: AuthHeader = None):
    """空桩：作废迟到项（不删物理行，只审计）。"""
    user = require_user(authorization)
    with connect() as conn:
        late = conn.execute(
            "SELECT * FROM late_bucket WHERE id=?", (late_id,)
        ).fetchone()
        if not late:
            raise HTTPException(404, "迟到记录不存在")
        write_audit(
            conn,
            user,
            "late_discard_stub",
            str(late_id),
            {"biz_key": late["biz_key"], "at": _now()},
        )
        conn.commit()
    return {"ok": True, "stub": True}
