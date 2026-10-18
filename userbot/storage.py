"""Постоянное состояние бота в SQLite.

Хранит:
  clones    — соответствие «исходный чат → чат-клон»;
  forwarded — какие сообщения уже пересланы (защита от дублей и точка
              отсчёта для догрузки пропущенного);
  pending   — очередь сообщений на пересылку, переживает перезапуск;
  failed    — сообщения, которые так и не удалось переслать.
"""

import sqlite3
import time
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS clones (
    source_id  INTEGER PRIMARY KEY,
    clone_id   INTEGER NOT NULL UNIQUE,
    title      TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS forwarded (
    source_id    INTEGER NOT NULL,
    msg_id       INTEGER NOT NULL,
    clone_msg_id INTEGER,
    done_at      REAL NOT NULL,
    PRIMARY KEY (source_id, msg_id)
);
CREATE INDEX IF NOT EXISTS forwarded_done ON forwarded (source_id, done_at);
CREATE TABLE IF NOT EXISTS pending (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    msg_id    INTEGER NOT NULL,
    added_at  REAL NOT NULL,
    attempts  INTEGER NOT NULL DEFAULT 0,
    UNIQUE (source_id, msg_id)
);
CREATE TABLE IF NOT EXISTS failed (
    source_id INTEGER NOT NULL,
    msg_id    INTEGER NOT NULL,
    error     TEXT,
    failed_at REAL NOT NULL,
    PRIMARY KEY (source_id, msg_id)
);
"""


class Storage:
    def __init__(self, path):
        # autocommit: каждая запись сразу на диске, падение процесса ничего не теряет
        self.db = sqlite3.connect(str(path), isolation_level=None, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    # --- клоны -------------------------------------------------------------

    def get_clone(self, source_id: int) -> Optional[int]:
        row = self.db.execute("SELECT clone_id FROM clones WHERE source_id=?", (source_id,)).fetchone()
        return row[0] if row else None

    def set_clone(self, source_id: int, clone_id: int, title: str):
        self.db.execute(
            "INSERT OR REPLACE INTO clones (source_id, clone_id, title, created_at) VALUES (?,?,?,?)",
            (source_id, clone_id, title, time.time()),
        )

    def delete_clone(self, source_id: int):
        self.db.execute("DELETE FROM clones WHERE source_id=?", (source_id,))

    def clone_ids(self) -> set:
        return {r[0] for r in self.db.execute("SELECT clone_id FROM clones")}

    def sources(self) -> list:
        return [r[0] for r in self.db.execute("SELECT source_id FROM clones")]

    # --- пересланные -------------------------------------------------------

    def is_forwarded(self, source_id: int, msg_id: int) -> bool:
        return self.db.execute(
            "SELECT 1 FROM forwarded WHERE source_id=? AND msg_id=?", (source_id, msg_id)
        ).fetchone() is not None

    def mark_forwarded(self, source_id: int, msg_id: int, clone_msg_id: Optional[int]):
        with self.db:
            self.db.execute("BEGIN")
            self.db.execute(
                "INSERT OR REPLACE INTO forwarded (source_id, msg_id, clone_msg_id, done_at) VALUES (?,?,?,?)",
                (source_id, msg_id, clone_msg_id, time.time()),
            )
            self.db.execute("DELETE FROM pending WHERE source_id=? AND msg_id=?", (source_id, msg_id))

    def last_forwarded_id(self, source_id: int, before_ts: float) -> Optional[int]:
        """Последнее сообщение чата, обработанное до момента before_ts."""
        row = self.db.execute(
            "SELECT MAX(msg_id) FROM forwarded WHERE source_id=? AND done_at<?", (source_id, before_ts)
        ).fetchone()
        return row[0] if row and row[0] is not None else None

    def prune(self, keep_days: int):
        """Удаляет старые записи, но последнюю по каждому чату оставляет —
        она нужна как точка отсчёта для догрузки."""
        cutoff = time.time() - keep_days * 86400
        self.db.execute(
            """DELETE FROM forwarded WHERE done_at<? AND (source_id, msg_id) NOT IN
               (SELECT source_id, MAX(msg_id) FROM forwarded GROUP BY source_id)""",
            (cutoff,),
        )
        self.db.execute("DELETE FROM failed WHERE failed_at<?", (cutoff,))

    # --- очередь -----------------------------------------------------------

    def add_pending(self, source_id: int, msg_id: int) -> bool:
        """Ставит сообщение в очередь. False, если оно уже переслано или в очереди."""
        if self.is_forwarded(source_id, msg_id):
            return False
        cur = self.db.execute(
            "INSERT OR IGNORE INTO pending (source_id, msg_id, added_at) VALUES (?,?,?)",
            (source_id, msg_id, time.time()),
        )
        return cur.rowcount > 0

    def next_pending(self):
        """(id, source_id, msg_id, attempts) самого старого элемента или None."""
        return self.db.execute(
            "SELECT id, source_id, msg_id, attempts FROM pending ORDER BY id LIMIT 1"
        ).fetchone()

    def pending_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM pending").fetchone()[0]

    def bump_attempts(self, pending_id: int):
        self.db.execute("UPDATE pending SET attempts=attempts+1 WHERE id=?", (pending_id,))

    def drop_pending(self, pending_id: int):
        self.db.execute("DELETE FROM pending WHERE id=?", (pending_id,))

    def mark_failed(self, pending_id: int, source_id: int, msg_id: int, error: str):
        with self.db:
            self.db.execute("BEGIN")
            self.db.execute(
                "INSERT OR REPLACE INTO failed (source_id, msg_id, error, failed_at) VALUES (?,?,?,?)",
                (source_id, msg_id, error[:500], time.time()),
            )
            self.db.execute("DELETE FROM pending WHERE id=?", (pending_id,))
