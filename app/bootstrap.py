"""Единственный интерактивный запуск: логин + поиск группы-цели + тест LLM."""
import asyncio
from telethon.errors import RPCError
from .main import client, CHANNELS, rewrite, RAW_TARGET, resolve_target


def _title(ent):
    return getattr(ent, "title", None) or getattr(ent, "username", None) or str(ent.id)


async def run():
    await client.start()  # если сессия уже валидна — НЕ спросит ничего
    me = await client.get_me()
    print(f"\nOK, авторизован как: {me.first_name} (@{me.username})")

    print("\nТвои группы/каналы (числовой ID -> название):")
    async for d in client.iter_dialogs():
        if d.is_group or d.is_channel:
            print(f"  {d.id:<15} -> {d.title}")

    try:
        dest = await resolve_target(client, RAW_TARGET)
        print(f"\nЦель черновиков: {_title(dest)} ({RAW_TARGET})")

        # тест, что в цель вообще можно писать
        probe = await client.send_message(
            dest, "✅ tgnews: тестовое сообщение пайплайна — можно удалять")
        print(f"Тестовое сообщение доставлено, msg_id={probe.id}")
    except Exception as e:
        dest = None
        print(f"\n!! Цель '{RAW_TARGET}' не резолвится: {e}")
        print("   Вставь числовой ID группы в .env -> DRAFT_TARGET и запусти bootstrap ещё раз.")
    except RPCError as e:
        print(f"!! Не смогла писать в цель: {e}")

    test = await rewrite(
        "President Trump regarding the midterms: It's going to all work out. "
        "You know why? It always does. — Axios | @TheWarReporter"
    )
    print(f"\nТест LLM: {test!r}")
    print("\nГотово. Дальше: docker compose up -d")


if __name__ == "__main__":
    asyncio.run(run())
