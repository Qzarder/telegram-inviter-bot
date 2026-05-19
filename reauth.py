import asyncio
from telethon import TelegramClient
import config

async def reauthorize():
    phone = input("Введите номер телефона (с +): ")
    # Создаем новую сессию с твоими API_ID и API_HASH
    client = TelegramClient(f"sessions/{phone}", config.API_ID, config.API_HASH)
    
    await client.start(phone=lambda: phone)
    
    if await client.is_user_authorized():
        print(f"✅ Аккаунт {phone} успешно авторизован и сохранен в папку sessions!")
    else:
        print("❌ Ошибка авторизации.")
    
    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(reauthorize())