from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, status

from app.clients import KieClient, SalesBotClient, TelegramBotClient
from app.config import Settings, get_settings
from app.models import HealthResponse, SessionStartRequest
from app.service import AuditOrchestrator
from app.storage import SQLiteRepository


def create_app(
    *,
    settings: Settings | None = None,
    repository: SQLiteRepository | None = None,
    kie_client: KieClient | None = None,
    salesbot_client: SalesBotClient | None = None,
    telegram_client: TelegramBotClient | None = None,
) -> FastAPI:
    app_settings = settings or get_settings()
    repo = repository or SQLiteRepository(app_settings.database_path)
    kie = kie_client or KieClient(app_settings)
    salesbot = salesbot_client or SalesBotClient(app_settings)
    telegram = telegram_client or TelegramBotClient(app_settings)
    orchestrator = AuditOrchestrator(
        settings=app_settings,
        repository=repo,
        kie_client=kie,
        salesbot_client=salesbot,
        telegram_client=telegram,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        repo.init_db()
        yield
        for client in (kie, salesbot, telegram):
            close = getattr(client, "aclose", None)
            if callable(close):
                await close()

    app = FastAPI(
        title="Instagram Audit Bot",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = app_settings
    app.state.repository = repo
    app.state.orchestrator = orchestrator

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(status="ok")

    @app.post("/salesbot/session/start")
    async def start_session(payload: SessionStartRequest) -> dict[str, Any]:
        return await orchestrator.open_session(payload)

    @app.post("/salesbot/events")
    async def salesbot_events(
        request: Request,
        background_tasks: BackgroundTasks,
        token: str = Query(...),
    ) -> dict[str, Any]:
        if token != app_settings.salesbot_webhook_token:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid token")
        payload = await request.json()
        result = await orchestrator.ingest_salesbot_event(payload)
        client_info = payload.get("client") or {}
        client_id = str(client_info.get("id") or payload.get("client_id") or "").strip()
        if result.schedule_processing and client_id:
            background_tasks.add_task(orchestrator.process_session, client_id)
        return {
            "status": "ok",
            "action": result.action,
            "attachments_count": result.attachments_count,
        }

    @app.post("/kie/callback")
    async def kie_callback(
        request: Request,
        token: str = Query(...),
        job_id: int | None = Query(default=None),
    ) -> dict[str, Any]:
        if token != app_settings.kie_callback_token:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid token")
        payload = await request.json()
        try:
            return await orchestrator.handle_kie_callback(job_id=job_id, payload=payload)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @app.post("/telegram/webhook")
    async def telegram_webhook(
        request: Request,
        token: str = Query(...),
    ) -> dict[str, Any]:
        if token != app_settings.telegram_webhook_token:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid token")
        payload = await request.json()
        return await orchestrator.handle_telegram_update(payload)

    return app

