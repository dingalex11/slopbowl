"""HTTP routes for captain-created packet challenges.

The router receives the existing authentication callable instead of importing it,
which keeps the auth dependency shared without a circular import with ui_service.
Endpoints call it at the same point as before so validation precedence does not change.
"""

import asyncio
import logging
import re

from fastapi import APIRouter, HTTPException, Request

from core.config import APP_ENV
from services import challenge_service

logger = logging.getLogger(__name__)

_CHALLENGE_CODE_RE = re.compile(r"^[a-z0-9]{4,8}$")


def _challenge_code(code: str) -> str:
    if not isinstance(code, str) or not _CHALLENGE_CODE_RE.fullmatch(code):
        raise HTTPException(status_code=400, detail="Invalid challenge code")
    return code


def create_challenge_router(authenticated_user) -> APIRouter:
    router = APIRouter()

    @router.get("/api/challenges/{code}")
    async def challenge_meta_endpoint(code: str, request: Request):
        code = _challenge_code(code)
        await authenticated_user(request)
        try:
            return await asyncio.to_thread(challenge_service.get_challenge_meta, code)
        except PermissionError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            logger.warning("Unable to read a challenge: %s", exc)
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.get("/api/challenges")
    async def list_challenges_endpoint(request: Request):
        """The signed-in captain's own created challenges, for the profile screen."""
        user = await authenticated_user(request)
        try:
            return await asyncio.to_thread(challenge_service.list_challenges, user.uid)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            logger.warning("Unable to list challenges: %s", exc)
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.post("/api/challenges")
    async def create_challenge_endpoint(request: Request):
        user = await authenticated_user(request)
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError("Invalid challenge setup")
            return await asyncio.to_thread(
                challenge_service.create_challenge, user.uid, data.get("title"), data.get("bank"),
                read_bonuses=data.get("readBonuses", True), display_mode=data.get("displayMode", "hybrid"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            logger.warning("Unable to create a challenge: %s", exc)
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.post("/api/challenges/{code}/join")
    async def join_challenge_endpoint(code: str, request: Request):
        code = _challenge_code(code)
        user = await authenticated_user(request)
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError("Invalid join request")
            return await asyncio.to_thread(
                challenge_service.join_challenge, code, user.uid, data.get("displayName")
            )
        except PermissionError as exc:
            # A spent attempt is a conflict, not a bad request: the member played already.
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            logger.warning("Unable to join a challenge: %s", exc)
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.get("/api/challenges/{code}/next")
    async def next_challenge_question(code: str, request: Request):
        code = _challenge_code(code)
        user = await authenticated_user(request)
        try:
            return await asyncio.to_thread(challenge_service.next_question, code, user.uid)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except RuntimeError as exc:
            logger.warning("Unable to read the next question: %s", exc)
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.post("/api/challenges/{code}/answer")
    async def submit_challenge_answer(code: str, request: Request):
        code = _challenge_code(code)
        user = await authenticated_user(request)
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError("Invalid answer submission")
            return await asyncio.to_thread(
                challenge_service.submit_answer, code, user.uid,
                data.get("qIndex"), data.get("phase"),
                data.get("typedAnswer", ""), data.get("buzz"), data.get("timing"),
                bool(data.get("expired", False)), data.get("outcome", "answered"),
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            logger.warning("Unable to record an answer: %s", exc)
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.post("/api/challenges/{code}/report")
    async def report_challenge_question(code: str, request: Request):
        """A member flags one answered question for the captain to review."""
        code = _challenge_code(code)
        user = await authenticated_user(request)
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError("Invalid question report")
            return await asyncio.to_thread(
                challenge_service.report_question, code, user.uid,
                data.get("qIndex"), data.get("phase"), data.get("reason", ""),
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            logger.warning("Unable to record a question report: %s", exc)
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.post("/api/challenges/{code}/judge")
    async def judge_challenge_answer(code: str, request: Request):
        """The captain sets a toss-up or bonus verdict by hand."""
        code = _challenge_code(code)
        user = await authenticated_user(request)
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError("Invalid judgement")
            return await asyncio.to_thread(
                challenge_service.judge_answer, code, user.uid,
                data.get("uid"), data.get("qIndex"), data.get("phase"),
                bool(data.get("correct", False)),
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            logger.warning("Unable to judge an answer: %s", exc)
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.get("/api/challenges/{code}/board")
    async def challenge_board_endpoint(code: str, request: Request):
        code = _challenge_code(code)
        user = await authenticated_user(request)
        try:
            return await asyncio.to_thread(challenge_service.get_board, code, user.uid)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except RuntimeError as exc:
            logger.warning("Unable to read the board: %s", exc)
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.post("/api/challenges/{code}/seed")
    async def seed_challenge_board_endpoint(code: str, request: Request):
        """Development only: fabricate demo runs so the board is verifiable without accounts."""
        if APP_ENV == "production":
            raise HTTPException(status_code=404, detail="Not found")
        code = _challenge_code(code)
        user = await authenticated_user(request)
        try:
            return await asyncio.to_thread(challenge_service.seed_dev_runs, code, user.uid)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except RuntimeError as exc:
            logger.warning("Unable to seed the board: %s", exc)
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    return router
