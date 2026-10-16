import asyncio
import os
import random
import re
import time
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo

from telethon import TelegramClient, events, functions, types, utils

from .store import Store
from .rewriter import rewrite, volume_plan, CAP_MEDIA, CAP_TEXT

TZ = ZoneInfo(os.getenv("TZ", "Europe/Berlin"))
WINDOW_START = int(os.getenv("WINDOW_START", "7"))
WINDOW_END = int(os.getenv("WINDOW_END", "22"))
RATE = int(os.getenv("RATE_SECONDS", "180"))  # 1 пара черновиков за столько секунд (180 = 20/час)
DUP_WINDOW_H = 6
DUP_THRESHOLD = 0.6  # жаккар по токенам против присланных за последние N часов
SESSION_PATH = os.getenv("SESSION_PATH", "/data/session")
# Куда сыпать черновики. Варианты:
#   'me'        — Saved Messages
#   '@username' — бот из BotFather или публичный канал
#   -100xxxxxx  — числовой ID закрытой группы (узнаётся через bootstrap.py)
# Пишет юзербот — никаких ограничений Bot API и перезаливки медиа нет.
RAW_TARGET = os.getenv("DRAFT_TARGET", "me")
DRAFT_TARGET = int(RAW_TARGET) if RAW_TARGET.lstrip("-").isdigit() else RAW_TARGET

with open("/app/channels.txt") as f:
    CHANNELS = [l.strip().lstrip("@") for l in f if l.strip() and not l.strip().startswith("#")]

# ЖЁСТКИЙ ALLOWLIST: коллектор берёт посты ТОЛЬКО из этих каналов.
# Заполняется при старте из CHANNELS; пока пуст — все посты игнорируются (fail-closed).
ALLOW_BARE = set()    # bare channel id (положительный, entity.id)
ALLOW_MARKED = set()  # marked id (-100...)


def in_allow(chat) -> bool:
    if chat is None:
        return False
    cid = getattr(chat, "id", None)
    return cid in ALLOW_BARE or cid in ALLOW_MARKED


async def build_allow(client):
    """Резолвит список каналов + АВТО-ДЖОЙН (для свежесозданного юзер-аккаунта).
    Новые каналы добавляются просто правкой channels.txt + restart."""
    for ch in CHANNELS:
        try:
            e = await client.get_entity(ch)
        except Exception as ex:
            print(f"[allow] не резолвится {ch}: {ex}")
            continue
        ALLOW_BARE.add(e.id)
        ALLOW_MARKED.add(utils.get_peer_id(e))
        try:
            await client(functions.channels.JoinChannelRequest(e))
        except Exception:
            pass  # уже участник или приватный — не критично
    print(f"[allow] allowlist: {len(ALLOW_BARE)} каналов")


store = Store("/data/queue.db")


async def resolve_target(client, raw):
    """Резолвит цель: '@username' / 'me' / -100... (супергруппа/канал) / базовая группа.
    Базовые группы (ID без префикса -100) по голому числу не резолвятся — сканируем диалоги.
    Если группа мигрировала в супергруппу (migrated_to) — следуем за новой сущностью,
    иначе сервер отвечает PEER_ID_INVALID на каждый пост."""
    ent = None
    try:
        ent = await client.get_entity(raw)
    except Exception:
        want = int(raw) if isinstance(raw, str) and raw.lstrip("-").isdigit() else raw
        async for d in client.iter_dialogs():
            if d.id == want:
                ent = d.entity
                break
    if ent is None:
        raise ValueError(f"Не найдена цель черновиков: {raw} (нет в диалогах аккаунта)")
    # за миграцией в супергруппу — сколько бы раз ни переезжала
    hops = 0
    while getattr(ent, "migrated_to", None) is not None and hops < 5:
        ent = await client.get_entity(ent.migrated_to)
        hops += 1
    return ent

# Публичные ключи Telegram Desktop — my.telegram.org не нужен.
# Как только получишь собственные api_id/api_hash — переопредели через .env
DEFAULT_API_ID = 2040
DEFAULT_API_HASH = "b18441a1ff607e10a989891a5462e627"  # официальные ключи Telegram Desktop

client = TelegramClient(
    SESSION_PATH,
    int(os.environ.get("API_ID", DEFAULT_API_ID)),
    os.environ.get("API_HASH", DEFAULT_API_HASH),
)


def in_window() -> bool:
    return WINDOW_START <= datetime.now(TZ).hour < WINDOW_END


def norm_tokens(text: str) -> set:
    text = unicodedata.normalize("NFKD", text)
    text = re.sub(r"[^\w\s]", " ", text.lower(), flags=re.UNICODE)
    return {t for t in text.split() if len(t) > 2}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _rid() -> int:
    # random_id обязан быть знаковым int64 — без этого 'q' struct.error
    return random.randrange(1, 2**63)


def valid_media(m) -> bool:
    """Только фото/видео/гиф (документы). Вебпревью/опросы/сервисные — не медиа."""
    return bool(getattr(m, "photo", None) or getattr(m, "document", None))


async def send_media_copy(client, tgt, msgs, caption: str):
    """Копия медиа (в т.ч. альбом) из чужого чата + наша подпись.
    Строим InputSingleMedia руками — send_file с чужими media-объектами
    падает на резолве пиров."""
    from telethon.extensions import markdown as md_lib
    text, entities = md_lib.parse(caption or "")
    singles = []
    for idx, m in enumerate(msgs):
        media = utils.get_input_media(m.media)
        if media is None:
            continue
        singles.append(types.InputSingleMedia(
            media=media,
            message=text if idx == 0 else "",
            entities=entities if idx == 0 else [],
            random_id=_rid(),
        ))
    if not singles:
        raise ValueError("нет валидного медиа для копии")
    if len(singles) == 1:
        await client(functions.messages.SendMediaRequest(
            peer=tgt, media=singles[0].media,
            message=text, entities=entities,
            random_id=_rid(),
        ))
    else:
        await client(functions.messages.SendMultiMediaRequest(peer=tgt, multi=singles))


def md_safe(s: str) -> str:
    """Жирный вывод модели (**...) schennai для parse_mode='md':
    если звёздочек нечётное число — снимаем разбаланс, чтобы телегу не падала."""
    if s.count("**") % 2:
        s = s.replace("**", "")
    return s


def is_ad_or_chatter(text: str) -> bool:
    """Дешёвые локальные фильтры ДО LLM: реклама и посты-голые-ссылки — мимо сразу."""
    low = text.lower()
    ad_markers = (
        "crypto", "trading", "don't miss", "don`t miss", "не пропусти",
        "подпишись", "subscribe", "referral", "промокод", "promo", "affiliate",
    )
    if any(m in low for m in ad_markers):
        return True
    # пост = одна ссылка + максимум огрызок текста (как у theriotimes)
    links = re.findall(r"https?://\S+", text)
    no_links = re.sub(r"https?://\S+", "", text).strip()
    if links and len(no_links) < 40:
        return True
    return False


async def queue_message(msgs, text: str):
    first = msgs[0]
    if not in_allow(first.chat):
        return  # ЖЁСТКИЙ фильтр: только каналы из channels.txt
    if getattr(first, "fwd_from", None) is not None:
        return  # это РЕПОСТ чужого канала внутри донора — не авторский пост, скип
    if not text or not text.strip():
        return  # медиа без текста — скип
    if is_ad_or_chatter(text):
        return
    key = str(getattr(first, "grouped_id", None) or f"m{first.id}")
    if store.exists(first.chat_id, key):
        return  # защита от повторной доставки того же поста/альбома
    ids = ",".join(str(m.id) for m in msgs)
    store.enqueue(first.chat_id, ids, key, text)


@client.on(events.NewMessage(func=lambda e: not e.is_group and e.is_channel))
async def on_new_message(event):
    await queue_message([event.message], event.message.raw_text or "")


@client.on(events.Album)
async def on_album(event):
    await queue_message(event.messages, event.messages[0].raw_text or "")


async def backfill():
    """Подписка на каналы + добор пропущенного после дауна с последнего виденного id."""
    for ch in CHANNELS:
        try:
            ent = await client.get_entity(ch)
            last = store.last_seen(ch)
            new_msgs = []
            async for m in client.iter_messages(ent, min_id=last, limit=30):
                new_msgs.append(m)
            if not new_msgs:
                continue
            if ent.id not in ALLOW_BARE:
                continue  # перестраховка: backfill только по allowlist
            new_msgs.sort(key=lambda m: m.id)
            for m in new_msgs:
                if m.grouped_id is not None:
                    # альбомы: очередь кладём один раз по первому сообщению группы
                    first_in_group = min(x.id for x in new_msgs if x.grouped_id == m.grouped_id)
                    if m.id != first_in_group:
                        continue
                text = m.raw_text or ""
                if m.fwd_from is None and text.strip() and not is_ad_or_chatter(text):
                    key = str(m.grouped_id) if m.grouped_id is not None else f"m{m.id}"
                    if not store.exists(ent.id, key):
                        store.enqueue(ent.id, str(m.id), key, text)
            store.set_last_seen(ch, max(m.id for m in new_msgs))
        except Exception as e:
            print(f"[backfill] {ch}: {e}")


async def send_draft() -> bool:
    rows = store.pending()  # свежие первыми
    for src, ids, key, text, qid, ts in rows:
        if store.too_old(ts):  # пост старше 12 часов — не актуален
            store.mark(qid, "skipped")
            continue
        toks = norm_tokens(text)
        if any(jaccard(t, toks) >= DUP_THRESHOLD for t in store.recent_sets(DUP_WINDOW_H)):
            store.mark(qid, "skipped")
            continue

        # медиа грузим ДО переписи — от этого зависит целевой объём и кап символов
        msg_ids = [int(i) for i in ids.split(",") if i]
        try:
            fetched = [m for m in await client.get_messages(src, ids=msg_ids) if m]
        except Exception as e:
            print(f"[media] {src}/{ids}: {e} — скип")
            store.mark(qid, "skipped")
            continue
        # страховка на бэклог: форварды чужих каналов не проводим даже из старой очереди
        if any(getattr(m, "fwd_from", None) is not None for m in fetched):
            store.mark(qid, "skipped")
            continue
        medias = [m for m in fetched if valid_media(m)]

        if not medias:
            store.mark(qid, "skipped")
            continue  # жёсткое правило: только посты с медиа (фото/видео/гиф/альбом)

        note, cap = volume_plan(text, has_media=bool(medias))

        draft = await rewrite(text, note, cap)
        if draft is None:  # модель сказала SKIP / не осилила
            store.mark(qid, "skipped")
            continue
        if len(draft) > cap:  # жёсткая обрезка по границе предложения, кап не рушимый
            draft = draft[:cap].rsplit(". ", 1)[0]
            if len(draft) > cap or len(draft.split()) < 8:
                store.mark(qid, "skipped")
                continue

        # --- соо 1: медиа из первоисточника + переписанный текст ---
        try:
            draft_md = md_safe(draft)
            await send_media_copy(client, DRAFT_TARGET, medias, draft_md)
        except Exception as e:
            print(f"[send1] {src}/{ids}: {e}")
            store.mark(qid, "skipped")
            continue

        # --- соо 2: форвард оригинала целиком ---
        try:
            await client.forward_messages(DRAFT_TARGET, msg_ids, src)
        except Exception as e:
            print(f"[send2] {src}/{ids}: {e}")

        store.mark(qid, "sent")
        store.remember(toks)
        print(f"[sent] {len(draft)} chars | src msgs: {ids}")
        return True
    return False


async def sender_loop():
    last_sent = 0.0
    while True:
        try:
            loop = asyncio.get_running_loop()
            now = loop.time()
            if in_window() and now - last_sent >= RATE:
                if await send_draft():
                    last_sent = now
        except Exception as e:
            print(f"[sender] {e}")
            await asyncio.sleep(30)
            continue
        await asyncio.sleep(15)


async def main():
    global DRAFT_TARGET
    await client.connect()
    if not await client.is_user_authorized():
        raise SystemExit(
            "Сессия не авторизована. Один раз запусти интерактивно:\n"
            "  docker compose run --rm -it app python -m app.bootstrap\n"
            "и введи код подтверждения Telegram."
        )
    print(f"[run] channels: {CHANNELS}")
    await build_allow(client)          # allowlist + авто-джойн каналов
    store.purge_not_in(ALLOW_BARE)     # чистим очередь от чужих источников
    # резолвим цель (группа по числовому ID, бот по @username или Saved Messages)
    DRAFT_TARGET = await resolve_target(client, RAW_TARGET)
    asyncio.create_task(backfill())
    asyncio.create_task(sender_loop())
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
