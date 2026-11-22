"""Точка входа: юзербот-коллектор + бот-интерфейс + фоновый фактчекинг."""

import asyncio

from .bot import AdminBot
from .factcheck.repo import FactcheckRepo
from .settings import Settings
from .store import Store
from .worker import Worker


def _admin_ids(raw: str) -> set[int]:
    return {int(x) for x in (raw or "").replace(" ", "").split(",") if x}


async def main():
    settings = Settings.load()
    store = Store(settings.db_path)

    fc_repo = FactcheckRepo(store.con, store.lock) if settings.fc_mode != "off" else None
    if fc_repo is not None:
        print(f"[fc] режим: {settings.fc_mode}")

    worker = Worker(
        store,
        settings.session_path,
        settings.api_id,
        settings.api_hash,
        settings.bot_username,
        fc_repo=fc_repo,
        fc_mode=settings.fc_mode,
    )
    await worker.run()

    admin_ids = _admin_ids(settings.admin_ids)
    if not admin_ids:
        raise SystemExit("ADMIN_IDS пуст — перечисли telegram-айди админов через запятую")

    app = AdminBot(store, worker, admin_ids, settings=settings, fc_repo=fc_repo)

    if fc_repo is not None:
        from .factcheck.factory import build_service

        service, _llm = build_service(settings, fc_repo)
        app.fc_service = service
        asyncio.create_task(
            service.run_forever(on_done=app.note_verdict), name="factcheck"
        )
        print("[fc] фоновый воркер запущен")

    print(f"[run] admins: {admin_ids}")
    await app.run()  # polling блокирует — норм для одного процесса


if __name__ == "__main__":
    asyncio.run(main())
