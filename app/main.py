"""Точка входа: юзербот-коллектор + бот-интерфейс в одном процессе."""
import asyncio
import os

from .bot import AdminBot
from .store import Store
from .worker import Worker

DEFAULT_API_ID = 2040
DEFAULT_API_HASH = "b18441a1ff607e10a989891a5462e627"  # Telegram Desktop


async def main():
    store = Store("/data/queue.db")
    worker = Worker(
        store,
        os.getenv("SESSION_PATH", "/data/session"),
        int(os.environ.get("API_ID", DEFAULT_API_ID)),
        os.environ.get("API_HASH", DEFAULT_API_HASH),
        os.getenv("BOT_USERNAME", "lalsdjasdljkbot"),
    )
    await worker.run()

    admin_ids = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
    if not admin_ids:
        raise SystemExit("ADMIN_IDS пуст — перечисли telegram-айди админов через запятую")
    app = AdminBot(store, worker, admin_ids)

    print(f"[run] admins: {admin_ids}")
    await app.run()  # polling блокирует — норм для одного процесса


if __name__ == "__main__":
    asyncio.run(main())
