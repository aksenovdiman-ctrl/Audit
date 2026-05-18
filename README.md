# Instagram Audit Bot

FastAPI-сервис, который связывает `SalesBot` + `KIE` + Telegram-админку для аудита Instagram-профилей по скриншотам.

## Что делает сервис

1. `SalesBot` открывает intake-сессию через `POST /salesbot/session/start`.
2. Пользователь присылает 2 скриншота профиля в Instagram Direct.
3. `SalesBot` может передавать входящие сообщения двумя способами:
   - project webhook на `POST /salesbot/events?token=...`
   - явный `POST /salesbot/session/input?token=...` из блока/состояния с переменной `#{attachments}`
4. После сообщения `ГОТОВО` сервис:
   - анализирует скриншоты через KIE `GPT-5.2`
   - получает структурный JSON-аудит на русском
   - запускает KIE `GPT Image 2 Image-to-Image` с 3 изображениями:
     1. первый скрин профиля
     2. второй скрин профиля
     3. style-reference URL из `STYLE_REFERENCE_IMAGE_URL`
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
- `STYLE_REFERENCE_IMAGE_URL` должен указывать на публично доступный style-reference image
- `KIE_CALLBACK_TOKEN`, `SALESBOT_WEBHOOK_TOKEN`, `TELEGRAM_WEBHOOK_TOKEN` должны быть длинными случайными токенами
- ключи, которые ранее уже были опубликованы в переписке, перед запуском нужно обязательно ротировать

В этом проекте можно хранить style-reference локально в `static/style-reference.jpeg` и отдавать его по адресу:

`https://YOUR-DOMAIN/static/style-reference.jpeg`

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

`Пришлите 2 скриншота профиля и затем сообщением ГОТОВО.`

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

### 3. Надежный сбор нескольких фото через `#{attachments}`

Если пользователи часто отправляют 2 скриншота одним сообщением, лучше передавать вложения в backend не только через project webhook, а отдельным API-запросом из SalesBot.

Используйте endpoint:

`https://YOUR-DOMAIN/salesbot/session/input?token=YOUR_SALESBOT_WEBHOOK_TOKEN`

JSON body:

```json
{
  "client_id": "#{client_id}",
  "message": "#{question}",
  "attachments": "#{attachments}",
  "attachment_url": "#{attachment_url}",
  "client_name": "#{name}",
  "instagram_username": "#{login}"
}
```

Где:

- `#{attachments}` — JSON-массив URL вложений пользователя из SalesBot
- `#{attachment_url}` — запасной одиночный URL, если платформа прислала только один файл

Рекомендуемая схема:

- пользователь доходит до блока с инструкцией
- на вложение/ответ пользователя SalesBot вызывает `POST /salesbot/session/input`
- когда пользователь отправляет `ГОТОВО`, SalesBot снова вызывает `POST /salesbot/session/input`

### 4. Ветки callback воронки

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
- дедупликацию и лимит в 2 файла
- reject на неверные webhook token
- text-only fallback при неуспехе генерации картинки
