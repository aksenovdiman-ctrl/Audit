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
                f"{format_session_identity(session)}"
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
        session = self.repository.update_session_identity(
            client_id,
            client_name=extract_client_name(payload),
            instagram_username=extract_instagram_username(payload),
        )
        if session.state in {"processing", "image_pending", "completed", "failed"}:
            return EventResult("ignored_inactive_state", False, len(session.attachments))

        raw_attachments = payload.get("attachments")
        attachment_urls = extract_attachment_urls(raw_attachments)
        if attachment_urls:
            session = self.repository.append_attachments(
                client_id,
                attachment_urls,
                max_images=self.settings.session_max_images,
            )

        message_text = str(payload.get("message") or "")
        await self._notify_admins(
            (
                "SalesBot event получен.\n"
                f"{format_session_identity(session)}\n"
                f"state: {session.state}\n"
                f"message: {message_text[:120] or '-'}\n"
                f"attachments_parsed: {len(attachment_urls)}\n"
                f"session_screens_total: {len(session.attachments)}\n"
                f"attachments_raw: {format_debug_value(raw_attachments)}"
            )
        )
        if is_finish_message(message_text):
            if len(session.attachments) < self.settings.session_min_images:
                remaining = self.settings.session_min_images - len(session.attachments)
                await self._notify_admins(
                    (
                        "Недостаточно скриншотов для запуска аудита.\n"
                        f"{format_session_identity(session)}\n"
                        f"received: {len(session.attachments)}\n"
                        f"required: {self.settings.session_min_images}\n"
                        f"remaining: {remaining}"
                    )
                )
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
                    f"{format_session_identity(session)}\n"
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
            await self._notify_admins(
                (
                    "GPT-5.2 анализ завершен.\n"
                    f"{format_session_identity(session)}\n"
                    f"score: {analysis.overall_score}\n"
                    f"niche: {analysis.niche_guess}"
                )
            )
            try:
                image_input_urls = build_image_input_urls(
                    screenshot_urls=session.attachments,
                    style_reference_url=self.settings.style_reference_image_url,
                )
                image_prompt = build_image_prompt(
                    analysis=analysis,
                    brand_name=self.settings.brand_name,
                )
                task_id = await self.kie_client.create_image_to_image_task(
                    prompt=image_prompt,
                    callback_url=self.settings.kie_callback_url(job.id),
                    input_urls=image_input_urls,
                    aspect_ratio="3:4",
                )
                self.repository.set_image_task(job.id, task_id)
                self.repository.set_session_state(client_id, "image_pending")
                await self._notify_admins(
                    (
                        "GPT Image 2 createTask отправлен.\n"
                        f"{format_session_identity(session)}\n"
                        f"task_id: {task_id}"
                    )
                )
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
                        f"{format_session_identity(session)}\n"
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
                        f"{format_session_identity(session)}\n"
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
                    f"{format_session_identity(session)}\n"
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
                f"{format_session_identity(session)}\n"
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
        try:
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
        except ExternalAPIError as exc:
            await self._notify_admins(
                (
                    "Не удалось отправить результат обратно в SalesBot.\n"
                    f"client_id: {client_id}\n"
                    f"error: {exc}"
                )
            )
            raise

    async def _mark_failed(
        self,
        *,
        client_id: str,
        job_id: int,
        internal_error: str,
        user_error: str,
    ) -> None:
        self.repository.complete_job(job_id, status="failed", error=internal_error)
        session = self.repository.set_session_state(client_id, "failed", last_error=internal_error)
        await self.salesbot_client.send_callback(
            client_id=client_id,
            message=self.settings.salesbot_fail_message,
            extra_variables={"audit_error": user_error},
        )
        await self._notify_admins(
            (
                "Аудит завершился ошибкой.\n"
                f"{format_session_identity(session)}\n"
                f"error: {internal_error}"
            )
        )

    async def _notify_admins(self, text: str) -> None:
        admins = [admin.chat_id for admin in self.repository.list_admins()]
        if not admins:
            return
        await self.telegram_client.broadcast(admins=admins, text=text)


def build_analysis_prompt(*, min_images: int, max_images: int) -> str:
    screenshots_line = (
        f"- Скриншотов будет ровно {min_images}."
        if min_images == max_images
        else f"- Скриншотов может быть от {min_images} до {max_images}."
    )
    return f"""
Ты — эксперт по аудиту Instagram-профилей и конверсии.
Проанализируй присланные скриншоты профиля Instagram и верни ТОЛЬКО JSON без markdown и без пояснений.

Правила:
- Ответ строго на русском языке.
{screenshots_line}
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
    problems = "; ".join(analysis.problems[:4])
    quick_wins = "; ".join(analysis.quick_wins[:4])
    return f"""
Создай изображение в формате 3:4 — handwritten аудит Instagram-профиля в стиле Pinterest bullet journal.

ВАЖНО:
в генерацию будут загружены 3 изображения:

1 и 2 изображения — скрины Instagram-профиля.
Используй из них:
— аватарку
— ник
— имя
— описание
— статистику
— хайлайтсы
— посты
— визуал ленты
— оформление профиля
— контент профиля.

3 изображение — референс стиля.
Используй ТОЛЬКО его стиль:
— оформление страницы
— bullet journal aesthetic
— handwritten-подачу
— doodles
— текстуры
— расположение блоков
— цвета
— маркеры
— стикеры
— стиль заполнения.

Не копируй контент с 3 изображения.
Только визуальный стиль.

Сделай итог как полноценную handwritten-страницу маркетингового разбора профиля для бренда {brand_name}.

Стиль:
живой Pinterest planner / handwritten notebook / aesthetic journal.

Белая бумага в клетку.
Много ручных заметок.
Текстовыделители.
Маркеры.
Чекбоксы.
Doodles.
Стрелки.
Небольшие стикеры.
Эффект реально заполненного вручную блокнота.

Цвета:
розовый,
голубой,
салатовый,
жёлтый,
оранжевый,
сиреневый.

Наверху:
большой handwritten заголовок:

АУДИТ INSTAGRAM-ПРОФИЛЯ

Ниже:
сделай handwritten-блок профиля на основе 1 и 2 изображений:
— аватарка
— ник
— имя
— статистика
— описание
— ссылка
— хайлайтсы
— мини-превью постов.

Добавь МНОГО handwritten-анализа по профилю.

Блоки:

СИЛЬНЫЕ СТОРОНЫ
✓ визуал
✓ экспертность
✓ контент
✓ доверие
✓ личный бренд
✓ позиционирование

ТОЧКИ РОСТА
— где теряются заявки
— что выглядит слабо
— что мешает продажам
— чего не хватает в упаковке
— слабые места контента

КРИТИЧНЫЕ ОШИБКИ
— слабый оффер
— нет CTA
— мало кейсов
— нет прогревов
— нет воронки
— профиль не удерживает аудиторию

ЧТО УСИЛИТ ПРОДАЖИ
□ добавить кейсы
□ усилить stories
□ внедрить Telegram
□ делать больше Reels
□ усилить прогрев
□ добавить CTA
□ усилить личный бренд

КОНТЕНТ
— Reels
— Stories
— экспертный контент
— вовлекающий контент
— продающий контент
— прогревы

ВОРОНКА
REELS → STORIES → DIRECT → TELEGRAM → ПРОДАЖА

Добавь:
— handwritten-комментарии на полях
— выделения маркером
— doodles
— звездочки
— кружочки
— стрелки
— мини-графики
— иконки денег, охватов и лайков

Главное:
итог должен выглядеть как реальная handwritten-страница разбора профиля от сильного маркетолога, а не как шаблонный digital-дизайн.

Дополнительные ориентиры по анализу профиля:
— Общая оценка: {analysis.overall_score}/100
— Предполагаемая ниша: {analysis.niche_guess}
— Сильные стороны: {strengths}
— Точки роста и слабые места: {problems}
— Что усилит продажи в первую очередь: {quick_wins}
— Дополнительный бриф: {analysis.image_brief}
""".strip()


def build_image_input_urls(
    *,
    screenshot_urls: list[str],
    style_reference_url: str,
) -> list[str]:
    urls = list(screenshot_urls[:2])
    if style_reference_url:
        urls.append(style_reference_url)
    return urls


def extract_attachment_urls(attachments: Any) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()

    def add_url(value: str) -> None:
        cleaned = value.strip()
        if cleaned.startswith(("http://", "https://")) and cleaned not in seen:
            seen.add(cleaned)
            urls.append(cleaned)

    def walk(node: Any) -> None:
        if node is None:
            return
        if isinstance(node, str):
            stripped = node.strip()
            if not stripped:
                return
            if stripped.startswith(("http://", "https://")):
                add_url(stripped)
                return
            try:
                decoded = json.loads(stripped)
            except json.JSONDecodeError:
                return
            walk(decoded)
            return
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if isinstance(node, dict):
            for key in (
                "url",
                "file",
                "href",
                "link",
                "src",
                "downloadUrl",
                "download_url",
                "imageUrl",
                "image_url",
                "preview",
                "preview_url",
                "asset_url",
            ):
                value = node.get(key)
                if isinstance(value, str):
                    add_url(value)
            for value in node.values():
                walk(value)

    walk(attachments)
    return urls


def format_debug_value(value: Any, *, max_length: int = 400) -> str:
    if value in (None, "", [], {}):
        return "-"
    try:
        rendered = json.dumps(value, ensure_ascii=False)
    except TypeError:
        rendered = str(value)
    if len(rendered) <= max_length:
        return rendered
    return f"{rendered[:max_length]}..."


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


def extract_client_name(payload: dict[str, Any]) -> str | None:
    client = payload.get("client") or {}
    for key in ("name", "client_name", "full_name"):
        value = client.get(key) or payload.get(key)
        if value:
            return str(value)
    return None


def extract_instagram_username(payload: dict[str, Any]) -> str | None:
    client = payload.get("client") or {}
    for key in ("instagram_username", "login", "username", "nick", "nickname"):
        value = client.get(key) or payload.get(key)
        if value:
            return str(value)
    return None


def format_session_identity(session: Any) -> str:
    return (
        f"client_id: {session.client_id}\n"
        f"username: {session.instagram_username or '-'}\n"
        f"name: {session.client_name or '-'}"
    )
