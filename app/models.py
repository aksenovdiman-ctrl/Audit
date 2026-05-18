from __future__ import annotations

from pydantic import BaseModel, Field


class SessionStartRequest(BaseModel):
    client_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    client_type: str = Field(min_length=1)
    client_name: str | None = None
    instagram_username: str | None = None


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
