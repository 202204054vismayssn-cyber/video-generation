"""Small persistent job store. SQLite coordinates the API and isolated render processes."""

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class JobStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        (self.root / "jobs").mkdir(parents=True, exist_ok=True)
        self.database = self.root / "jobs.sqlite3"
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, status TEXT NOT NULL, stage TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL,
                error TEXT, result TEXT, completed_stages INTEGER NOT NULL DEFAULT 0
            )""")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.database, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, job_id):
        now = time.time()
        with self.connection() as db:
            db.execute(
                "INSERT INTO jobs(id,status,stage,created,updated) VALUES(?,?,?,?,?)",
                (job_id, "queued", "queued", now, now),
            )

    def get(self, job_id):
        with self.connection() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["job_id"] = data.pop("id")
        data["result"] = json.loads(data["result"]) if data["result"] else None
        return data

    def recent(self):
        with self.connection() as db:
            ids = [
                row[0]
                for row in db.execute(
                    "SELECT id FROM jobs ORDER BY created DESC LIMIT 50"
                )
            ]
        return [self.get(job_id) for job_id in ids]

    def pending(self):
        with self.connection() as db:
            return db.execute(
                "SELECT count(*) FROM jobs WHERE status IN ('queued','running')"
            ).fetchone()[0]

    def claim(self):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT id FROM jobs WHERE status='queued' ORDER BY created LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE jobs SET status='running',stage='starting',updated=? WHERE id=?",
                (time.time(), row[0]),
            )
            return row[0]

    def update(
        self,
        job_id,
        *,
        status=None,
        stage=None,
        error=None,
        result=None,
        completed_stages=None,
    ):
        values = {"updated": time.time()}
        for key, value in dict(
            status=status, stage=stage, error=error, completed_stages=completed_stages
        ).items():
            if value is not None:
                values[key] = value
        if result is not None:
            values["result"] = json.dumps(result)
        columns = ",".join(f"{key}=?" for key in values)
        with self.connection() as db:
            db.execute(
                f"UPDATE jobs SET {columns} WHERE id=?", [*values.values(), job_id]
            )

    def recover(self):
        with self.connection() as db:
            db.execute(
                "UPDATE jobs SET status='failed',stage='interrupted',error=?,updated=? WHERE status='running'",
                (
                    "The API/runtime restarted during rendering. Review the log and submit the request again.",
                    time.time(),
                ),
            )


def save_json(path, data):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    temporary.replace(path)
