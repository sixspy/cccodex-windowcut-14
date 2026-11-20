"""审计与互证只读旁路。"""
from __future__ import annotations

from fastapi import APIRouter

from auth import AuthHeader, require_user
from db import connect

router = APIRouter(tags=["audit"])


@router.get("/api/audit")
def list_audit(authorization: AuthHeader = None, limit: int = 100):
    require_user(authorization)
    limit = max(1, min(limit, 500))
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_trail ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/audit/actions")
def audit_actions(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            """SELECT action, COUNT(*) c FROM audit_trail
               GROUP BY action ORDER BY c DESC"""
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/audit/consistency")
def consistency(authorization: AuthHeader = None):
    """五处同态互证：可补入队列 / 核销态 / 正式事件 / 审计 / 争用。

    返回 ok=False 时列出每一笔不一致，供关账拦截与排障。
    """
    require_user(authorization)
    with connect() as conn:
        mismatches: list[dict] = []
        late_rows = conn.execute("SELECT * FROM late_bucket ORDER BY id").fetchall()
        idx_rows = {
            r["late_id"]: r
            for r in conn.execute("SELECT * FROM late_promotable").fetchall()
        }
        for late in late_rows:
            lid = late["id"]
            idx = idx_rows.get(lid)
            if late["status"] == "可补入":
                if idx is None:
                    mismatches.append(
                        {"late_id": lid, "issue": "可补入态缺索引", "status": late["status"]}
                    )
                elif (
                    idx["biz_key"] != late["biz_key"]
                    or idx["event_ts"] != late["event_ts"]
                    or abs(idx["amount"] - late["amount"]) > 1e-9
                ):
                    mismatches.append(
                        {"late_id": lid, "issue": "索引与桶行不一致", "status": late["status"]}
                    )
            else:
                if idx is not None:
                    mismatches.append(
                        {
                            "late_id": lid,
                            "issue": f"{late['status']}仍留可补入索引",
                            "status": late["status"],
                        }
                    )
                if late["status"] == "已核销" and late["promoted_event_id"]:
                    mismatches.append(
                        {"late_id": lid, "issue": "已核销却有正式事件", "status": late["status"]}
                    )
                if late["status"] == "已补入":
                    n = conn.execute(
                        """SELECT COUNT(*) c FROM events
                           WHERE id=? AND disposition='窗内'""",
                        (late["promoted_event_id"],),
                    ).fetchone()["c"]
                    if n != 1:
                        mismatches.append(
                            {"late_id": lid, "issue": "已补入态无正式事件", "status": late["status"]}
                        )

        # 核销 / 补入必须有审计；争用行不得对应业务已写
        audited = {
            int(r["target"])
            for r in conn.execute(
                "SELECT target FROM audit_trail WHERE action IN ('late_writeoff','late_promote') AND target IS NOT NULL"
            ).fetchall()
            if str(r["target"]).isdigit()
        }
        for late in late_rows:
            if late["status"] in ("已核销", "已补入") and late["id"] not in audited:
                mismatches.append(
                    {"late_id": late["id"], "issue": "处置态缺审计", "status": late["status"]}
                )

        # 争用台账里的桶锁键必须能落到真实迟到行
        known_ids = {row["id"] for row in late_rows}
        for c_row in conn.execute("SELECT lock_key,actor FROM contention_log").fetchall():
            key = c_row["lock_key"]
            if key.startswith("late-bucket:"):
                try:
                    lid = int(key.split(":", 1)[1])
                except ValueError:
                    lid = None
                if lid not in known_ids:
                    mismatches.append(
                        {
                            "late_id": lid,
                            "issue": f"争用桶锁键无对应迟到行（{c_row['actor']}）",
                            "status": "-",
                        }
                    )

        return {"ok": not mismatches, "mismatches": mismatches, "checked": len(late_rows)}


@router.get("/api/dashboard")
def dashboard(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        open_n = conn.execute(
            "SELECT COUNT(*) c FROM windows WHERE status='开启'"
        ).fetchone()["c"]
        closed_n = conn.execute(
            "SELECT COUNT(*) c FROM windows WHERE status='已关闭'"
        ).fetchone()["c"]
        late_n = conn.execute("SELECT COUNT(*) c FROM late_bucket").fetchone()["c"]
        reject_n = conn.execute("SELECT COUNT(*) c FROM reject_slips").fetchone()["c"]
        hold_n = conn.execute(
            "SELECT COUNT(*) c FROM hold_staging WHERE status='暂存'"
        ).fetchone()["c"]
        mats = conn.execute("SELECT COUNT(*) c FROM materializations").fetchone()["c"]
        contention = conn.execute("SELECT COUNT(*) c FROM contention_log").fetchone()["c"]
        return {
            "open_windows": open_n,
            "closed_windows": closed_n,
            "late_bucket": late_n,
            "rejects": reject_n,
            "holds": hold_n,
            "materializations": mats,
            "contention": contention,
        }
