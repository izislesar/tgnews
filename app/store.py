"""SQLite: каналы, посты, выдача админам, задачи доставки медиа, file_id кэш."""
import math
import sqlite3
import threading
import time


class Store:
    def __init__(self, path: str):
        self.lock = threading.Lock()
        self.con = sqlite3.connect(path, check_same_thread=False)
        self.con.execute("PRAGMA journal_mode=WAL")
        c = self.con
        c.execute("""CREATE TABLE IF NOT EXISTS channels(
            username TEXT PRIMARY KEY, title TEXT, active INTEGER DEFAULT 1,
            added_by TEXT, added_ts REAL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS posts(
            qid INTEGER PRIMARY KEY AUTOINCREMENT,
            src_bare INTEGER, username TEXT, ids TEXT, key TEXT UNIQUE,
            ts REAL, text TEXT, views INTEGER DEFAULT 0, reactions INTEGER DEFAULT 0,
            has_media INTEGER DEFAULT 0, state TEXT DEFAULT 'new')""")
        c.execute("""CREATE TABLE IF NOT EXISTS issued(
            qid INTEGER, admin_id INTEGER, ts REAL, PRIMARY KEY(qid, admin_id))""")
        c.execute("""CREATE TABLE IF NOT EXISTS tasks(
            qid INTEGER PRIMARY KEY, state TEXT DEFAULT 'pending',
            file_ids TEXT, updated REAL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS fileid_cache(
            key TEXT PRIMARY KEY, file_ids TEXT, ts REAL)""")
        c.execute("CREATE TABLE IF NOT EXISTS last_request(admin_id INTEGER PRIMARY KEY, ts REAL)")
        c.execute("CREATE TABLE IF NOT EXISTS last_seen(ch TEXT PRIMARY KEY, mid INTEGER)")
        c.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_posts_state ON posts(state, ts)")
        c.commit()

    # ---------- каналы ----------
    def seed_channels(self, usernames):
        with self.lock:
            n = self.con.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
            if not n:
                for u in usernames:
                    self.con.execute(
                        "INSERT OR IGNORE INTO channels(username, added_by, added_ts) VALUES(?,?,?)",
                        (u.lstrip("@"), "seed", time.time()))
                self.con.commit()

    def add_channel(self, username, added_by):
        with self.lock:
            self.con.execute(
                "INSERT INTO channels(username, added_by, added_ts) VALUES(?,?,?) "
                "ON CONFLICT(username) DO UPDATE SET active=1",
                (username.lstrip("@"), str(added_by), time.time()))
            self.con.commit()

    def add_channel_meta(self, username, title):
        with self.lock:
            self.con.execute("UPDATE channels SET title=? WHERE username=?",
                             (title, username.lstrip("@")))
            self.con.commit()

    def exists_post(self, key) -> bool:
        return self.con.execute("SELECT 1 FROM posts WHERE key=? LIMIT 1", (key,)).fetchone() is not None

    def post_by_qid(self, qid):
        row = self.con.execute(
            "SELECT src_bare, username, ids, key, text, has_media FROM posts WHERE qid=?", (qid,)
        ).fetchone()
        if not row:
            return None
        return dict(zip(("src_bare", "username", "ids", "key", "text", "has_media"), row))

    def del_channel(self, username):
        with self.lock:
            cur = self.con.execute(
                "UPDATE channels SET active=0 WHERE username=?", (username.lstrip("@"),))
            self.con.commit()
            return cur.rowcount > 0

    def list_channels(self, only_active=True):
        q = "SELECT username, title, active FROM channels" + (" WHERE active=1" if only_active else "")
        return self.con.execute(q + " ORDER BY username").fetchall()

    def active_usernames(self):
        return [r[0] for r in self.con.execute("SELECT username FROM channels WHERE active=1")]

    # ---------- посты ----------
    def upsert_post(self, src_bare, username, ids, key, text, views, reactions, has_media):
        with self.lock:
            self.con.execute(
                "INSERT OR IGNORE INTO posts(src_bare, username, ids, key, ts, text, views, reactions, has_media) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (src_bare, username, ids, key, time.time(), text, views or 0, reactions or 0, int(has_media)))

    def candidates(self, admin_id, limit=15, want_window_h=48):
        last = self.last_request(admin_id) or 0
        now = time.time()
        rows = self.con.execute(
            "SELECT qid, username, ids, key, text, views, reactions, ts, has_media FROM posts "
            "WHERE state='new' AND ts>=? AND ts>=? "
            "AND qid NOT IN (SELECT qid FROM issued WHERE admin_id=?)",
            (last, now - want_window_h * 3600, admin_id)).fetchall()
        if not rows:
            return []

        def score(r):
            views, reactions, ts = r[5], r[6], r[7]
            freshness = 0.5 ** ((now - ts) / 86400)  # полураспад сутки
            return math.log10((views or 0) + 10) * (1 + 0.05 * min(reactions or 0, 100)) * freshness

        rows.sort(key=score, reverse=True)
        return rows[:limit]

    def mark_dead(self, qid):
        with self.lock:
            self.con.execute("UPDATE posts SET state='dead' WHERE qid=?", (qid,))
            self.con.commit()

    def mark_issued(self, qid, admin_id):
        with self.lock:
            self.con.execute("INSERT OR IGNORE INTO issued(qid, admin_id, ts) VALUES(?,?,?)",
                             (qid, admin_id, time.time()))
            self.con.commit()

    def cleanup(self, max_age_h=48):
        cutoff = time.time() - max_age_h * 3600
        with self.lock:
            n = self.con.execute("DELETE FROM posts WHERE state IN ('new','dead') AND ts<?", (cutoff,)).rowcount
            self.con.commit()
            return n

    def stats(self):
        now = time.time()
        posts = self.con.execute("SELECT state, COUNT(*) FROM posts GROUP BY state").fetchall()
        backlog = self.con.execute(
            "SELECT COUNT(*) FROM posts WHERE state='new' AND ts>?", (now - 48 * 3600,)).fetchone()[0]
        return dict(posts, backlog48h=backlog)

    # ---------- задачи доставки медиа ----------
    def create_task(self, qid):
        with self.lock:
            self.con.execute(
                "INSERT INTO tasks(qid, state, updated) VALUES(?,?,?) "
                "ON CONFLICT(qid) DO UPDATE SET state='pending', updated=excluded.updated "
                "WHERE state='failed'",
                (qid, "pending", time.time()))
            self.con.commit()

    def pending_tasks(self):
        return [r[0] for r in self.con.execute(
            "SELECT qid FROM tasks WHERE state='pending' ORDER BY qid LIMIT 5")]

    def task_state(self, qid):
        return self.con.execute("SELECT state, file_ids FROM tasks WHERE qid=?", (qid,)).fetchone()

    def set_task(self, qid, state, file_ids=None):
        with self.lock:
            self.con.execute("UPDATE tasks SET state=?, file_ids=?, updated=? WHERE qid=?",
                             (state, file_ids, time.time(), qid))
            self.con.commit()

    # ---------- file_id кэш ----------
    def cached_fileids(self, key):
        row = self.con.execute("SELECT file_ids FROM fileid_cache WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def remember_fileids(self, key, file_ids):
        with self.lock:
            self.con.execute("INSERT OR REPLACE INTO fileid_cache(key, file_ids, ts) VALUES(?,?,?)",
                             (key, file_ids, time.time()))
            self.con.commit()

    # ---------- настройки ----------
    def get_setting(self, key, default=None):
        row = self.con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key, value):
        with self.lock:
            self.con.execute(
                "INSERT INTO settings(key, value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
            self.con.commit()

    # ---------- админы / last_seen ----------
    def last_request(self, admin_id):
        row = self.con.execute("SELECT ts FROM last_request WHERE admin_id=?", (admin_id,)).fetchone()
        return row[0] if row else 0

    def set_last_request(self, admin_id):
        with self.lock:
            self.con.execute("INSERT OR REPLACE INTO last_request(admin_id, ts) VALUES(?,?)",
                             (admin_id, time.time()))
            self.con.commit()

    def last_seen(self, ch):
        row = self.con.execute("SELECT mid FROM last_seen WHERE ch=?", (ch,)).fetchone()
        return row[0] if row else 0

    def set_last_seen(self, ch, mid):
        with self.lock:
            self.con.execute("INSERT OR REPLACE INTO last_seen(ch, mid) VALUES(?,?)", (ch, mid))
            self.con.commit()
