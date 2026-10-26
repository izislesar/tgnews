"""Бот-интерфейс для админов: /get [N], /add_channel, /delete_channel, /list_channels, /stats, /help."""
import asyncio
import html
import json
import os
import re

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import (Message, CallbackQuery, BotCommand,
                           InlineKeyboardMarkup, InlineKeyboardButton,
                           InputMediaPhoto, InputMediaVideo, InputMediaAnimation, InputMediaDocument)
from telethon import functions

from . import llm

DEFAULT_N = int(os.getenv("GET_N", "5"))
MAX_N = int(os.getenv("GET_MAX", "10"))
CANDIDATES = int(os.getenv("GET_CANDIDATES", "15"))
MEDIA_WAIT_S = int(os.getenv("MEDIA_WAIT_S", "120"))
LINK_GAP = int(os.getenv("LINK_GAP", "5"))  # пустых строк между постом и ссылкой на источник

KIND_MAP = {
    "photo": InputMediaPhoto,
    "video": InputMediaVideo,
    "animation": InputMediaAnimation,
    "document": InputMediaDocument,
}
SENDERS = {"photo": "send_photo", "video": "send_video",
           "animation": "send_animation", "document": "send_document"}

MENU = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="Получить посты", callback_data="get")],
    [InlineKeyboardButton(text="Каналы", callback_data="list"),
     InlineKeyboardButton(text="Статистика", callback_data="stats")],
])

CARD = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="В отбор", callback_data="pool")],
])


def fmt_html(text: str) -> str:
    """Разметка модели -> Telegram HTML:
    **жирный** -> <b>, голые URL -> <a>, строки '>> ' -> цитата (blockquote).
    Длинные цитаты (>200 симв.) делают expandable — сворачиваются на клиенте."""
    out = []
    qbuf = []

    def flush_q():
        if not qbuf:
            return
        inner = "\n".join(qbuf)
        qbuf.clear()
        tag = "blockquote expandable" if len(inner) > 200 else "blockquote"
        out.append(f"<{tag}>{html.escape(inner, quote=False)}</{tag}>")

    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith(">>"):
            qbuf.append(stripped[2:].strip())
            continue
        flush_q()
        s = html.escape(line, quote=False)
        s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s, flags=re.S)
        s = re.sub(r"(?<![/\"'>])\b(https?://[^\s<]+)", r'<a href="\1">\1</a>', s)
        if s.count("<b>") != s.count("</b>"):
            s = re.sub(r"</?b>", "", s)
        out.append(s)
    flush_q()
    return "\n".join(out)


class AdminBot:
    def __init__(self, store, worker, admin_ids: set[int]):
        token = os.environ.get("BOT_TOKEN")
        if not token:
            raise SystemExit("BOT_TOKEN пуст — вписать токен из BotFather в .env")
        self.bot = Bot(token)
        self.dp = Dispatcher()
        self.store = store
        self.worker = worker
        self.admin_ids = admin_ids
        self.relay_id = int(store.get_setting("relay_chat_id", "0") or 0)
        self.pool_chat = int(os.environ.get("APPROVAL_CHAT_ID") or
                             store.get_setting("approval_chat_id", "0") or 0)
        self.relay_buffers = {}
        self._register()

    # ---------- хендлеры ----------
    def _register(self):
        _CMDS = ("get", "add_channel", "delete_channel", "list_channels", "stats",
                 "set_approval_chat", "help", "start", "menu")

        @self.dp.message(Command(*_CMDS), F.chat.type == "private")
        async def commands(message: Message, command: CommandObject):
            if not message.from_user or message.from_user.id not in self.admin_ids:
                return  # чужих молча игнорируем
            await self._route(message, command)

        @self.dp.message(Command("set_approval_chat"), F.chat.type.in_({"group", "supergroup"}))
        async def set_pool_chat(message: Message):
            if not message.from_user or message.from_user.id not in self.admin_ids:
                return
            self.store.set_setting("approval_chat_id", str(message.chat.id))
            self.pool_chat = message.chat.id
            await message.answer("Чат отбора назначен. Одобренные посты будут падать сюда.")

        @self.dp.message(F.chat.type == "private", F.text.startswith("/"))
        async def unknown_cmd(message: Message):
            if not message.from_user or message.from_user.id not in self.admin_ids:
                return
            await message.answer("Неизвестная команда. /help")

        @self.dp.callback_query(F.data.in_({"get", "list", "stats", "help"}))
        async def callbacks(cb: CallbackQuery):
            if cb.from_user.id not in self.admin_ids:
                await cb.answer("Нет доступа", show_alert=False)
                return
            await cb.answer()
            route = {"get": self.cmd_get, "list": self.cmd_list,
                     "stats": self.cmd_stats, "help": self.cmd_help}
            await route[cb.data](cb.message, None, admin_id=cb.from_user.id)

        @self.dp.callback_query(F.data == "pool")
        async def card_cb(cb: CallbackQuery):
            if cb.from_user.id not in self.admin_ids:
                await cb.answer("Нет доступа", show_alert=False)
                return
            qid = self.store.card_qid(cb.message.chat.id, cb.message.message_id)
            if not qid or not self.store.post_by_qid(qid):
                await cb.answer("Пост не найден", show_alert=False)
                return
            if self.store.pooled_by_anyone(qid):
                await cb.answer("Этот пост уже в отборе")
                await self._edit_card(cb.message, None)
                return
            ok = await self._send_to_pool(qid, cb.from_user)
            if ok:
                self.store.add_decision(qid, cb.from_user.id, "pool")
                await cb.answer("Добавлено в отбор")
                await self._edit_card(cb.message, "— в отборе")
            else:
                await cb.answer("Не вышло: чат отбора не задан (/set_approval_chat)",
                                show_alert=True)

        # релей: юзербот постит медиа с маркером #Q в канал-релей
        @self.dp.channel_post(F.chat.id == self.relay_id)
        async def relay(message: Message):
            await self._relay_message(message)

    # ---------- релей ----------
    async def _relay_message(self, message: Message):
        cap = message.caption or ""
        mm = re.match(r"#Q(\d+)", cap.strip())
        if not mm:
            return
        qid = int(mm.group(1))
        mgid = message.media_group_id
        if mgid is None:
            await self._capture(qid, [message])
        else:
            key = (message.chat.id, mgid)
            buf = self.relay_buffers.setdefault(key, [])
            buf.append(message)
            if len(buf) == 1:
                asyncio.get_event_loop().call_later(
                    2.5, lambda: asyncio.create_task(self._flush(key)))

    async def _flush(self, key):
        msgs = self.relay_buffers.pop(key, [])
        if not msgs:
            return
        mm = re.match(r"#Q(\d+)", (msgs[0].caption or "").strip())
        if not mm:
            return
        await self._capture(int(mm.group(1)), msgs)

    async def _capture(self, qid, msgs):
        out = []
        for m in msgs:
            if m.photo:
                out.append({"kind": "photo", "id": m.photo[-1].file_id})
            elif m.video:
                out.append({"kind": "video", "id": m.video.file_id})
            elif m.animation:
                out.append({"kind": "animation", "id": m.animation.file_id})
            elif m.document:
                out.append({"kind": "document", "id": m.document.file_id})
        self.store.set_task(qid, "done", json.dumps(out, ensure_ascii=False))
        post = self.store.post_by_qid(qid)
        if post:
            self.store.remember_fileids(post["key"], json.dumps(out, ensure_ascii=False))
        for m in msgs:  # прибираем за собой — релей-канал не должен засоряться
            try:
                await m.delete()
            except Exception as e:
                print(f"[relay] delete: {e}")

    # ---------- команды ----------
    async def _route(self, message: Message, command: CommandObject):
        handler = {
            "get": self.cmd_get,
            "add_channel": self.cmd_add,
            "delete_channel": self.cmd_del,
            "list_channels": self.cmd_list,
            "stats": self.cmd_stats,
            "help": self.cmd_help,
            "start": self.cmd_help,
            "menu": self.cmd_help,
        }.get(command.command.lower())
        if handler:
            await handler(message, command)

    async def cmd_get(self, message: Message, command: CommandObject, admin_id: int | None = None):
        admin = admin_id or message.from_user.id
        n = DEFAULT_N
        if command and command.args:
            try:
                n = max(1, min(MAX_N, int(command.args.strip().split()[0])))
            except ValueError:
                await message.answer(f"/get [N] — от 1 до {MAX_N}, по умолчанию {DEFAULT_N}")
                return
        await message.answer("Посты готовятся")
        cands = self.store.candidates(admin, limit=CANDIDATES)
        if not cands:
            await message.answer("Новых постов нет (окно 48 часов).")
            return

        # перевод и ожидание медиарелея — параллельно
        translate_task = asyncio.create_task(llm.translate_many([c[4] for c in cands]))
        created = []
        for qid, username, ids, key, text, views, reactions, ts, has_media in cands:
            if not has_media:
                continue
            if self.store.cached_fileids(key) is not None:
                continue
            self.store.create_task(qid)
            created.append(qid)
        if created:
            deadline = asyncio.get_event_loop().time() + MEDIA_WAIT_S
            while asyncio.get_event_loop().time() < deadline:
                states = [self.store.task_state(q) for q in created]
                if all(s and s[0] in ("done", "failed") for s in states):
                    break
                await asyncio.sleep(1)

        delivered = 0
        results = await translate_task
        for (qid, username, ids, key, text, views, reactions, ts, has_media), out in zip(cands, results):
            if delivered >= n:
                break
            if out is False:  # LLM-сбой — пост остаётся в бэклоге
                continue
            if out is None:  # SKIP (реклама/не-новость)
                self.store.mark_dead(qid)
                continue
            self.store.set_ru_text(qid, out)

            url = f"https://t.me/{username}/{ids.split(',')[0]}"
            body = fmt_html(out) + "\n" * LINK_GAP + f"<a href=\"{url}\">{url}</a>"
            try:
                await self._deliver(message.chat.id, key if has_media else None, body, qid=qid)
                delivered += 1
                self.store.mark_issued(qid, admin)
            except Exception as e:
                print(f"[deliver {qid}] {e}")
        await message.answer(f"Выдано {delivered} из {n}")
        self.store.set_last_request(admin)

    async def _deliver(self, chat_id: int, cache_key: str | None, body_html: str,
                       qid: int | None = None):
        """Одно сообщение: медиа+подпись (<=1024) или медиа+текст следом, или просто текст.
        С qid вешает карточку модерации (кнопки В отбор/Отказ)."""
        items = json.loads(cache_key and self.store.cached_fileids(cache_key) or "[]")
        items = [it for it in items if it.get("id")]
        markup = CARD if qid else None
        sent = []
        if not items:
            sent.append(await self.bot.send_message(chat_id, body_html, parse_mode="HTML",
                                                    reply_markup=markup))
        else:
            # альбом: карточку к media group не прикрепить (Bot API) — шлём альбом
            # без подписи, а текст + карточку следом отдельным сообщением
            media = [KIND_MAP[it["kind"]](media=it["id"]) for it in items]
            sent += await self.bot.send_media_group(chat_id, media=media)
            sent.append(await self.bot.send_message(chat_id, body_html, parse_mode="HTML",
                                                    reply_markup=markup))
        if qid:
            for m in sent:
                self.store.map_card(chat_id, m.message_id, qid)

    async def _edit_card(self, message: Message, note: str | None):
        try:
            await message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        if note:
            try:
                if message.caption:
                    await message.edit_caption(caption=message.caption + note, parse_mode="HTML")
                elif message.text:
                    await message.edit_text(text=message.text + note, parse_mode="HTML")
            except Exception:
                pass

    async def _send_to_pool(self, qid: int, user) -> bool:
        """Копия поста в общий чат отбора + пометка, кто добавил."""
        if not self.pool_chat:
            return False
        post = self.store.post_by_qid(qid)
        who = f"@{user.username}" if user.username else user.first_name
        url = f"https://t.me/{post['username']}/{post['ids'].split(',')[0]}"
        ru = post.get("ru_text") or post.get("text") or ""
        body = (fmt_html(ru) + "\n" * LINK_GAP + f"<a href=\"{url}\">{url}</a>"
                + f"\n\nДобавил: {html.escape(who)}")
        key = post["key"] if post["has_media"] else None
        try:
            await self._deliver(self.pool_chat, key, body)
            return True
        except Exception as e:
            print(f"[pool {qid}] {e}")
            return False

    async def cmd_add(self, message: Message, command: CommandObject):
        username = (command.args or "").strip().lstrip("@")
        if not username:
            await message.answer("Использование: /add_channel @username")
            return
        try:
            ent = await self.worker.client.get_entity(username)
            await self.worker.client(functions.channels.JoinChannelRequest(ent))
            self.store.add_channel(username, message.from_user.id)
            self.store.add_channel_meta(username, getattr(ent, "title", "") or "")
            await message.answer(f"Канал {username} добавлен и взят в работу.")
        except Exception as e:
            await message.answer(f"Не получилось: {e}")

    async def cmd_del(self, message: Message, command: CommandObject):
        username = (command.args or "").strip()
        if not username:
            await message.answer("Использование: /delete_channel @username")
            return
        ok = self.store.del_channel(username)
        await message.answer("Канал убран из работы." if ok else "Такого канала в списке нет.")

    async def cmd_list(self, message: Message, command: CommandObject, admin_id: int | None = None):
        rows = self.store.list_channels(only_active=False)
        if not rows:
            await message.answer("Список пуст.")
            return
        lines = [f"{'[x]' if active else '[ ]'} @{u} — {title or '—'}" for u, title, active in rows]
        await message.answer("Каналы-доноры:\n" + "\n".join(lines))

    async def cmd_stats(self, message: Message, command: CommandObject, admin_id: int | None = None):
        from .store import WINDOW_H
        s = self.store.stats()
        u = llm.USAGE
        lines = [
            f"В окне ({int(WINDOW_H)}ч): {s.get('backlog_window', 0)} новых",
            f"Всего: new={s.get('new', 0)} dead={s.get('dead', 0)} dup={s.get('dup', 0)}",
            f"LLM: {u['calls']} вызовов, in={u['in']} out={u['out']} cached={u['cached']} "
            f"(режим {llm.MODE})",
            "",
            "Источники (7 дней): всего / скип / дубли / выдано / в отбор",
        ]
        for u, total, dead, dup, iss, pool in self.store.stats_by_source():
            lines.append(f"@{u}: {total} / {dead or 0} / {dup or 0} / {iss or 0} / {pool or 0}")
        await message.answer("\n".join(lines))

    async def cmd_help(self, message: Message, command: CommandObject, admin_id: int | None = None):
        await message.answer(
            "Собирает посты из каналов-доноров и выдаёт по запросу: перевод на русский "
            "в заданном стиле + ссылка на первоисточник.\n\n"
            "/get [N] — получить посты с момента твоего прошлого запроса "
            f"(по умолчанию {DEFAULT_N}, максимум {MAX_N})\n"
            "/add_channel @user — добавить канал-донор\n"
            "/delete_channel @user — убрать канал\n"
            "/list_channels — список каналов\n"
            "/stats — статистика по источникам\n"
            "/set_approval_chat — назначить этот чат чатом отбора (отправить В нём)\n"
            "/help — эта справка\n\n"
            "Под каждым постом — кнопки: В отбор (в общий чат для доработки) / Отказ.",
            reply_markup=MENU)

    # ---------- запуск ----------
    async def run(self):
        await self.bot.set_my_commands([
            BotCommand(command="get", description="получить посты: /get [N]"),
            BotCommand(command="list_channels", description="список каналов-доноров"),
            BotCommand(command="add_channel", description="добавить канал: /add_channel @user"),
            BotCommand(command="delete_channel", description="убрать канал: /delete_channel @user"),
            BotCommand(command="stats", description="состояние очереди"),
            BotCommand(command="help", description="справка"),
        ])
        await self.dp.start_polling(self.bot)
