import sqlite3
import threading
import time

STALE_H = 12  # пост старше N часов в очереди — дроп без обработки


class Store:
    def __init__(self, path: str):
        self.lock = threading.Lock()
        self.con = sqlite3.connect(path, check_same_thread=False)
        c = self.con
        c.execute(
            """CREATE TABLE IF NOT EXISTS queue(
                qid INTEGER PRIMARY KEY AUTOINCREMENT,
                src INTEGER, ids TEXT, key TEXT UNIQUE, ts REAL, text TEXT,
                state TEXT DEFAULT 'pending')"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS seen(
                qid INTEGER PRIMARY KEY, tokens TEXT, ts REAL)"""
        )
        c.execute("CREATE TABLE IF NOT EXISTS last_seen(ch TEXT PRIMARY KEY, mid INTEGER)")
        c.commit()

    def enqueue(self, src, ids, key, text):
        with self.lock:
            self.con.execute(
                "INSERT OR IGNORE INTO queue(src, ids, key, ts, text) VALUES(?,?,?,?,?)",
                (src, ids, key, time.time(), text),
            )
            self.con.commit()

    def purge_not_in(self, allow_bare):
        with self.lock:
            rows = self.con.execute("SELECT DISTINCT src FROM queue WHERE state='pending'").fetchall()
            bad = [r[0] for r in rows if r[0] not in allow_bare]
            for src in bad:
                self.con.execute("DELETE FROM queue WHERE src=? AND state='pending'", (src,))
            self.con.commit()
            if bad:
                print(f"[purge] выкинуты pending с чужих src: {bad}")

    def exists(self, src, key) -> bool:
        cur = self.con.execute(
            "SELECT 1 FROM queue WHERE src=? AND key=? LIMIT 1", (src, key)
        )
        return cur.fetchone() is not None

    def pending(self):
        cur = self.con.execute(
            "SELECT src, ids, key, text, qid, ts FROM queue WHERE state='pending' "
            "ORDER BY ts DESC LIMIT 50"
        )
        return cur.fetchall()

    def mark(self, qid, state):
        with self.lock:
            self.con.execute("UPDATE queue SET state=? WHERE qid=?", (state, qid))
            self.con.commit()

    def remember(self, toks):
        with self.lock:
            qid = self.con.execute("SELECT MAX(qid) FROM queue").fetchone()[0] or 0
            self.con.execute(
                "INSERT OR REPLACE INTO seen(qid, tokens, ts) VALUES(?,?,?)",
                (qid, " ".join(sorted(toks)), time.time()),
            )
            self.con.commit()

    def recent_sets(self, hours=6):
        since = time.time() - hours * 3600
        cur = self.con.execute(
            "SELECT tokens FROM seen WHERE ts > ? ORDER BY ts DESC LIMIT 200", (since,)
        )
        return [set(r[0].split()) for r in cur.fetchall()]

    def last_result_too_old(self, ts_row) -> bool:
        return False

    def too_old(self, ts) -> bool:
        return time.time() - ts > STALE_H * 3600

    def last_seen(self, ch):
        row = self.con.execute("SELECT mid FROM last_seen WHERE ch=?", (ch,)).fetchone()
        return row[0] if row else 0

    def set_last_seen(self, ch, mid):
        with self.lock:
            self.con.execute("INSERT OR REPLACE INTO last_seen(ch, mid) VALUES(?,?)", (ch, mid))
            self.con.commit()
