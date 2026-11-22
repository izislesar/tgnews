"""Хранение вердиктов и очередь задач с лизом (переживает рестарт процесса)."""

from __future__ import annotations

import json
import sqlite3
import threading
import time

from .models import ClaimVerdict, Claim, ClaimType, Evidence, PostVerdict, Priority, Status

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fc_jobs(
    qid INTEGER PRIMARY KEY,
    tier TEXT DEFAULT 't1',
    state TEXT DEFAULT 'queued',
    attempts INTEGER DEFAULT 0,
    locked_until REAL,
    next_try REAL DEFAULT 0,
    created REAL,
    updated REAL,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_fc_jobs_state ON fc_jobs(state, next_try);
CREATE TABLE IF NOT EXISTS claims(
    qid INTEGER, idx INTEGER, claim_key TEXT, text TEXT, ctype TEXT, priority TEXT,
    status TEXT, confidence REAL, note TEXT, cycles INTEGER, error TEXT,
    PRIMARY KEY(qid, idx)
);
CREATE TABLE IF NOT EXISTS evidence(
    claim_key TEXT, url TEXT, domain TEXT, publisher_group TEXT, title TEXT, quote TEXT,
    status TEXT, ts REAL,
    PRIMARY KEY(claim_key, url)
);
CREATE TABLE IF NOT EXISTS fc_runs(
    qid INTEGER, cycle INTEGER, stage TEXT, model TEXT, query TEXT,
    usage_json TEXT, ms REAL, ts REAL
);
"""

_POST_COLUMNS = {
    "fc_status": "TEXT",
    "fc_summary": "TEXT",
    "fc_version": "TEXT",
    "fc_ts": "REAL",
    "neutral_ok": "INTEGER",
}


class FactcheckRepo:
    def __init__(self, con: sqlite3.Connection, lock: threading.Lock | None = None):
        self.con = con
        self.lock = lock or threading.Lock()
        self.ensure_schema()

    def ensure_schema(self) -> None:
        with self.lock:
            self.con.executescript(_SCHEMA)
            columns = {row[1] for row in self.con.execute("PRAGMA table_info(posts)")}
            for name, sql_type in _POST_COLUMNS.items():
                if name not in columns:
                    self.con.execute(f"ALTER TABLE posts ADD COLUMN {name} {sql_type}")
            self.con.commit()

    # ---------- очередь ----------
    def enqueue(self, qid: int, tier: str = "t1") -> None:
        now = time.time()
        with self.lock:
            self.con.execute(
                "INSERT INTO fc_jobs(qid, tier, state, created, updated, next_try) "
                "VALUES(?,?, 'queued', ?, ?, 0) "
                "ON CONFLICT(qid) DO UPDATE SET state='queued', updated=excluded.updated, "
                "next_try=0 WHERE fc_jobs.state IN ('done','error','skipped')",
                (qid, tier, now, now),
            )
            self.con.commit()

    def claim_next(self, *, lease_s: float = 180.0) -> int | None:
        now = time.time()
        with self.lock:
            self.con.execute(
                "UPDATE fc_jobs SET state='queued' WHERE state='running' AND locked_until < ?",
                (now,),
            )
            row = self.con.execute(
                "SELECT qid FROM fc_jobs WHERE state IN ('queued','retry') AND next_try <= ? "
                "ORDER BY created LIMIT 1",
                (now,),
            ).fetchone()
            if not row:
                self.con.commit()
                return None
            qid = int(row[0])
            self.con.execute(
                "UPDATE fc_jobs SET state='running', attempts=attempts+1, locked_until=?, "
                "updated=? WHERE qid=?",
                (now + lease_s, now, qid),
            )
            self.con.commit()
            return qid

    def finish(self, qid: int, state: str = "done") -> None:
        with self.lock:
            self.con.execute(
                "UPDATE fc_jobs SET state=?, locked_until=NULL, updated=? WHERE qid=?",
                (state, time.time(), qid),
            )
            self.con.commit()

    def fail(self, qid: int, error: str, *, delay_s: float = 120.0) -> None:
        with self.lock:
            self.con.execute(
                "UPDATE fc_jobs SET state='retry', locked_until=NULL, next_try=?, "
                "updated=?, last_error=? WHERE qid=?",
                (time.time() + delay_s, time.time(), error[:300], qid),
            )
            self.con.commit()

    def post_text(self, qid: int) -> str | None:
        row = self.con.execute("SELECT text FROM posts WHERE qid=?", (qid,)).fetchone()
        return row[0] if row else None

    def post_ts(self, qid: int) -> float | None:
        row = self.con.execute("SELECT ts FROM posts WHERE qid=?", (qid,)).fetchone()
        return float(row[0]) if row and row[0] else None

    # ---------- вердикты ----------
    def save(self, qid: int, verdict: PostVerdict, *, version: str = "") -> None:
        now = time.time()
        summary = verdict.disclaimer or ""
        with self.lock:
            self.con.execute(
                "UPDATE posts SET fc_status=?, fc_summary=?, fc_version=?, fc_ts=? WHERE qid=?",
                (verdict.status.value, summary, version, now, qid),
            )
            self.con.execute("DELETE FROM claims WHERE qid=?", (qid,))
            for item in verdict.claims:
                self.con.execute(
                    "INSERT OR REPLACE INTO claims(qid, idx, claim_key, text, ctype, priority, "
                    "status, confidence, note, cycles, error) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (qid, item.claim.idx, item.claim.claim_key, item.claim.text,
                     item.claim.ctype.value, item.claim.priority.value, item.status.value,
                     item.confidence, item.note, item.cycles, item.error),
                )
                for evidence in item.evidence:
                    self.con.execute(
                        "INSERT OR REPLACE INTO evidence(claim_key, url, domain, "
                        "publisher_group, title, quote, status, ts) VALUES(?,?,?,?,?,?,?,?)",
                        (item.claim.claim_key, evidence.url, evidence.domain,
                         evidence.publisher_group, evidence.title, evidence.quote,
                         item.status.value, now),
                    )
            self.con.commit()

    def load(self, qid: int) -> PostVerdict | None:
        row = self.con.execute(
            "SELECT fc_status, fc_summary, fc_version FROM posts WHERE qid=?", (qid,)
        ).fetchone()
        if not row:
            return None
        claims_rows = self.con.execute(
            "SELECT idx, text, ctype, priority, status, confidence, note, cycles, error "
            "FROM claims WHERE qid=? ORDER BY idx",
            (qid,),
        ).fetchall()
        claims: list[ClaimVerdict] = []
        for idx, text, ctype, priority, status, confidence, note, cycles, error in claims_rows:
            claim = Claim(
                idx=idx, text=text,
                ctype=ClaimType(ctype or "event"),
                priority=Priority(priority or "secondary"),
            )
            evidence_rows = self.con.execute(
                "SELECT url, domain, publisher_group, title, quote FROM evidence WHERE claim_key=?",
                (claim.claim_key,),
            ).fetchall()
            evidence = [
                Evidence(url=u, domain=d, publisher_group=g, title=t, quote=q)
                for u, d, g, t, q in evidence_rows
            ]
            claims.append(
                ClaimVerdict(
                    claim=claim,
                    status=Status(status or "not_found"),
                    confidence=confidence or 0.0,
                    note=note or "",
                    cycles=cycles or 0,
                    error=error,
                    evidence=evidence,
                )
            )
        return PostVerdict(
            status=Status(row[0] or "skipped"),
            claims=claims,
            qid=qid,
            note=row[1] or "",
        )

    def log_run(self, qid: int, *, cycle: int, stage: str, model: str = "",
                query: str = "", usage: dict | None = None, ms: float = 0.0) -> None:
        with self.lock:
            self.con.execute(
                "INSERT INTO fc_runs(qid, cycle, stage, model, query, usage_json, ms, ts) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (qid, cycle, stage, model, query, json.dumps(usage or {}, ensure_ascii=False),
                 ms, time.time()),
            )
            self.con.commit()

    def stats(self, days: int = 7) -> list[tuple[str, int]]:
        cutoff = time.time() - days * 86400
        return self.con.execute(
            "SELECT fc_status, COUNT(*) FROM posts WHERE fc_ts > ? GROUP BY fc_status "
            "ORDER BY COUNT(*) DESC",
            (cutoff,),
        ).fetchall()

    def pending(self) -> int:
        row = self.con.execute(
            "SELECT COUNT(*) FROM fc_jobs WHERE state IN ('queued','retry','running')"
        ).fetchone()
        return int(row[0]) if row else 0
