"""核销门禁册的可执行守卫。

冻结口径见 docs/核销门禁册.md：
- 只走甲路径（状态机冻结核销），乙路径（void_ticket 票根）不得落地；
- 核销 / 补入写路径先过本守卫，任何偏离拒写并登记 gate_deviation（含「门禁偏了」）。
"""
from __future__ import annotations

from fastapi import HTTPException

from db import GATE_BOOK_FROZEN, write_audit

FROZEN_PATH = "甲"
DEV_MARK = "门禁偏了"


def read_book(conn) -> dict:
    row = conn.execute("SELECT * FROM writeoff_gate_book WHERE id=1").fetchone()
    if row is None:
        # 冻结册缺失本身即偏离，不许静默补写
        raise HTTPException(409, f"{DEV_MARK}：核销门禁册缺失，拒绝写路径")
    return dict(row)


def _record_deviation(conn, actor: str, op: str, detail: str) -> None:
    write_audit(
        conn,
        actor,
        "gate_deviation",
        op,
        {"mark": DEV_MARK, "detail": detail, "frozen": GATE_BOOK_FROZEN},
    )


def assert_frozen(conn, actor: str, op: str) -> dict:
    """写路径总门禁：路径必须为甲且已锁定，否则拒写并记门禁偏了。"""
    book = read_book(conn)
    if (
        book["path"] != FROZEN_PATH
        or not book["locked"]
        or book["frozen_on"] != GATE_BOOK_FROZEN["frozen_on"]
    ):
        detail = (
            f"当前路径={book['path']} 锁定={book['locked']} "
            f"冻结日={book['frozen_on']}；冻结要求 path=甲 且 locked=1"
        )
        _record_deviation(conn, actor, op, detail)
        conn.commit()
        raise HTTPException(409, f"{DEV_MARK}：{detail}，拒绝写路径")
    return book
