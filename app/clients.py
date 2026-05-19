from __future__ import annotations

import asyncio
import json
import mimetypes
from typing import Any, Iterable, Sequence
from urllib.parse import urlparse

import httpx

from app.config import Settings


class ExternalAPIError(RuntimeError):
    """Raised when an upstream API call fails."""


class KieClient:
    def __init__(self, settings: Settings, http_client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = http_client or httpx.AsyncClient(
            timeout=settings.http_timeout_seconds
        )

    async def analyze_profile(self, image_urls: Sequence[str], prompt: str) -> str:
        # Messenger/CDN signed URLs are unstable for upstream multimodal analysis too,
        # so normalize them through KIE file upload before calling GPT-5.2.
        uploaded_urls = await self._upload_remote_images(list(image_urls))
        last_error: ExternalAPIError | None = None
        reasoning_sequence = self._analysis_reasoning_sequence()
        for attempt, reasoning_effort in enumerate(reasoning_sequence, start=1):
            try:
                return await self._chat_completion(
                    prompt=prompt,
                    image_urls=uploaded_urls,
                    reasoning_effort=reasoning_effort,
                )
            except ExternalAPIError as exc:
                last_error = exc
                if not self._is_retryable_analysis_error(exc):
                    break
                if attempt < len(reasoning_sequence):
                    await asyncio.sleep(min(attempt, 2))
        if last_error:
            raise last_error
        raise ExternalAPIError("KIE analysis failed without a captured exception")

    async def create_image_task(self, prompt: str, callback_url: str) -> str:
        return await self._create_task(
            prompt=prompt,
            callback_url=callback_url,
            input_urls=None,
        )

    async def create_image_to_image_task(
        self,
        *,
        prompt: str,
        callback_url: str,
        input_urls: Sequence[str],
        aspect_ratio: str = "3:4",
    ) -> str:
        # GPT Image 2 rejects some signed messenger URLs directly, so always
        # upload generation inputs to KIE first and pass the stable download URLs.
        uploaded_urls = await self._upload_remote_images(list(input_urls))
        return await self._create_task(
            prompt=prompt,
            callback_url=callback_url,
            input_urls=uploaded_urls,
            aspect_ratio=aspect_ratio,
        )

    async def _create_task(
        self,
        *,
        prompt: str,
        callback_url: str,
        input_urls: Sequence[str] | None,
        aspect_ratio: str = "auto",
    ) -> str:
        payload = {
            "model": (
                "gpt-image-2-image-to-image"
                if input_urls
                else "gpt-image-2-text-to-image"
            ),
            "callBackUrl": callback_url,
            "input": {
                "prompt": prompt,
                "aspect_ratio": aspect_ratio,
            },
        }
        if input_urls:
            payload["input"]["input_urls"] = list(input_urls)
        response = await self._client.post(
            f"{self.settings.kie_api_base_url}/api/v1/jobs/createTask",
            headers=self._auth_headers(),
            json=payload,
        )
        payload = self._decode_json(response)
        if payload.get("code") not in (None, 0, 200) and not payload.get("data"):
            raise ExternalAPIError(
                f"KIE image task rejected ({payload.get('code')}): {payload.get('msg') or payload}"
            )
        try:
            return str(payload["data"]["taskId"])
        except (KeyError, TypeError) as exc:
            raise ExternalAPIError(f"Unexpected KIE image task response: {payload}") from exc

    async def resolve_image_url(
        self,
        *,
        task_id: str,
        callback_payload: dict[str, Any] | None = None,
    ) -> str:
        for payload in filter(None, [callback_payload, await self.get_task_details(task_id)]):
            result_url = self._extract_image_url(payload)
            if result_url:
                return result_url
        raise ExternalAPIError(f"No result image URL found for task_id={task_id}")

    async def get_task_details(self, task_id: str) -> dict[str, Any]:
        response = await self._client.get(
            f"{self.settings.kie_api_base_url}/api/v1/jobs/recordInfo",
            headers={"Authorization": f"Bearer {self.settings.kie_api_key}"},
            params={"taskId": task_id},
        )
        return self._decode_json(response)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _chat_completion(
        self,
        *,
        prompt: str,
        image_urls: Sequence[str],
        reasoning_effort: str,
    ) -> str:
        content = [{"type": "text", "text": prompt}]
        content.extend(
            {
                "type": "image_url",
                "image_url": {"url": url},
            }
            for url in image_urls
        )
        response = await self._client.post(
            f"{self.settings.kie_api_base_url}/gpt-5-2/v1/chat/completions",
            headers=self._auth_headers(),
            json={
                "messages": [{"role": "user", "content": content}],
                "reasoning_effort": reasoning_effort,
            },
        )
        payload = self._decode_json(response)
        try:
            return str(payload["choices"][0]["message"]["content"])
        except (IndexError, KeyError, TypeError) as exc:
            raise ExternalAPIError(f"Unexpected KIE analysis response: {payload}") from exc

    async def _upload_remote_images(self, image_urls: Sequence[str]) -> list[str]:
        uploaded_urls: list[str] = []
        for index, source_url in enumerate(image_urls, start=1):
            download = await self._client.get(source_url)
            download.raise_for_status()
            content_type = download.headers.get("content-type", "application/octet-stream")
            filename = self._guess_filename(
                source_url=source_url,
                index=index,
                content_type=content_type,
            )
            files = {"file": (filename, download.content, content_type)}
            response = await self._client.post(
                f"{self.settings.kie_file_upload_base_url}/api/file-stream-upload",
                headers={"Authorization": f"Bearer {self.settings.kie_api_key}"},
                data={
                    "uploadPath": "images/user-uploads",
                    "fileName": filename,
                },
                files=files,
            )
            payload = self._decode_json(response)
            try:
                uploaded_urls.append(str(payload["data"]["downloadUrl"]))
            except KeyError as exc:
                raise ExternalAPIError(f"Unexpected KIE file upload response: {payload}") from exc
        return uploaded_urls

    @staticmethod
    def _guess_filename(*, source_url: str, index: int, content_type: str) -> str:
        path_name = urlparse(source_url).path.rsplit("/", 1)[-1]
        if "." in path_name:
            return path_name
        extension = mimetypes.guess_extension(content_type.split(";", 1)[0].strip()) or ".png"
        return f"image-{index}{extension}"

    @staticmethod
    def _extract_image_url(payload: dict[str, Any]) -> str | None:
        data = payload.get("data", payload)
        result_json = data.get("resultJson") or data.get("result_json")
        if isinstance(result_json, str):
            try:
                result_json = json.loads(result_json)
            except json.JSONDecodeError:
                result_json = None
        if isinstance(result_json, dict):
            for key in ("resultUrls", "result_urls", "images"):
                value = result_json.get(key)
                result = KieClient._first_url(value)
                if result:
                    return result
        for key in ("resultUrls", "result_urls", "images"):
            result = KieClient._first_url(data.get(key))
            if result:
                return result
        return None

    @staticmethod
    def _first_url(value: Any) -> str | None:
        if isinstance(value, list) and value:
            first = value[0]
            if isinstance(first, str):
                return first
            if isinstance(first, dict):
                for key in ("url", "imageUrl", "image_url"):
                    if first.get(key):
                        return str(first[key])
        if isinstance(value, str):
            return value
        return None

    def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.kie_api_key}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _decode_json(response: httpx.Response) -> dict[str, Any]:
        try:
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ExternalAPIError(
                f"Upstream request failed ({response.status_code}): {response.text}"
            ) from exc

    def _analysis_reasoning_sequence(self) -> list[str]:
        primary = (self.settings.kie_reasoning_effort or "high").strip().lower()
        if primary == "high":
            return ["high", "medium", "low"]
        if primary == "medium":
            return ["medium", "low"]
        return [primary]

    @staticmethod
    def _is_retryable_analysis_error(error: ExternalAPIError) -> bool:
        text = str(error).lower()
        return any(code in text for code in ("(500)", "(502)", "(503)", "(504)", "(429)")) or (
            "server exception" in text
        )


class SalesBotClient:
    def __init__(self, settings: Settings, http_client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = http_client or httpx.AsyncClient(
            timeout=settings.http_timeout_seconds
        )

    async def send_callback(
        self,
        *,
        client_id: str,
        message: str,
        extra_variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "client_id": str(client_id),
            "message": message,
        }
        if extra_variables:
            payload.update(extra_variables)
        response = await self._client.post(
            f"{self.settings.salesbot_api_base_url}/{self.settings.salesbot_api_key}/callback",
            json=payload,
        )
        return self._decode_json(response)

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _decode_json(response: httpx.Response) -> dict[str, Any]:
        try:
            response.raise_for_status()
            if not response.content:
                return {}
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ExternalAPIError(
                f"SalesBot request failed ({response.status_code}): {response.text}"
            ) from exc


class TelegramBotClient:
    def __init__(self, settings: Settings, http_client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = http_client or httpx.AsyncClient(
            timeout=settings.http_timeout_seconds
        )

    async def send_message(self, *, chat_id: str, text: str) -> dict[str, Any]:
        response = await self._client.post(
            f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
        )
        return self._decode_json(response)

    async def broadcast(self, admins: Iterable[str], text: str) -> None:
        for chat_id in admins:
            await self.send_message(chat_id=chat_id, text=text)

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _decode_json(response: httpx.Response) -> dict[str, Any]:
        try:
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ExternalAPIError(
                f"Telegram request failed ({response.status_code}): {response.text}"
            ) from exc
