"""SQLite state: every proposal ever made and what was decided about it.

This is what makes re-runs safe. A proposal's fingerprint identifies "this exact change to
this exact record". Once a fingerprint has been approved or rejected it is never put back
in the queue, no matter how many times the pipeline runs.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import config

PENDING, APPLIED, REJECTED, FAILED, STALE = "pending", "applied", "rejected", "failed", "stale"
DECIDED = (APPLIED, REJECTED)

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    locations INTEGER, accounts INTEGER,
    new_proposals INTEGER, still_pending INTEGER, skipped_decided INTEGER, went_stale INTEGER,
    message TEXT, report_json TEXT
);
CREATE TABLE IF NOT EXISTS proposals (
    fingerprint TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    payload_json TEXT NOT NULL,
    first_run_id INTEGER, last_run_id INTEGER,
    created_at TEXT NOT NULL,
    decided_at TEXT, decision_note TEXT, result_json TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


class Store:
    def __init__(self, path: Path = None):
        self.path = Path(path or config.DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # --- runs ------------------------------------------------------------------
    def start_run(self) -> int:
        cur = self.db.execute("INSERT INTO runs (started_at) VALUES (?)", (now(),))
        self.db.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, status: str, message: str = "", report: dict = None, **counts):
        self.db.execute(
            "UPDATE runs SET finished_at=?, status=?, message=?, report_json=?, locations=?, accounts=?, "
            "new_proposals=?, still_pending=?, skipped_decided=?, went_stale=? WHERE id=?",
            (now(), status, message, json.dumps(report) if report is not None else None,
             counts.get("locations"), counts.get("accounts"), counts.get("new_proposals"),
             counts.get("still_pending"), counts.get("skipped_decided"), counts.get("went_stale"), run_id))
        self.db.commit()

    def last_ok_run(self):
        return self.db.execute("SELECT * FROM runs WHERE status='ok' ORDER BY id DESC LIMIT 1").fetchone()

    def runs(self, limit: int = 15) -> list:
        return self.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    # --- proposals -----------------------------------------------------------------
    def sync_proposals(self, run_id: int, proposals: list) -> dict:
        """Merge this run's findings into the queue without resurrecting decided items."""
        counts = {"new_proposals": 0, "still_pending": 0, "skipped_decided": 0, "went_stale": 0}
        seen = set()
        for p in proposals:
            seen.add(p["fingerprint"])
            row = self.db.execute("SELECT status FROM proposals WHERE fingerprint=?", (p["fingerprint"],)).fetchone()
            if row is None:
                self.db.execute(
                    "INSERT INTO proposals (fingerprint, kind, title, status, payload_json, first_run_id, "
                    "last_run_id, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (p["fingerprint"], p["kind"], p["title"], PENDING, json.dumps(p), run_id, run_id, now()))
                counts["new_proposals"] += 1
            elif row["status"] in DECIDED:
                counts["skipped_decided"] += 1          # already decided: never re-proposed
            else:
                # pending / failed / stale-but-true-again: refresh the evidence, keep it in the queue
                new_status = PENDING if row["status"] == STALE else row["status"]
                self.db.execute("UPDATE proposals SET payload_json=?, title=?, last_run_id=?, status=? WHERE fingerprint=?",
                                (json.dumps(p), p["title"], run_id, new_status, p["fingerprint"]))
                counts["still_pending"] += 1
        # Anything still open that this run no longer produces is out of date.
        for row in self.db.execute("SELECT fingerprint FROM proposals WHERE status IN (?,?)", (PENDING, FAILED)).fetchall():
            if row["fingerprint"] not in seen:
                self.db.execute("UPDATE proposals SET status=?, decided_at=?, decision_note=? WHERE fingerprint=?",
                                (STALE, now(), "No longer produced by the pipeline (the website or CRM changed).",
                                 row["fingerprint"]))
                counts["went_stale"] += 1
        self.db.commit()
        return counts

    def get(self, fingerprint: str):
        row = self.db.execute("SELECT * FROM proposals WHERE fingerprint=?", (fingerprint,)).fetchone()
        return self._inflate(row) if row else None

    def list(self, statuses: tuple) -> list:
        marks = ",".join("?" * len(statuses))
        rows = self.db.execute("SELECT * FROM proposals WHERE status IN ({}) ORDER BY decided_at DESC, created_at".format(marks),
                               statuses).fetchall()
        return [self._inflate(r) for r in rows]

    def counts(self) -> dict:
        return {r["status"]: r["n"] for r in
                self.db.execute("SELECT status, COUNT(*) AS n FROM proposals GROUP BY status").fetchall()}

    def decide(self, fingerprint: str, status: str, note: str = "", result: dict = None):
        self.db.execute("UPDATE proposals SET status=?, decided_at=?, decision_note=?, result_json=? WHERE fingerprint=?",
                        (status, now(), note, json.dumps(result) if result is not None else None, fingerprint))
        self.db.commit()

    @staticmethod
    def _inflate(row) -> dict:
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        item["result"] = json.loads(item["result_json"]) if item.get("result_json") else None
        return item
