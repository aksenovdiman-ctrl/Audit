# Instagram Audit Bot

FastAPI-сервис, который связывает `SalesBot` + `KIE` + Telegram-админку для аудита Instagram-профилей по скриншотам.

## Что делает сервис

1. `SalesBot` открывает intake-сессию через `POST /salesbot/session/start`.
2. Пользователь присылает 3-6 скриншотов профиля в Instagram Direct.
3. `SalesBot` пересылает входящие сообщения на `POST /salesbot/events?token=...`.
4. После сообщения `ГОТОВО` сервис:
   - анализирует скриншоты через KIE `GPT-5.2`
   - получает структурный JSON-аудит на русском
   - запускает KIE `GPT Image 2` для одной summary-card
5. Когда KIE присылает callback, сервис отправляет обратно в SalesBot callback `audit_ready`.
6. SalesBot сам доставляет результат пользователю в Instagram.
7. Telegram-бот принимает `/start` и потом получает админ-уведомления о старте, успехе и ошибках.

## Структура callback-переменных для SalesBot

При успешной выдаче сервис отправляет callback с `message=audit_ready` и переменными:

- `audit_text`
- `audit_image_url`
- `overall_score`
- `niche_guess`
- `strengths_json`
- `problems_json`
- `quick_wins_json`

При нехватке скриншотов сервис отправляет `message=audit_need_more_screens` и:

- `screens_received`
- `screens_required`
- `screens_remaining`

При полной ошибке сервиса отправляется `message=audit_failed` и:

- `audit_error`

## Переменные окружения

Скопируйте `.env.example` в `.env` и заполните значения.

Критично:

- `PUBLIC_BASE_URL` должен быть публичным HTTPS-адресом
- `KIE_CALLBACK_TOKEN`, `SALESBOT_WEBHOOK_TOKEN`, `TELEGRAM_WEBHOOK_TOKEN` должны быть длинными случайными токенами
- ключи, которые ранее уже были опубликованы в переписке, перед запуском нужно обязательно ротировать

## Запуск локально

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
uvicorn app.asgi:app --host 0.0.0.0 --port 8000 --reload
```

## Настройка SalesBot

### 1. После проверки подписки

В блоке SalesBot отправьте пользователю сообщение:

`Пришлите 3-6 скриншотов профиля и затем сообщением ГОТОВО.`

В этом же блоке добавьте внешний `POST` на:

`https://YOUR-DOMAIN/salesbot/session/start`

JSON body:

```json
{
  "client_id": "#{client_id}",
  "project_id": "#{project_id}",
  "client_type": "#{client_type}",
  "client_name": "#{name}",
  "instagram_username": "#{login}"
}
```

### 2. Проектный webhook

Укажите project webhook в SalesBot:

`https://YOUR-DOMAIN/salesbot/events?token=YOUR_SALESBOT_WEBHOOK_TOKEN`

Сервис ожидает стандартный SalesBot payload с `client`, `message`, `attachments`, `is_input`.

### 3. Ветки callback воронки

Нужно создать три callback-ветки:

- `audit_ready`
- `audit_need_more_screens`
- `audit_failed`

В ветке `audit_ready` отправляйте:

- текст из `#{audit_text}`
- картинку по `#{audit_image_url}` если строка не пустая

## Настройка Telegram

Webhook Telegram:

`https://YOUR-DOMAIN/telegram/webhook?token=YOUR_TELEGRAM_WEBHOOK_TOKEN`

После этого отправьте боту `/start` из админского чата. Этот `chat_id` сохранится в SQLite, и дальше сервис начнет слать уведомления.

## Проверки

Основные тесты покрывают:

- happy path
- нехватку скриншотов
- дедупликацию и лимит в 6 файлов
- reject на неверные webhook token
- text-only fallback при неуспехе генерации картинки
