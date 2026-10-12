"""Goal storage never launches, resumes or interrupts a runner."""

import asyncio

from fastapi import APIRouter, HTTPException, Request

from omnigent.entities.session_goal import (
    SessionGoalConflict,
    SessionGoalResponse,
    SetSessionGoalRequest,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_EDIT, LEVEL_READ, AuthProvider
from omnigent.server.routes._auth_helpers import get_user_id, require_access
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.permission_store import PermissionStore


def register_goal_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    auth_provider: AuthProvider | None,
    permission_store: PermissionStore | None,
) -> None:
    async def access(request: Request, session_id: str, level: int) -> str | None:
        user_id = get_user_id(request, auth_provider)
        await require_access(user_id, session_id, level, permission_store, conversation_store)
        conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if conv is None:
            raise OmnigentError("Session not found", code=ErrorCode.NOT_FOUND)
        if conv.labels.get("omnigent.wrapper") not in {
            "claude-code-native-ui",
            "opencode-native-ui",
        }:
            raise OmnigentError(
                "Omnigent goals require Claude or OpenCode native sessions; "
                "Codex sessions retain codex_goal",
                code=ErrorCode.INVALID_INPUT,
            )
        return user_id

    @router.get("/sessions/{session_id}/goals", response_model=SessionGoalResponse)
    async def get_goal(request: Request, session_id: str) -> SessionGoalResponse:
        await access(request, session_id, LEVEL_READ)
        goal = await asyncio.to_thread(conversation_store.get_session_goal, session_id)
        return SessionGoalResponse(goal=goal)

    @router.put("/sessions/{session_id}/goals", response_model=SessionGoalResponse)
    async def set_goal(
        request: Request, session_id: str, body: SetSessionGoalRequest
    ) -> SessionGoalResponse:
        user_id = await access(request, session_id, LEVEL_EDIT)
        try:
            goal = await asyncio.to_thread(
                conversation_store.set_session_goal, session_id, body, user_id
            )
        except SessionGoalConflict as exc:
            raise HTTPException(409, "Goal revision changed; read it before editing") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return SessionGoalResponse(goal=goal)
