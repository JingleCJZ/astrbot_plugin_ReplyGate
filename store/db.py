"""SQLite 流水：写入、查询、统计、清理。

写入用后台线程（asyncio.to_thread），避免 DB 落在事件循环上；
查询也走线程，面板刷新不会卡住聊天。
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS events(
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  ts           REAL    NOT NULL,
  kind         TEXT    NOT NULL,
  umo          TEXT,
  gid          TEXT,
  sender_id    TEXT,
  sender_name  TEXT,
  reason       TEXT,
  act          TEXT,
  confidence   INTEGER,
  wait_seconds INTEGER,
  followup     TEXT,
  ms           INTEGER,
  model        TEXT,
  result       TEXT,
  detail       TEXT,
  user_input   TEXT,
  raw_output   TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts  ON events(ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_umo ON events(umo, ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind, ts DESC);
"""

FIELDS = ("ts", "kind", "umo", "gid", "sender_id", "sender_name", "reason", "act",
          "confidence", "wait_seconds", "followup", "ms", "model", "result", "detail",
          "user_input", "raw_output")

# 老库补列（SQLite 的 ALTER TABLE ADD COLUMN 只能追加，正好够用）
MIGRATIONS = (
    ("user_input", "TEXT"),
    ("raw_output", "TEXT"),
)

FINDABLE = {"kind", "umo", "gid", "sender_id", "reason", "act", "result"}


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._ready = False

    # ---- 基础 ----

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @contextlib.contextmanager
    def _conn(self):
        """事务 + 真正关闭。sqlite3 的 `with conn` 只提交，不关文件句柄。"""
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def init_sync(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._conn() as conn:
            conn.executescript(SCHEMA)
            existing = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
            for column, ctype in MIGRATIONS:
                if column not in existing:
                    conn.execute(f"ALTER TABLE events ADD COLUMN {column} {ctype}")
        self._ready = True

    async def init(self) -> None:
        await asyncio.to_thread(self.init_sync)

    @property
    def ready(self) -> bool:
        return self._ready and self.path.exists()

    # ---- 写入 ----

    def insert_sync(self, rows: Iterable[dict[str, Any]]) -> int:
        payload = [tuple(row.get(field) for field in FIELDS) for row in rows]
        if not payload:
            return 0
        with self._lock, self._conn() as conn:
            conn.executemany(
                f"INSERT INTO events({','.join(FIELDS)}) VALUES({','.join('?' * len(FIELDS))})",
                payload,
            )
        return len(payload)

    def insert_one_sync(self, row: dict[str, Any]) -> int:
        """插入一行并返回自增主键（HOLD 那条“等待中”需要后续改状态）。"""
        payload = tuple(row.get(field) for field in FIELDS)
        with self._lock, self._conn() as conn:
            cursor = conn.execute(
                f"INSERT INTO events({','.join(FIELDS)}) VALUES({','.join('?' * len(FIELDS))})",
                payload)
            return int(cursor.lastrowid or 0)

    async def insert_one(self, row: dict[str, Any]) -> int:
        if not self._ready:
            return 0
        try:
            return await asyncio.to_thread(self.insert_one_sync, row)
        except Exception:
            return 0

    # 就地改状态时允许一并补齐的列
    _UPDATABLE = ("detail", "reason", "confidence", "raw_output")

    def update_status_sync(self, row_id: int, result: str,
                           **fields: Any) -> bool:
        """就地改一条流水的状态（等待中的 HOLD / 判定中的判定，解决后不再另插一条）。"""
        args: list[Any] = [result]
        sql = "UPDATE events SET result = ?"
        for key in self._UPDATABLE:
            if key in fields and fields[key] is not None:
                sql += f", {key} = ?"
                args.append(fields[key])
        sql += " WHERE id = ?"
        args.append(int(row_id))
        with self._lock, self._conn() as conn:
            return conn.execute(sql, args).rowcount > 0

    async def update_status(self, row_id: int, result: str,
                            **fields: Any) -> bool:
        if not self._ready or not row_id:
            return False
        try:
            return await asyncio.to_thread(self.update_status_sync, row_id, result,
                                           **fields)
        except Exception:
            return False

    async def insert(self, rows: Iterable[dict[str, Any]]) -> int:
        if not self._ready:
            return 0
        payload = list(rows)
        if not payload:
            return 0
        try:
            return await asyncio.to_thread(self.insert_sync, payload)
        except Exception:
            return 0

    # ---- 查询 ----

    def query_sync(self, *, page: int = 1, size: int = 50,
                   filters: dict[str, str] | None = None,
                   since: float | None = None, until: float | None = None,
                   keyword: str = "") -> tuple[list[dict[str, Any]], int]:
        where, args = [], []
        for key, value in (filters or {}).items():
            if key not in FINDABLE or not value:
                continue
            where.append(f"{key} = ?")
            args.append(value)
        if since is not None:
            where.append("ts >= ?")
            args.append(float(since))
        if until is not None:
            where.append("ts <= ?")
            args.append(float(until))
        if keyword:
            where.append("(detail LIKE ? OR followup LIKE ? OR sender_name LIKE ? "
                         "OR user_input LIKE ? OR raw_output LIKE ?)")
            like = f"%{keyword}%"
            args.extend([like, like, like, like, like])
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        page = max(1, int(page))
        size = min(max(1, int(size)), 500)
        with self._lock, self._conn() as conn:
            conn.row_factory = sqlite3.Row
            total = conn.execute(f"SELECT COUNT(*) AS c FROM events{clause}", args).fetchone()["c"]
            rows = conn.execute(
                f"SELECT * FROM events{clause} ORDER BY ts DESC LIMIT ? OFFSET ?",
                args + [size, (page - 1) * size],
            ).fetchall()
        return [dict(row) for row in rows], int(total)

    async def query(self, **kwargs) -> tuple[list[dict[str, Any]], int]:
        if not self._ready:
            return [], 0
        return await asyncio.to_thread(self.query_sync, **kwargs)

    def detail_sync(self, row_id: int) -> dict[str, Any] | None:
        with self._lock, self._conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM events WHERE id = ?", (int(row_id),)).fetchone()
        return dict(row) if row else None

    async def detail(self, row_id: int) -> dict[str, Any] | None:
        if not self._ready:
            return None
        return await asyncio.to_thread(self.detail_sync, row_id)

    def stats_sync(self, since: float) -> dict[str, Any]:
        with self._lock, self._conn() as conn:
            conn.row_factory = sqlite3.Row
            def count(sql: str, args: tuple = ()) -> int:
                row = conn.execute(sql, args).fetchone()
                return int(row[0]) if row else 0

            total = count("SELECT COUNT(*) FROM events WHERE ts >= ?", (since,))
            by_kind = {row["kind"]: row["c"] for row in conn.execute(
                "SELECT kind, COUNT(*) AS c FROM events WHERE ts >= ? GROUP BY kind", (since,))}
            by_reason = {row["reason"]: row["c"] for row in conn.execute(
                "SELECT reason, COUNT(*) AS c FROM events WHERE ts >= ? AND reason != '' "
                "GROUP BY reason ORDER BY c DESC", (since,))}
            by_act = {row["act"]: row["c"] for row in conn.execute(
                "SELECT act, COUNT(*) AS c FROM events WHERE ts >= ? AND act != '' "
                "GROUP BY act", (since,))}
            by_result = {row["result"]: row["c"] for row in conn.execute(
                "SELECT result, COUNT(*) AS c FROM events WHERE ts >= ? AND result != '' "
                "GROUP BY result", (since,))}
            avg_conf = conn.execute(
                "SELECT AVG(confidence) FROM events WHERE ts >= ? AND confidence IS NOT NULL",
                (since,)).fetchone()[0]
            avg_ms = conn.execute(
                "SELECT AVG(ms) FROM events WHERE ts >= ? AND ms IS NOT NULL",
                (since,)).fetchone()[0]
        return {
            "total": total,
            "by_kind": by_kind,
            "by_reason": by_reason,
            "by_act": by_act,
            "by_result": by_result,
            "avg_confidence": round(float(avg_conf), 1) if avg_conf else None,
            "avg_ms": round(float(avg_ms), 0) if avg_ms else None,
        }

    async def stats(self, days: int = 1) -> dict[str, Any]:
        if not self._ready:
            return {"total": 0, "by_kind": {}, "by_reason": {}, "by_act": {},
                    "by_result": {}, "avg_confidence": None, "avg_ms": None}
        since = time.time() - max(1, int(days)) * 86400
        return await asyncio.to_thread(self.stats_sync, since)

    # ---- 运维 ----

    def clear_sync(self) -> int:
        with self._lock, self._conn() as conn:
            removed = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            conn.execute("DELETE FROM events")
            try:
                conn.execute("DELETE FROM sqlite_sequence WHERE name = 'events'")
            except sqlite3.Error:
                pass
        return int(removed)

    async def clear(self) -> int:
        if not self._ready:
            return 0
        return await asyncio.to_thread(self.clear_sync)

    def prune_sync(self, days: int, max_rows: int) -> int:
        removed = 0
        cutoff = time.time() - max(1, int(days)) * 86400
        with self._lock, self._conn() as conn:
            removed += conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,)).rowcount
            if max_rows > 0:
                extra = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] - int(max_rows)
                if extra > 0:
                    removed += conn.execute(
                        "DELETE FROM events WHERE id IN "
                        "(SELECT id FROM events ORDER BY ts ASC LIMIT ?)", (extra,)).rowcount
        return int(max(0, removed))

    async def prune(self, days: int, max_rows: int) -> int:
        if not self._ready:
            return 0
        return await asyncio.to_thread(self.prune_sync, days, max_rows)

    def row_count_sync(self) -> int:
        with self._lock, self._conn() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    async def row_count(self) -> int:
        if not self._ready:
            return 0
        return await asyncio.to_thread(self.row_count_sync)

    def size_bytes_sync(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0


def day_start(ts: float | None = None) -> float:
    now = datetime.fromtimestamp(ts if ts is not None else time.time())
    return datetime(now.year, now.month, now.day).timestamp()