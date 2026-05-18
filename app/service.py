from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from app.clients import ExternalAPIError, KieClient, SalesBotClient, TelegramBotClient
from app.config import Settings
from app.models import AnalysisPayload, SessionStartRequest
from app.storage import AuditJobRecord, SQLiteRepository


FINISH_KEYWORDS = {"ГОТОВО", "ГОТОВ", "READY", "DONE"}


@dataclass
class EventResult:
    action: str
    schedule_processing: bool
    attachments_count: int


class AuditOrchestrator:
    def __init__(
        self,
        *,
        settings: Settings,
        repository: SQLiteRepository,
        kie_client: KieClient,
        salesbot_client: SalesBotClient,
        telegram_client: TelegramBotClient,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.kie_client = kie_client
        self.salesbot_client = salesbot_client
        self.telegram_client = telegram_client

    async def open_session(self, payload: SessionStartRequest) -> dict[str, Any]:
        session = self.repository.start_session(
            client_id=payload.client_id,
            project_id=payload.project_id,
            client_type=payload.client_type,
            client_name=payload.client_name,
            instagram_username=payload.instagram_username,
            state="waiting_screens",
        )
        await self._notify_admins(
            (
                "Новая сессия аудита открыта.\n"
                f"client_id: {session.client_id}\n"
                f"username: {session.instagram_username or '-'}\n"
                f"name: {session.client_name or '-'}"
            )
        )
        return {
            "status": "ok",
            "client_id": session.client_id,
            "state": session.state,
        }

    async def ingest_salesbot_event(self, payload: dict[str, Any]) -> EventResult:
        if str(payload.get("is_input", 0)) != "1":
            return EventResult("ignored_output", False, 0)
        client_info = payload.get("client") or {}
        client_id = str(client_info.get("id") or payload.get("client_id") or "").strip()
        if not client_id:
            return EventResult("ignored_missing_client", False, 0)
        session = self.repository.get_session_by_client_id(client_id)
        if not session:
            return EventResult("ignored_missing_session", False, 0)
        if session.state in {"processing", "image_pending", "completed", "failed"}:
            return EventResult("ignored_inactive_state", False, len(session.attachments))

        attachment_urls = extract_attachment_urls(payload.get("attachments"))
        if attachment_urls:
            session = self.repository.append_attachments(
                client_id,
                attachment_urls,
                max_images=self.settings.session_max_images,
            )

        message_text = str(payload.get("message") or "")
        if is_finish_message(message_text):
            if len(session.attachments) < self.settings.session_min_images:
                remaining = self.settings.session_min_images - len(session.attachments)
                await self.salesbot_client.send_callback(
                    client_id=client_id,
                    message=self.settings.salesbot_need_more_message,
                    extra_variables={
                        "screens_received": len(session.attachments),
                        "screens_required": self.settings.session_min_images,
                        "screens_remaining": remaining,
                    },
                )
                return EventResult("need_more_screens", False, len(session.attachments))
            self.repository.set_session_state(client_id, "processing")
            self.repository.create_job(session.id, "analyzing")
            await self._notify_admins(
                (
                    "Аудит отправлен в обработку.\n"
                    f"client_id: {client_id}\n"
                    f"screens: {len(session.attachments)}"
                )
            )
            return EventResult("processing_started", True, len(session.attachments))

        if attachment_urls:
            return EventResult("attachments_collected", False, len(session.attachments))
        return EventResult("message_ignored", False, len(session.attachments))

    async def process_session(self, client_id: str) -> None:
        session = self.repository.get_session_by_client_id(client_id)
        if not session or session.state != "processing":
            return
        job = self.repository.get_job_by_session_id(session.id)
        if not job:
            job = self.repository.create_job(session.id, "analyzing")

        try:
            analysis = await self._analyze_session(session.attachments)
            self.repository.save_analysis(job.id, analysis.model_dump())
            try:
                image_prompt = build_image_prompt(
                    analysis=analysis,
                    brand_name=self.settings.brand_name,
                )
                task_id = await self.kie_client.create_image_task(
                    prompt=image_prompt,
                    callback_url=self.settings.kie_callback_url(job.id),
                )
                self.repository.set_image_task(job.id, task_id)
                self.repository.set_session_state(client_id, "image_pending")
            except ExternalAPIError as exc:
                self.repository.complete_job(
                    job.id,
                    status="completed_text_only",
                    error=str(exc),
                )
                self.repository.set_session_state(client_id, "completed")
                await self._deliver_ready(client_id, analysis, image_url="")
                await self._notify_admins(
                    (
                        "Изображение не сгенерировалось на этапе createTask, "
                        "отправлен текстовый аудит.\n"
                        f"client_id: {client_id}\n"
                        f"error: {exc}"
                    )
                )
        except ExternalAPIError as exc:
            await self._mark_failed(
                client_id=client_id,
                job_id=job.id,
                internal_error=str(exc),
                user_error=(
                    "Не удалось завершить аудит прямо сейчас. "
                    "Пожалуйста, попробуйте отправить скриншоты еще раз чуть позже."
                ),
            )
        except Exception as exc:  # pragma: no cover - safety net for unexpected runtime failures
            await self._mark_failed(
                client_id=client_id,
                job_id=job.id,
                internal_error=f"Unexpected error: {exc}",
                user_error=(
                    "Произошла техническая ошибка при обработке аудита. "
                    "Пожалуйста, попробуйте еще раз позже."
                ),
            )

    async def handle_kie_callback(
        self,
        *,
        job_id: int | None,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        job = self.repository.get_job_by_id(job_id)
        task_id = extract_task_id(payload)
        if not job and task_id:
            job = self.repository.get_job_by_image_task_id(task_id)
        if not job:
            raise KeyError("Audit job not found")

        session = self.repository.get_session_by_id(job.session_id)
        if not session:
            raise KeyError("Session not found")

        state = extract_kie_state(payload)
        analysis = AnalysisPayload.model_validate(job.analysis or {})

        if state in {"waiting", "queuing", "generating", None}:
            return {"status": "ignored", "state": state or "unknown"}

        if state == "success":
            try:
                image_url = await self.kie_client.resolve_image_url(
                    task_id=task_id or str(job.image_task_id),
                    callback_payload=payload,
                )
            except ExternalAPIError as exc:
                self.repository.complete_job(
                    job.id,
                    status="completed_text_only",
                    error=str(exc),
                )
                self.repository.set_session_state(session.client_id, "completed")
                await self._deliver_ready(session.client_id, analysis, image_url="")
                await self._notify_admins(
                    (
                        "KIE прислал success, но URL результата не удалось получить. "
                        "Отправлен текстовый аудит.\n"
                        f"client_id: {session.client_id}\n"
                        f"error: {exc}"
                    )
                )
                return {"status": "text_only", "error": str(exc)}
            self.repository.complete_job(job.id, status="completed", image_url=image_url)
            self.repository.set_session_state(session.client_id, "completed")
            await self._deliver_ready(session.client_id, analysis, image_url=image_url)
            await self._notify_admins(
                (
                    "Аудит завершен успешно.\n"
                    f"client_id: {session.client_id}\n"
                    f"score: {analysis.overall_score}"
                )
            )
            return {"status": "ok", "image_url": image_url}

        error_message = extract_kie_error(payload) or "KIE image generation failed"
        self.repository.complete_job(job.id, status="completed_text_only", error=error_message)
        self.repository.set_session_state(session.client_id, "completed")
        await self._deliver_ready(session.client_id, analysis, image_url="")
        await self._notify_admins(
            (
                "Изображение не сгенерировалось, отправлен текстовый аудит.\n"
                f"client_id: {session.client_id}\n"
                f"error: {error_message}"
            )
        )
        return {"status": "text_only", "error": error_message}

    async def handle_telegram_update(self, payload: dict[str, Any]) -> dict[str, Any]:
        message = payload.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id") or "").strip()
        text = str(message.get("text") or "").strip()
        username = message.get("from", {}).get("username")
        if chat_id and text == "/start":
            self.repository.add_admin(chat_id=chat_id, username=username)
            await self.telegram_client.send_message(
                chat_id=chat_id,
                text="Админ-уведомления для Instagram Audit Bot подключены.",
            )
            return {"status": "registered", "chat_id": chat_id}
        return {"status": "ignored"}

    async def _analyze_session(self, image_urls: list[str]) -> AnalysisPayload:
        prompt = build_analysis_prompt(
            min_images=self.settings.session_min_images,
            max_images=self.settings.session_max_images,
        )
        raw_content = await self.kie_client.analyze_profile(image_urls=image_urls, prompt=prompt)
        cleaned = extract_json_document(raw_content)
        return AnalysisPayload.model_validate_json(cleaned)

    async def _deliver_ready(
        self,
        client_id: str,
        analysis: AnalysisPayload,
        *,
        image_url: str,
    ) -> None:
        await self.salesbot_client.send_callback(
            client_id=client_id,
            message=self.settings.salesbot_ready_message,
            extra_variables={
                "audit_text": analysis.dm_audit_text,
                "audit_image_url": image_url,
                "overall_score": analysis.overall_score,
                "niche_guess": analysis.niche_guess,
                "strengths_json": json.dumps(analysis.strengths, ensure_ascii=False),
                "problems_json": json.dumps(analysis.problems, ensure_ascii=False),
                "quick_wins_json": json.dumps(analysis.quick_wins, ensure_ascii=False),
            },
        )

    async def _mark_failed(
        self,
        *,
        client_id: str,
        job_id: int,
        internal_error: str,
        user_error: str,
    ) -> None:
        self.repository.complete_job(job_id, status="failed", error=internal_error)
        self.repository.set_session_state(client_id, "failed", last_error=internal_error)
        await self.salesbot_client.send_callback(
            client_id=client_id,
            message=self.settings.salesbot_fail_message,
            extra_variables={"audit_error": user_error},
        )
        await self._notify_admins(
            (
                "Аудит завершился ошибкой.\n"
                f"client_id: {client_id}\n"
                f"error: {internal_error}"
            )
        )

    async def _notify_admins(self, text: str) -> None:
        admins = [admin.chat_id for admin in self.repository.list_admins()]
        if not admins:
            return
        await self.telegram_client.broadcast(admins=admins, text=text)


def build_analysis_prompt(*, min_images: int, max_images: int) -> str:
    return f"""
Ты — эксперт по аудиту Instagram-профилей и конверсии.
Проанализируй присланные скриншоты профиля Instagram и верни ТОЛЬКО JSON без markdown и без пояснений.

Правила:
- Ответ строго на русском языке.
- Скриншотов может быть от {min_images} до {max_images}.
- Не выдумывай факты, которых не видно на скриншотах.
- Оцени профиль как маркетолог и как контент-стратег.
- Учитывай: аватар, имя, ник, био, закрепы, визуал ленты, оффер, понятность позиционирования, доверие, призыв к действию.

Требуемый JSON-формат:
{{
  "overall_score": 0,
  "niche_guess": "строка",
  "strengths": ["строка", "строка"],
  "problems": ["строка", "строка", "строка"],
  "quick_wins": ["строка", "строка", "строка"],
  "dm_audit_text": "Короткий, дружелюбный аудит в Direct. 700-1200 символов. Без markdown-таблиц.",
  "image_brief": "Краткий бриф для генерации одной визуальной summary-card по этому аудиту"
}}

Требования к полям:
- overall_score: целое число от 0 до 100
- strengths: 2-5 пунктов
- problems: 3-6 пунктов
- quick_wins: 3-5 конкретных улучшений, которые можно внедрить быстро
- dm_audit_text: короткое структурное сообщение для клиента, с сильными сторонами, проблемами и быстрыми рекомендациями
- image_brief: 1-3 предложения, что важно показать на итоговой карточке
""".strip()


def build_image_prompt(*, analysis: AnalysisPayload, brand_name: str) -> str:
    strengths = "; ".join(analysis.strengths[:3])
    quick_wins = "; ".join(analysis.quick_wins[:3])
    return (
        f"Create one polished Instagram profile audit summary visual for brand {brand_name}. "
        f"Style: modern social media consultant, premium but friendly, clean layout, warm light background, "
        f"subtle phone and interface motifs, high contrast accents, no dense paragraphs, minimal decorative text, "
        f"focus on the feeling of clarity and growth. "
        f"The profile score is {analysis.overall_score}/100. "
        f"Niche guess: {analysis.niche_guess}. "
        f"Main strengths: {strengths}. "
        f"Quick wins: {quick_wins}. "
        f"Image brief: {analysis.image_brief}."
    )


def extract_attachment_urls(attachments: Any) -> list[str]:
    parsed = attachments
    if isinstance(attachments, str):
        stripped = attachments.strip()
        if not stripped:
            return []
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = [stripped]

    urls: list[str] = []
    if isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, str) and item.startswith(("http://", "https://")):
                urls.append(item)
            elif isinstance(item, dict):
                for key in ("url", "file", "href", "link"):
                    value = item.get(key)
                    if isinstance(value, str) and value.startswith(("http://", "https://")):
                        urls.append(value)
                        break
    elif isinstance(parsed, dict):
        for key in ("url", "file", "href", "link"):
            value = parsed.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                urls.append(value)
    return urls


def is_finish_message(message: str) -> bool:
    normalized = re.sub(r"\s+", " ", message or "").strip().upper()
    return normalized in FINISH_KEYWORDS


def extract_json_document(raw_content: str) -> str:
    content = raw_content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?", "", content).strip()
        content = re.sub(r"```$", "", content).strip()
    start = content.find("{")
    end = content.rfind("}")
    if start != -1 and end != -1 and end > start:
        return content[start : end + 1]
    return content


def extract_task_id(payload: dict[str, Any]) -> str | None:
    data = payload.get("data", payload)
    for key in ("taskId", "task_id"):
        value = data.get(key)
        if value:
            return str(value)
    return None


def extract_kie_state(payload: dict[str, Any]) -> str | None:
    data = payload.get("data", payload)
    value = data.get("state")
    return str(value) if value else None


def extract_kie_error(payload: dict[str, Any]) -> str | None:
    data = payload.get("data", payload)
    for key in ("failMsg", "fail_msg", "msg", "error"):
        value = data.get(key) or payload.get(key)
        if value:
            return str(value)
    return None
