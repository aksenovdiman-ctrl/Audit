from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


class SessionStartRequest(BaseModel):
    client_id: str = Field(min_length=1)
    project_id: str = Field(default="salesbot")
    client_type: str = Field(default="instagram")
    client_name: str | None = None
    instagram_username: str | None = None

    @field_validator("project_id", mode="before")
    @classmethod
    def default_project_id(cls, value: object) -> str:
        if value is None:
            return "salesbot"
        text = str(value).strip()
        return text or "salesbot"

    @field_validator("client_type", mode="before")
    @classmethod
    def default_client_type(cls, value: object) -> str:
        if value is None:
            return "instagram"
        text = str(value).strip()
        return text or "instagram"


class AnalysisPayload(BaseModel):
    overall_score: int = Field(ge=0, le=100)
    niche_guess: str = Field(min_length=1)
    strengths: list[str] = Field(min_length=1)
    problems: list[str] = Field(min_length=1)
    quick_wins: list[str] = Field(min_length=1)
    dm_audit_text: str = Field(min_length=1)
    image_brief: str = Field(min_length=1)


class HealthResponse(BaseModel):
    status: str


class SessionInputRequest(BaseModel):
    client_id: str = Field(min_length=1)
    message: str | None = None
    attachments: Any = None
    attachment_url: str | None = None
    client_name: str | None = None
    instagram_username: str | None = None
