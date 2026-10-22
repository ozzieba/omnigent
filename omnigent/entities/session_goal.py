"""Durable Omnigent goals for harnesses without native goal control."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

GoalStatus = Literal["active", "paused", "blocked", "completed", "cancelled"]


class SessionGoal(BaseModel):
    session_id: str
    objective: str
    status: GoalStatus
    revision: int
    completion_evidence: str | None = None
    created_at: int
    updated_at: int
    updated_by: str | None = None


class SetSessionGoalRequest(BaseModel):
    objective: str = Field(min_length=1, max_length=4000)
    expected_revision: int = Field(ge=0, strict=True)
    status: GoalStatus | None = None
    completion_evidence: str | None = Field(default=None, max_length=4000)

    model_config = ConfigDict(extra="forbid")

    @field_validator("objective")
    @classmethod
    def nonempty_objective(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("objective must contain non-NUL text")
        return value

    @field_validator("completion_evidence")
    @classmethod
    def nonnul_evidence(cls, value: str | None) -> str | None:
        if value is not None and "\x00" in value:
            raise ValueError("completion evidence must contain non-NUL text")
        return value


class SessionGoalResponse(BaseModel):
    goal: SessionGoal | None


class SessionGoalConflict(Exception):
    """The caller edited a stale goal revision."""
