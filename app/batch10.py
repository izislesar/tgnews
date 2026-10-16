"""Одноразовый батч: обработать до 10 свежих кандидатов из очереди и отправить в группу."""
import asyncio

from .main import (
    client, RAW_TARGET, resolve_target, rewrite, store,
    norm_tokens, jaccard, md_safe, volume_plan, valid_media, send_media_copy,
    DUP_WINDOW_H, DUP_THRESHOLD,
)

N = 10


async def go():
    await client.connect()
    tgt = await resolve_target(client, RAW_TARGET)
    rows = store.pending()[:N]
    print(f"[batch] кандидатов взято: {len(rows)}\n")
    ok = 0
    for i, (src, ids, key, text, qid, ts) in enumerate(rows, 1):
        if store.too_old(ts):
            store.mark(qid, "skipped")
            print(f"--- #{i} stale, скип")
            continue
        toks = norm_tokens(text)
        if any(jaccard(t, toks) >= DUP_THRESHOLD for t in store.recent_sets(DUP_WINDOW_H)):
            store.mark(qid, "skipped")
            print(f"--- #{i} дубль, скип")
            continue

        from .main import valid_media
        msg_ids = [int(x) for x in ids.split(",") if x]
        try:
            fetched = [m for m in await client.get_messages(src, ids=msg_ids) if m]
        except Exception as e:
            print(f"    [media] {e}")
            continue
        if any(getattr(m, "fwd_from", None) is not None for m in fetched):
            store.mark(qid, "skipped")
            print(f"--- #{i} форвард чужого канала, скип")
            continue
        medias = [m for m in fetched if valid_media(m)]

        note, cap = volume_plan(text, has_media=bool(medias))

        if not medias:
            store.mark(qid, "skipped")
            print(f"--- #{i} без медиа, скип")
            print(f"    IN : {text[:100]}")
            continue
        draft = await rewrite(text, note, cap)
        if draft is None:
            store.mark(qid, "skipped")
            print(f"--- #{i} SKIP от модели")
            print(f"    IN : {text[:100]}")
            continue
        if len(draft) > cap:
            draft = draft[:cap].rsplit(". ", 1)[0]
            if len(draft) > cap or len(draft.split()) < 8:
                store.mark(qid, "skipped")
                print(f"--- #{i} не вписался в кап {cap} ({len(draft)} симв)")
                continue

        try:
            await send_media_copy(client, tgt, medias, md_safe(draft))
            sent_ok = True
        except Exception as e:
            print(f"--- #{i} НЕ ОТПРАВИЛОСЬ: {e}")
            continue

        try:
            await client.forward_messages(tgt, msg_ids, src)
        except Exception as e:
            print(f"    [fwd] форвард не удался (тип не пересиловый): {e}")

        store.mark(qid, "sent")
        store.remember(toks)
        ok += 1
        print(f"--- #{i} ✔ медиа-объектов: {len(medias)} | слов ориг: {len(text.split())} -> OUT: {len(draft.split())} слов / {len(draft)} сим")
        print(f"    IN : {text[:100]}")
        print(f"    OUT: {draft}")
        print()
    print(f"[batch] отправлено пар: {ok}/{len(rows)}")


if __name__ == "__main__":
    asyncio.run(go())
