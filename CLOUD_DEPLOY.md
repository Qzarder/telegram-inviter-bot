# Запуск бота в облаке (без локального ПК)

Ниже самый простой и стабильный вариант: VPS + Docker.

## 1) Что нужно на сервере

- Linux VPS (Ubuntu 22.04/24.04)
- Docker + Docker Compose
- Твоя папка проекта `telegram-inviter-bot`

## 2) Установка Docker (один раз)

```bash
sudo apt update
sudo apt install -y docker.io docker-compose-plugin
sudo systemctl enable --now docker
```

## 3) Загрузка проекта

Скопируй на сервер всю папку проекта (включая `sessions/`, `.env`, `proxy_pool.json`, `targets.txt`, `channels.txt`).

Пример через `scp` с локального ПК:

```bash
scp -r "telegram-inviter-bot" user@SERVER_IP:/home/user/
```

На сервере:

```bash
cd /home/user/telegram-inviter-bot
```

## 4) Старт

```bash
docker compose up -d --build
```

Проверить логи:

```bash
docker compose logs -f --tail=200
```

Остановить:

```bash
docker compose down
```

Перезапуск после изменений:

```bash
docker compose up -d --build
```

## 5) Как это работает

- Контейнер перезапускается сам (`restart: unless-stopped`).
- Все данные остаются в твоей папке на сервере (`volumes: ./:/app`).
- `sessions`, прогресс прогрева, кулдауны и прочие файлы не пропадут после рестарта.

## 6) Важные советы

- Не запускай одновременно локальную копию и облачную на одних и тех же сессиях.
- Оставь открытым только один рабочий экземпляр бота.
- Для логов всегда смотри:

```bash
docker compose logs -f --tail=200
```
