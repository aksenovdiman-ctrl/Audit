from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import AnalysisPayload
from app.storage import SQLiteRepository


class FakeKieClient:
    def __init__(self) -> None:
        self.created_prompts: list[str] = []
        self.created_input_urls: list[list[str]] = []
        self.analysis_calls: list[list[str]] = []
        self.image_task_id = "task_gptimage_123"
        self.force_image_failure = False

    async def analyze_profile(self, image_urls: list[str], prompt: str) -> str:
        self.analysis_calls.append(image_urls)
        return (
            '{"overall_score": 82, "niche_guess": "экспертный блог", '
            '"strengths": ["Понятный визуал", "Хороший оффер"], '
            '"problems": ["Слабый CTA", "Мало доверительных триггеров", "Неочевидна польза закрепов"], '
            '"quick_wins": ["Переписать био", "Добавить CTA", "Обновить закрепы"], '
            '"dm_audit_text": "Профиль выглядит аккуратно, но пока не дожимает до заявки. '
            'Сильная сторона — визуальная целостность и понятное позиционирование. '
            'Я бы в первую очередь усилил био, добавил конкретный CTA и сделал закрепы более продающими.", '
            '"image_brief": "Показать аккуратный аудит профиля с ощущением роста и ясности"}'
        )

    async def create_image_task(self, prompt: str, callback_url: str) -> str:
        self.created_prompts.append(prompt)
        return self.image_task_id

    async def create_image_to_image_task(
        self,
        *,
        prompt: str,
        callback_url: str,
        input_urls: list[str],
        aspect_ratio: str = "3:4",
    ) -> str:
        self.created_prompts.append(prompt)
        self.created_input_urls.append(input_urls)
        return self.image_task_id

    async def resolve_image_url(self, *, task_id: str, callback_payload: dict | None = None) -> str:
        if self.force_image_failure:
            raise RuntimeError("no image")
        return "https://cdn.example.com/audit-summary.png"

    async def aclose(self) -> None:
        return None


class FakeSalesBotClient:
    def __init__(self) -> None:
        self.callbacks: list[dict] = []

    async def send_callback(self, *, client_id: str, message: str, extra_variables: dict | None = None):
        payload = {
            "client_id": client_id,
            "message": message,
            "extra_variables": extra_variables or {},
        }
        self.callbacks.append(payload)
        return payload

    async def aclose(self) -> None:
        return None


class FakeTelegramClient:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send_message(self, *, chat_id: str, text: str):
        payload = {"chat_id": chat_id, "text": text}
        self.messages.append(payload)
        return payload

    async def broadcast(self, admins, text: str):
        for chat_id in admins:
            await self.send_message(chat_id=chat_id, text=text)

    async def aclose(self) -> None:
        return None


def build_settings(tmp_path: Path) -> Settings:
    return Settings(
        public_base_url="https://audit.example.com",
        database_path=tmp_path / "audit_bot.sqlite3",
        style_reference_image_url="https://cdn.example.com/style-reference.png",
        kie_api_key="kie-key",
        kie_api_base_url="https://api.kie.ai",
        kie_file_upload_base_url="https://kieai.redpandaai.co",
        kie_callback_token="kie-token",
        kie_reasoning_effort="high",
        use_direct_attachment_urls=True,
        salesbot_api_key="salesbot-key",
        salesbot_api_base_url="https://chatter.salebot.ai/api",
        salesbot_webhook_token="salesbot-token",
        salesbot_ready_message="audit_ready",
        salesbot_fail_message="audit_failed",
        salesbot_need_more_message="audit_need_more_screens",
        telegram_bot_token="telegram-token",
        telegram_webhook_token="telegram-hook",
        brand_name="audit_inst_bot",
        session_min_images=2,
        session_max_images=2,
        http_timeout_seconds=5.0,
    )


def build_client(tmp_path: Path):
    settings = build_settings(tmp_path)
    repository = SQLiteRepository(settings.database_path)
    kie = FakeKieClient()
    salesbot = FakeSalesBotClient()
    telegram = FakeTelegramClient()
    app = create_app(
        settings=settings,
        repository=repository,
        kie_client=kie,
        salesbot_client=salesbot,
        telegram_client=telegram,
    )
    return TestClient(app), kie, salesbot, telegram


def salesbot_payload(*, client_id: str, message: str, attachments: list[str]):
    return {
        "client": {"id": client_id, "name": "Alice", "client_type": "instagram"},
        "message": message,
        "attachments": attachments,
        "is_input": 1,
    }


def test_happy_path_with_callback_delivery(tmp_path: Path):
    client, kie, salesbot, telegram = build_client(tmp_path)
    with client:
        client.post(
            "/telegram/webhook",
            params={"token": "telegram-hook"},
            json={"message": {"chat": {"id": "1001"}, "from": {"username": "admin"}, "text": "/start"}},
        )
        response = client.post(
            "/salesbot/session/start",
            json={
                "client_id": "42",
                "project_id": "project-1",
                "client_type": "instagram",
                "client_name": "Alice",
                "instagram_username": "alice_blog",
            },
        )
        assert response.status_code == 200

        response = client.post(
            "/salesbot/events",
            params={"token": "salesbot-token"},
            json=salesbot_payload(
                client_id="42",
                message="screens",
                attachments=[
                    "https://example.com/1.png",
                    "https://example.com/2.png",
                ],
            ),
        )
        assert response.json()["action"] == "attachments_collected"

        response = client.post(
            "/salesbot/events",
            params={"token": "salesbot-token"},
            json=salesbot_payload(client_id="42", message="ГОТОВО", attachments=[]),
        )
        assert response.status_code == 200
        assert response.json()["action"] == "processing_started"
        assert kie.analysis_calls == [["https://example.com/1.png", "https://example.com/2.png"]]
        assert len(kie.created_prompts) == 1
        assert kie.created_input_urls == [[
            "https://example.com/1.png",
            "https://example.com/2.png",
            "https://cdn.example.com/style-reference.png",
        ]]
        assert "АУДИТ INSTAGRAM-ПРОФИЛЯ" in kie.created_prompts[0]

        callback_response = client.post(
            "/kie/callback",
            params={"token": "kie-token", "job_id": 1},
            json={"data": {"taskId": "task_gptimage_123", "state": "success"}},
        )
        assert callback_response.status_code == 200
        assert salesbot.callbacks[-1]["message"] == "audit_ready"
        assert salesbot.callbacks[-1]["extra_variables"]["audit_image_url"] == "https://cdn.example.com/audit-summary.png"
        assert telegram.messages[-1]["text"].startswith("Аудит завершен успешно.")


def test_session_start_accepts_json_string_payload(tmp_path: Path):
    client, _, _, telegram = build_client(tmp_path)
    with client:
        client.post(
            "/telegram/webhook",
            params={"token": "telegram-hook"},
            json={"message": {"chat": {"id": "1001"}, "from": {"username": "admin"}, "text": "/start"}},
        )
        response = client.post(
            "/salesbot/session/start",
            json='{\n  "client_id": "947100401"\n}',
        )
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert response.json()["client_id"] == "947100401"
        assert response.json()["state"] == "waiting_screens"
        assert telegram.messages[-1]["text"].startswith("Новая сессия аудита открыта.")


def test_need_more_screens_callback(tmp_path: Path):
    client, _, salesbot, _ = build_client(tmp_path)
    with client:
        client.post(
            "/salesbot/session/start",
            json={"client_id": "42", "project_id": "project-1", "client_type": "instagram"},
        )
        response = client.post(
            "/salesbot/events",
            params={"token": "salesbot-token"},
            json=salesbot_payload(
                client_id="42",
                message="ГОТОВО",
                attachments=["https://example.com/1.png"],
            ),
        )
        assert response.status_code == 200
        assert response.json()["action"] == "need_more_screens"
        assert salesbot.callbacks[-1]["message"] == "audit_need_more_screens"
        assert salesbot.callbacks[-1]["extra_variables"]["screens_remaining"] == 1


def test_attachment_dedup_and_cap(tmp_path: Path):
    client, _, _, _ = build_client(tmp_path)
    with client:
        client.post(
            "/salesbot/session/start",
            json={"client_id": "42", "project_id": "project-1", "client_type": "instagram"},
        )
        client.post(
            "/salesbot/events",
            params={"token": "salesbot-token"},
            json=salesbot_payload(
                client_id="42",
                message="batch-1",
                attachments=[
                    "https://example.com/1.png",
                    "https://example.com/2.png",
                    "https://example.com/2.png",
                ],
            ),
        )
        response = client.post(
            "/salesbot/events",
            params={"token": "salesbot-token"},
            json=salesbot_payload(
                client_id="42",
                message="batch-2",
                attachments=[
                    "https://example.com/3.png",
                    "https://example.com/4.png",
                ],
            ),
        )
        assert response.json()["attachments_count"] == 2


def test_invalid_tokens_are_rejected(tmp_path: Path):
    client, _, _, _ = build_client(tmp_path)
    with client:
        response = client.post("/salesbot/events", params={"token": "bad"}, json={})
        assert response.status_code == 403

        response = client.post("/kie/callback", params={"token": "bad"}, json={})
        assert response.status_code == 403

        response = client.post("/telegram/webhook", params={"token": "bad"}, json={})
        assert response.status_code == 403


def test_text_only_delivery_if_image_callback_fails(tmp_path: Path):
    client, _, salesbot, _ = build_client(tmp_path)
    with client:
        client.post(
            "/salesbot/session/start",
            json={"client_id": "42", "project_id": "project-1", "client_type": "instagram"},
        )
        client.post(
            "/salesbot/events",
            params={"token": "salesbot-token"},
            json=salesbot_payload(
                client_id="42",
                message="screens",
                attachments=[
                    "https://example.com/1.png",
                    "https://example.com/2.png",
                ],
            ),
        )
        client.post(
            "/salesbot/events",
            params={"token": "salesbot-token"},
            json=salesbot_payload(client_id="42", message="ГОТОВО", attachments=[]),
        )
        response = client.post(
            "/kie/callback",
            params={"token": "kie-token", "job_id": 1},
            json={"data": {"taskId": "task_gptimage_123", "state": "fail", "failMsg": "render failed"}},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "text_only"
        assert salesbot.callbacks[-1]["message"] == "audit_ready"
        assert salesbot.callbacks[-1]["extra_variables"]["audit_image_url"] == ""
