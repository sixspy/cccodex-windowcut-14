"""SQLite 连接、建表、种子。业务窗关账母仓公共库层。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FIXTURES = ROOT / "fixtures"
FRONTEND = ROOT / "frontend"

PRODUCT_META = {
    "modules": [
        "windows",
        "ingest",
        "late_bucket",
        "late_writeoff_gate",
        "materialize",
        "reconcile",
        "locks",
        "audit",
        "hold_staging",
        "reopen",
    ],
    "stubs": ["hold_release", "partition_mat", "reopen"],
}


def connect() -> sqlite3.Connection:
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def sha_password(password: str) -> str:
    import hashlib

    return hashlib.sha256(password.encode("utf-8")).hexdigest()


SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  username TEXT PRIMARY KEY,
  password_sha TEXT NOT NULL,
  role TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS windows(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  open_at TEXT NOT NULL,
  close_at TEXT NOT NULL,
  status TEXT NOT NULL,
  generation INTEGER NOT NULL DEFAULT 1,
  seal_token TEXT
);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  window_id INTEGER,
  biz_key TEXT NOT NULL,
  event_ts TEXT NOT NULL,
  amount REAL NOT NULL,
  disposition TEXT NOT NULL,
  hold_id INTEGER,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS late_bucket(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id INTEGER NOT NULL,
  window_name TEXT,
  biz_key TEXT NOT NULL,
  event_ts TEXT NOT NULL,
  amount REAL NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '可补入',
  written_off_at TEXT,
  written_off_by TEXT,
  promoted_event_id INTEGER,
  promoted_at TEXT
);
CREATE TABLE IF NOT EXISTS late_promotable(
  late_id INTEGER PRIMARY KEY,
  biz_key TEXT NOT NULL,
  event_ts TEXT NOT NULL,
  amount REAL NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(late_id) REFERENCES late_bucket(id)
);
CREATE TABLE IF NOT EXISTS writeoff_gate_book(
  id INTEGER PRIMARY KEY,
  path TEXT NOT NULL,
  frozen_on TEXT NOT NULL,
  locked INTEGER NOT NULL DEFAULT 1,
  note TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS materializations(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  window_id INTEGER NOT NULL,
  status TEXT NOT NULL,
  row_count INTEGER NOT NULL,
  total_amount REAL NOT NULL,
  partition_key TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hold_staging(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  window_id INTEGER,
  biz_key TEXT NOT NULL,
  event_ts TEXT NOT NULL,
  amount REAL NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS window_locks(
  lock_key TEXT PRIMARY KEY,
  holder TEXT NOT NULL,
  acquired_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contention_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lock_key TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  detail TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_trail(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  target TEXT,
  payload TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reopen_ledger(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  window_id INTEGER NOT NULL,
  from_generation INTEGER NOT NULL,
  to_generation INTEGER NOT NULL,
  mode TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reject_slips(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  window_name TEXT,
  biz_key TEXT NOT NULL,
  event_ts TEXT NOT NULL,
  amount REAL NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


def ensure_users(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        conn.execute(
            "INSERT INTO users VALUES (?,?,?)",
            ("admin", sha_password("123456"), "管理员"),
        )
        conn.execute(
            "INSERT INTO users VALUES (?,?,?)",
            ("viewer", sha_password("view123"), "只读"),
        )
        conn.execute(
            "INSERT INTO users VALUES (?,?,?)",
            ("closer", sha_password("close123"), "关账员"),
        )


def _load_fixture(name: str) -> list | dict | None:
    path = FIXTURES / name
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def seed_if_empty(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT COUNT(*) c FROM windows").fetchone()["c"] > 0:
        return
    windows = _load_fixture("windows.json") or [
        {
            "name": "W-1101",
            "open_at": "2026-11-01T00:00:00",
            "close_at": "2026-11-01T23:59:59",
            "status": "已关闭",
            "generation": 1,
        },
        {
            "name": "W-1102",
            "open_at": "2026-11-02T00:00:00",
            "close_at": "2026-11-02T23:59:59",
            "status": "开启",
            "generation": 1,
        },
        {
            "name": "W-1103",
            "open_at": "2026-11-03T00:00:00",
            "close_at": "2026-11-03T23:59:59",
            "status": "开启",
            "generation": 1,
        },
    ]
    for w in windows:
        conn.execute(
            "INSERT INTO windows(name,open_at,close_at,status,generation) VALUES (?,?,?,?,?)",
            (
                w["name"],
                w["open_at"],
                w["close_at"],
                w["status"],
                int(w.get("generation", 1)),
            ),
        )
    events = _load_fixture("events.json") or [
        {
            "window_id": 1,
            "biz_key": "ORD-1",
            "event_ts": "2026-11-01T10:00:00",
            "amount": 12.5,
            "disposition": "窗内",
            "created_at": "2026-11-01T10:00:01",
        },
        {
            "window_id": None,
            "biz_key": "ORD-late",
            "event_ts": "2026-11-01T23:50:00",
            "amount": 3.0,
            "disposition": "迟到桶",
            "created_at": "2026-11-02T00:01:00",
        },
    ]
    for e in events:
        conn.execute(
            "INSERT INTO events(window_id,biz_key,event_ts,amount,disposition,created_at) VALUES (?,?,?,?,?,?)",
            (
                e.get("window_id"),
                e["biz_key"],
                e["event_ts"],
                e["amount"],
                e["disposition"],
                e.get("created_at", e["event_ts"]),
            ),
        )
        if e["disposition"] == "迟到桶":
            conn.execute(
                """INSERT INTO late_bucket(event_id,window_name,biz_key,event_ts,amount,reason,created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    conn.execute("SELECT last_insert_rowid()").fetchone()[0],
                    "W-1101",
                    e["biz_key"],
                    e["event_ts"],
                    e["amount"],
                    "关窗后到达",
                    e.get("created_at", e["event_ts"]),
                ),
            )
    conn.execute(
        "INSERT INTO materializations(window_id,status,row_count,total_amount,partition_key,created_at) VALUES (?,?,?,?,?,?)",
        (1, "成功", 1, 12.5, "W-1101-g1", "2026-11-02T00:10:00"),
    )
    conn.execute(
        "INSERT INTO audit_trail(actor,action,target,payload,created_at) VALUES (?,?,?,?,?)",
        ("system", "seed", "windows", json.dumps({"count": len(windows)}), "2026-11-01T00:00:00"),
    )


GATE_BOOK_FROZEN = {
    "path": "甲",
    "frozen_on": "2026-11-20",
    "note": "状态机冻结核销：核销同事务清索引，补入以核销态为前置不变量；禁止票根路径乙，两侧不得混用",
}


def ensure_gate_book(conn: sqlite3.Connection) -> None:
    """写入冻结口径；已存在则不改口（当天及之后均只读）。"""
    row = conn.execute("SELECT * FROM writeoff_gate_book WHERE id=1").fetchone()
    if row is None:
        conn.execute(
            """INSERT INTO writeoff_gate_book(id,path,frozen_on,locked,note,created_at)
               VALUES (1,?,?,1,?,?)""",
            (
                GATE_BOOK_FROZEN["path"],
                GATE_BOOK_FROZEN["frozen_on"],
                GATE_BOOK_FROZEN["note"],
                datetime.now().isoformat(timespec="seconds"),
            ),
        )


def migrate_late_bucket(conn: sqlite3.Connection) -> None:
    """旧库补列 + 可补入索引回填；脏态（已核销仍有索引）就地清理。"""
    cols = {
        r["name"]
        for r in conn.execute("PRAGMA table_info(late_bucket)").fetchall()
    }
    for name, ddl in (
        ("status", "ALTER TABLE late_bucket ADD COLUMN status TEXT NOT NULL DEFAULT '可补入'"),
        ("written_off_at", "ALTER TABLE late_bucket ADD COLUMN written_off_at TEXT"),
        ("written_off_by", "ALTER TABLE late_bucket ADD COLUMN written_off_by TEXT"),
        ("promoted_event_id", "ALTER TABLE late_bucket ADD COLUMN promoted_event_id INTEGER"),
        ("promoted_at", "ALTER TABLE late_bucket ADD COLUMN promoted_at TEXT"),
    ):
        if name not in cols:
            conn.execute(ddl)
    conn.execute(
        """INSERT OR IGNORE INTO late_promotable(late_id,biz_key,event_ts,amount,created_at)
           SELECT id,biz_key,event_ts,amount,created_at
           FROM late_bucket WHERE status='可补入'"""
    )
    conn.execute(
        """DELETE FROM late_promotable
           WHERE late_id IN (SELECT id FROM late_bucket WHERE status!='可补入')"""
    )


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        ensure_users(conn)
        ensure_gate_book(conn)
        seed_if_empty(conn)
        # 迁移/回填放在种子之后：种子迟到行也要进可补入索引
        migrate_late_bucket(conn)
        conn.commit()


def write_audit(
    conn: sqlite3.Connection,
    actor: str,
    action: str,
    target: str | None = None,
    payload: dict | None = None,
    created_at: str | None = None,
) -> None:
    from datetime import datetime

    now = created_at or datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO audit_trail(actor,action,target,payload,created_at) VALUES (?,?,?,?,?)",
        (actor, action, target, json.dumps(payload or {}, ensure_ascii=False), now),
    )
