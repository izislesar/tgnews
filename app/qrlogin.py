"""QR-логин для юзербота на отдельном аккаунте (виртуальный номер, без SMS).

Запуск:
    docker compose run --rm -it -e SESSION_PATH=/data/session_vbot app python -m app.qrlogin

Понадобится телефон, где УЖЕ залогинен нужный аккаунт: сканируешь QR,
подтверждаешь вход — сессия сохраняется на сервере."""
import asyncio
import getpass
import os

import qrcode
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

API_ID = int(os.environ.get("API_ID", "2040"))
API_HASH = os.environ.get("API_HASH", "b18441a1ff607e10a989891a5462e627")
SESSION = os.getenv("SESSION_PATH", "/data/session_vbot")

client = TelegramClient(SESSION, API_ID, API_HASH)


async def go():
    await client.connect()
    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"Уже авторизован: {me.first_name} (@{me.username})")
        return

    for attempt in range(1, 11):
        qr = await client.qr_login()
        print(f"\n=== Попытка {attempt}: отсканируй QR телефоном, где залогинен ЦЕЛЕВОЙ аккаунт ===")
        print(f"(url: {qr.url})\n")
        img = qrcode.QRCode(border=2)
        img.add_data(qr.url)
        img.print_ascii(invert=True)
        print("\nTelegram -> Настройки -> Devices -> Link Desktop Device -> сканируй это\n")
        try:
            await qr.wait(timeout=60)
            me = await client.get_me()
            print(f"\n✅ OK! Юзербот авторизован как: {me.first_name} (@{me.username}) id={me.id}")
            print(f"Сессия сохранена в {SESSION}")
            print("Дальше: пропиши SESSION_PATH=" + SESSION + " в .env и docker compose up -d --force-recreate")
            return
        except asyncio.TimeoutError:
            print("QR истёк, генерирую новый...")
        except SessionPasswordNeededError:
            pwd = getpass.getpass("У аккаунта стоит облачный пароль (2FA). Введи его: ")
            await qr.wait(password=pwd)
            me = await client.get_me()
            print(f"\n✅ OK (2FA пройден): {me.first_name} (@{me.username})")
            return

    raise SystemExit("10 попыток исчерпаны — QR так и не отсканирован.")


if __name__ == "__main__":
    asyncio.run(go())
