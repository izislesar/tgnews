"""Worker (Telethon): коллектор постов из каналов + доставка медиа в личку бота (file_id relay)."""
import asyncio
import json
import os
import re
import time

from telethon import TelegramClient, events, functions, types, utils


def _rid() -> int:
    return int.from_bytes(os.urandom(8), "big") >> 1  # знаковый int64


class Worker:
    def __init__(self, store, session_path, api_id, api_hash, bot_username):
        self.store = store
        self.bot_username = bot_username
        self.client = TelegramClient(session_path, api_id, api_hash)
        self.user_id = None
        self._ents = {}  # username -> entity cache

    # ---------- запуск ----------
    async def run(self):
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise SystemExit(
                "Сессия worker'а не авторизована. Запусти:\n"
                "  docker compose run --rm -it -e SESSION_PATH=<путь> app python -m app.bootstrap")
        me = await self.client.get_me()
        self.user_id = me.id
        print(f"[worker] userbot: {me.first_name} (@{me.username}) id={me.id}")

        self.store.seed_channels(
            [l.strip().lstrip("@") for l in open("/app/channels.txt", encoding="utf-8")
             if l.strip() and not l.strip().startswith("#")])
        await self.join_all()
        await self.ensure_relay()
        await self.backfill()

        self.client.add_event_handler(self.on_message, events.NewMessage())
        self.client.add_event_handler(self.on_album, events.Album())
        asyncio.create_task(self.task_loop())
        asyncio.create_task(self.cleanup_loop())

    async def join_all(self):
        for username in self.store.active_usernames():
            try:
                ent = await self.client.get_entity(username)
                self._ents[username] = ent
                try:
                    await self.client(functions.channels.JoinChannelRequest(ent))
                except Exception:
                    pass  # уже участник
                self.store.add_channel_meta(username, getattr(ent, "title", "") or "")
            except Exception as ex:
                print(f"[worker] канал {username}: {ex}")
        print(f"[worker] каналов в работе: {len(self.store.active_usernames())}")

    def _entity(self, username):
        if username not in self._ents:
            raise KeyError(username)
        return self._ents[username]

    async def ensure_relay(self):
        """Скрытый канал-релей для передачи медиа боту (file_id relay).
        Публика юзербота постит туда медиа с маркером #Q<qid>, бот-админ ловит
        file_id и удаляет релей-пост — в чатах админов чисто."""
        rid = self.store.get_setting("relay_chat_id")
        bot_ent = await self.client.get_entity(self.bot_username)
        if rid:
            try:
                self.relay_ent = await self.client.get_entity(int(rid))
                await self._ensure_bot_admin(bot_ent)
                print(f"[relay] канал готов: {self.relay_ent.id}")
                return
            except Exception as e:
                print(f"[relay] старый канал недоступен ({e}), создаю новый")
        res = await self.client(functions.channels.CreateChannelRequest(
            title="Q Relay", about="internal media relay", megagroup=False))
        self.relay_ent = await self.client.get_entity(res.chats[0].id)
        await self._ensure_bot_admin(bot_ent)
        self.store.set_setting("relay_chat_id", str(utils.get_peer_id(self.relay_ent)))
        print(f"[relay] канал создан: {self.relay_ent.title} ({utils.get_peer_id(self.relay_ent)})")

    async def _ensure_bot_admin(self, bot_ent):
        # в broadcast-канале бот добавляется сразу как админ (постит и удаляет релей-посты)
        try:
            await self.client(functions.channels.EditAdminRequest(
                self.relay_ent, bot_ent,
                types.ChatAdminRights(post_messages=True, delete_messages=True), ""))
        except Exception as e:
            print(f"[relay] права бота: {e}")

    async def backfill(self):
        """Стартовый добор: до 20 последних постов с каждого канала, дальше — realtime."""
        total = 0
        for username in self.store.active_usernames():
            try:
                ent = await self.client.get_entity(username)
                self._ents[username] = ent
                last = self.store.last_seen(username)
                fresh = []
                async for m in self.client.iter_messages(ent, limit=20):
                    if m.id <= last:
                        break
                    fresh.append(m)
                if not fresh:
                    continue
                fresh.reverse()
                groups = {}
                for m in fresh:
                    if m.grouped_id is not None:
                        groups.setdefault(m.grouped_id, []).append(m)
                    else:
                        self._collect([m])
                for g in groups.values():
                    self._collect(g)
                self.store.set_last_seen(username, max(m.id for m in fresh))
                total += len(fresh)
            except Exception as ex:
                print(f"[backfill] {username}: {ex}")
        print(f"[backfill] подшито постов: {total}")

    # ---------- коллектор ----------
    def _collect(self, msgs):
        m0 = msgs[0]
        chat = m0.chat
        if not getattr(chat, "broadcast", False):
            return
        username = getattr(chat, "username", None) or ""
        if username.lower() not in {u.lower() for u in self.store.active_usernames()}:
            return
        if any(getattr(m, "fwd_from", None) is not None for m in msgs):
            return  # репосты чужих каналов — мимо
        text = next((m.raw_text for m in msgs if m.raw_text), "")
        if not text.strip():
            return  # медиа без текста не переводим
        reactions = 0
        if m0.reactions:
            reactions = sum(rc.count for rc in m0.reactions.results)
        views = m0.views or 0
        ids = ",".join(str(m.id) for m in msgs)
        key = str(m0.grouped_id) if m0.grouped_id is not None else f"m{m0.id}"
        if self.store.exists_post(key):
            return
        self.store.upsert_post(chat.id, username, ids, key, text, views, reactions,
                               has_media=any(m.photo or m.document for m in msgs))

    async def on_message(self, event):
        m = event.message
        if m is None or m.chat is None:
            return
        if m.grouped_id is not None:
            return  # альбомы ловим событием Album целиком
        self._collect([m])

    async def on_album(self, event):
        self._collect(event.messages)

    # ---------- доставка медиа в канал-релей (file_id relay) ----------
    async def task_loop(self):
        while True:
            try:
                for qid in self.store.pending_tasks():
                    try:
                        await self.process_task(qid)
                    except Exception as e:
                        print(f"[task {qid}] {e}")
                        self.store.set_task(qid, "failed", None)
            except Exception as e:
                print(f"[task_loop] {e}")
            await asyncio.sleep(1.5)

    async def process_task(self, qid):
        row = self.store.post_by_qid(qid)
        if not row:
            self.store.set_task(qid, "done", json.dumps([]))
            return
        ids = [int(x) for x in row["ids"].split(",") if x]
        username = row["username"]
        ent = self._entity(username) if username in self._ents else await self.client.get_entity(username)
        self._ents[username] = ent
        msgs = [m for m in await self.client.get_messages(ent, ids=ids) if m]
        media_msgs = [m for m in msgs if m.photo or m.document]
        if not media_msgs:
            self.store.set_task(qid, "done", json.dumps([]))
            return

        singles = []
        for m in media_msgs:
            media = utils.get_input_media(m.media)
            if media is None:
                continue
            singles.append(types.InputSingleMedia(
                media=media,
                message=f"#Q{qid}",
                random_id=_rid(),
            ))
        if not singles:
            self.store.set_task(qid, "done", json.dumps([]))
            return
        if len(singles) == 1:
            await self.client(functions.messages.SendMediaRequest(
                peer=self.relay_ent, media=singles[0].media,
                message=f"#Q{qid}", random_id=singles[0].random_id))
        else:
            await self.client(functions.messages.SendMultiMediaRequest(
                peer=self.relay_ent, multi_media=singles))
        # state=done проставит бот-хендлер, когда получит file_id

    # ---------- утилиты ----------
    async def cleanup_loop(self):
        while True:
            try:
                n = self.store.cleanup(48)
                if n:
                    print(f"[cleanup] старьё удалено: {n}")
            except Exception as e:
                print(f"[cleanup] {e}")
            await asyncio.sleep(1800)
