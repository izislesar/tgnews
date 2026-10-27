"""SQLite: каналы, посты, выдача админам, карточки модерации, задачи доставки, file_id кэш."""
import math
import os
import sqlite3
import threading
import time

DEDUP_JACCARD = float(os.getenv("DEDUP_JACCARD", "0.6"))
WINDOW_H = float(os.getenv("WINDOW_H", "24"))        # окно отбора для /get, часов
DEDUP_WINDOW_H = float(os.getenv("DEDUP_WINDOW_H", str(WINDOW_H)))  # окно дедупа, часов
FRESH_HALF = float(os.getenv("FRESH_HALF", "12"))    # полураспад свежести, часов
ERATE_W = float(os.getenv("ERATE_W", "10"))          # вес engagement rate
VRATE_EXP = float(os.getenv("VRATE_EXP", "0.5"))     # степень нормир. просмотров

_STOP = set("""a an and are as at be by for from has have in is it its of on or that the this to was were will with
и в во не на с со что как а то же бы за по у из о от до для есть был была были было его её ее их этот эта это
el la los las der die das le les un une des et est que qui pour dans sur""".split())


def _tokens(text):
    return {w for w in __import__("re").findall(r"[\w]{3,}", text.lower()) if w not in _STOP}


def _jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


class Store:
    def __init__(self, path: str):
        self.lock = threading.Lock()
        self.con = sqlite3.connect(path, check_same_thread=False)
        self.con.execute("PRAGMA journal_mode=WAL")
        c = self.con
        c.execute("""CREATE TABLE IF NOT EXISTS channels(
            username TEXT PRIMARY KEY, title TEXT, active INTEGER DEFAULT 1,
            added_by TEXT, added_ts REAL, subs INTEGER DEFAULT 0)""")
        c.execute("""CREATE TABLE IF NOT EXISTS posts(
            qid INTEGER PRIMARY KEY AUTOINCREMENT,
            src_bare INTEGER, username TEXT, ids TEXT, key TEXT UNIQUE,
            ts REAL, text TEXT, views INTEGER DEFAULT 0, reactions INTEGER DEFAULT 0,
            has_media INTEGER DEFAULT 0, state TEXT DEFAULT 'new', ru_text TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS issued(
            qid INTEGER, admin_id INTEGER, ts REAL, PRIMARY KEY(qid, admin_id))""")
        c.execute("""CREATE TABLE IF NOT EXISTS decisions(
            qid INTEGER, admin_id INTEGER, decision TEXT, ts REAL, PRIMARY KEY(qid, admin_id))""")
        c.execute("""CREATE TABLE IF NOT EXISTS tasks(
            qid INTEGER PRIMARY KEY, state TEXT DEFAULT 'pending',
            file_ids TEXT, updated REAL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS fileid_cache(
            key TEXT PRIMARY KEY, file_ids TEXT, ts REAL)""")
        c.execute("CREATE TABLE IF NOT EXISTS last_request(admin_id INTEGER PRIMARY KEY, ts REAL)")
        c.execute("CREATE TABLE IF NOT EXISTS last_seen(ch TEXT PRIMARY KEY, mid INTEGER)")
        c.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")
        c.execute("""CREATE TABLE IF NOT EXISTS cards(
            chat_id INTEGER, msg_id INTEGER, qid INTEGER, PRIMARY KEY(chat_id, msg_id))""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_posts_state ON posts(state, ts)")
        # миграции живых БД
        pcols = {r[1] for r in c.execute("PRAGMA table_info(posts)")}
        if "ru_text" not in pcols:
            c.execute("ALTER TABLE posts ADD COLUMN ru_text TEXT")
        ccols = {r[1] for r in c.execute("PRAGMA table_info(channels)")}
        if "subs" not in ccols:
            c.execute("ALTER TABLE channels ADD COLUMN subs INTEGER DEFAULT 0")
        c.commit()

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

    def set_subs(self, username, subs):
        with self.lock:
            self.con.execute("UPDATE channels SET subs=? WHERE username=?",
                             (int(subs or 0), username.lstrip("@")))
            self.con.commit()

    def exists_post(self, key) -> bool:
        return self.con.execute("SELECT 1 FROM posts WHERE key=? LIMIT 1", (key,)).fetchone() is not None

    def post_by_qid(self, qid):
        row = self.con.execute(
            "SELECT src_bare, username, ids, key, text, has_media, ru_text FROM posts WHERE qid=?", (qid,)
        ).fetchone()
        if not row:
            return None
        return dict(zip(("src_bare", "username", "ids", "key", "text", "has_media", "ru_text"), row))

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
        """Вставка с бесплатным дедупом: Jaccard-фингерпринт против постов за DEDUP_WINDOW_H часов."""
        toks = _tokens(text or "")
        cutoff = time.time() - DEDUP_WINDOW_H * 3600
        recent = self.con.execute(
            "SELECT text FROM posts WHERE ts>? AND state IN ('new')", (cutoff,)).fetchall()
        for (rt,) in recent:
            if _jaccard(toks, _tokens(rt)) >= DEDUP_JACCARD:
                state = "dup"
                break
        else:
            state = "new"
        with self.lock:
            self.con.execute(
                "INSERT OR IGNORE INTO posts(src_bare, username, ids, key, ts, text, views, reactions,"
                " has_media, state) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (src_bare, username, ids, key, time.time(), text, views or 0, reactions or 0,
                 int(has_media), state))

    def update_metrics(self, rows):
        """Пакетное обновление views/reactions: rows = [(qid, views, reactions), ...]"""
        with self.lock:
            self.con.executemany(
                "UPDATE posts SET views=?, reactions=? WHERE qid=?",
                [(v, r, q) for q, v, r in rows])
            self.con.commit()

    def fresh_ids_by_channel(self, max_age_h=None):
        """Сгруппированные по username ids новых постов в окне — для рефреша метрик."""
        cutoff = time.time() - (max_age_h or max(WINDOW_H, 2)) * 3600
        out = {}
        for u, ids in self.con.execute(
                "SELECT username, ids FROM posts WHERE state='new' AND ts>? ORDER BY ts DESC",
                (cutoff,)):
            out.setdefault(u, [])
            out[u].extend(int(x) for x in ids.split(",") if x)
        return out

    def set_ru_text(self, qid, ru_text):
        with self.lock:
            self.con.execute("UPDATE posts SET ru_text=? WHERE qid=?", (ru_text, qid))
            self.con.commit()

    def candidates(self, admin_id, limit=15):
        """Топ за WINDOW_H часов по нормированным просмотрам/реакциям.
        Исключает выданное этому админу и его решения (pool/reject)."""
        now = time.time()
        rows = self.con.execute(
            "SELECT qid, username, ids, key, text, views, reactions, ts, has_media FROM posts "
            "WHERE state='new' AND ts>=? "
            "AND qid NOT IN (SELECT qid FROM issued WHERE admin_id=?) "
            "AND qid NOT IN (SELECT qid FROM decisions WHERE admin_id=?)",
            (now - WINDOW_H * 3600, admin_id, admin_id)).fetchall()
        if not rows:
            return []
        subs = {u: s for u, s in self.con.execute("SELECT username, subs FROM channels")}

        def score(r):
            views, reactions, ts = (r[5] or 0), (r[6] or 0), r[7]
            ch_subs = subs.get(r[1]) or 0
            # нормировка на аудиторию: vrate ~ доля канала, посмотревшая пост
            vrate = (views + 1) / (ch_subs + 200) if ch_subs > 0 else min(math.log10(views + 10) / 5, 1)
            erate = reactions / max(views, 1) if views else 0
            freshness = 0.5 ** ((now - ts) / 3600 / FRESH_HALF)
            return (min(vrate, 1) ** VRATE_EXP) * (1 + ERATE_W * min(erate, 0.15)) * freshness

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

    # ---------- карточки выданных постов (для кнопок) ----------
    def map_card(self, chat_id, msg_id, qid):
        with self.lock:
            self.con.execute("INSERT OR IGNORE INTO cards(chat_id, msg_id, qid) VALUES(?,?,?)",
                             (chat_id, msg_id, qid))
            self.con.commit()

    def card_qid(self, chat_id, msg_id):
        row = self.con.execute("SELECT qid FROM cards WHERE chat_id=? AND msg_id=?",
                               (chat_id, msg_id)).fetchone()
        return row[0] if row else None

    # ---------- карточки модерации ----------
    def add_decision(self, qid, admin_id, decision):
        with self.lock:
            self.con.execute(
                "INSERT OR IGNORE INTO decisions(qid, admin_id, decision, ts) VALUES(?,?,?,?)",
                (qid, admin_id, decision, time.time()))
            self.con.commit()

    def decision_by(self, qid, admin_id):
        row = self.con.execute(
            "SELECT decision FROM decisions WHERE qid=? AND admin_id=?", (qid, admin_id)).fetchone()
        return row[0] if row else None

    def pooled_by_anyone(self, qid):
        row = self.con.execute(
            "SELECT 1 FROM decisions WHERE qid=? AND decision='pool' LIMIT 1", (qid,)).fetchone()
        return row is not None

    def cleanup(self, max_age_h=48):
        cutoff = time.time() - max_age_h * 3600
        with self.lock:
            n = self.con.execute(
                "DELETE FROM posts WHERE state IN ('new','dead','dup') AND ts<?", (cutoff,)).rowcount
            self.con.commit()
            return n

    def stats(self):
        now = time.time()
        backlog = self.con.execute(
            "SELECT COUNT(*) FROM posts WHERE state='new' AND ts>?",
            (now - WINDOW_H * 3600,)).fetchone()[0]
        states = dict(self.con.execute("SELECT state, COUNT(*) FROM posts GROUP BY state").fetchall())
        states["backlog_window"] = backlog
        return states

    def stats_by_source(self, days=7):
        cutoff = time.time() - days * 86400
        return self.con.execute(
            "SELECT p.username, COUNT(*), "
            "SUM(p.state='dead'), SUM(p.state='dup'), "
            "(SELECT COUNT(*) FROM issued i JOIN posts p2 ON i.qid=p2.qid "
            "   WHERE p2.username=p.username AND i.ts>?), "
            "(SELECT COUNT(*) FROM decisions d JOIN posts p3 ON d.qid=p3.qid "
            "   WHERE p3.username=p.username AND d.decision='pool' AND d.ts>?) "
            "FROM posts p WHERE p.ts>? GROUP BY p.username ORDER BY COUNT(*) DESC",
            (cutoff, cutoff, cutoff)).fetchall()

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

    # ---------- админы / last_seen ----------
    def last_request(self, admin_id):
        row = self.con.execute("SELECT ts FROM last_request WHERE admin_id=?", (admin_id,)).fetchone()
        return row[0] if row else None

    def set_last_request(self, admin_id):
        with self.lock:
            self.con.execute(
                "INSERT INTO last_request(admin_id, ts) VALUES(?,?) "
                "ON CONFLICT(admin_id) DO UPDATE SET ts=excluded.ts", (admin_id, time.time()))
            self.con.commit()

    def last_seen(self, ch):
        row = self.con.execute("SELECT mid FROM last_seen WHERE ch=?", (ch,)).fetchone()
        return row[0] if row else 0

    def set_last_seen(self, ch, mid):
        with self.lock:
            self.con.execute(
                "INSERT INTO last_seen(ch, mid) VALUES(?,?) "
                "ON CONFLICT(ch) DO UPDATE SET mid=excluded.mid", (ch, mid))
            self.con.commit()
