"""Error contract: every non-2xx body is {code, message, details?}."""
from __future__ import annotations

import logging
from enum import Enum
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("fd.errors")


class ErrorCode(str, Enum):
    WINDOW_NOT_OPEN = "WINDOW_NOT_OPEN"
    WINDOW_CLOSED = "WINDOW_CLOSED"
    NOT_WINNER = "NOT_WINNER"
    HOLD_EXPIRED = "HOLD_EXPIRED"
    ALREADY_CLAIMED = "ALREADY_CLAIMED"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    FORBIDDEN = "FORBIDDEN"
    RATE_LIMITED = "RATE_LIMITED"
    CHALLENGE_REQUIRED = "CHALLENGE_REQUIRED"
    REJECTED = "REJECTED"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    INTERNAL = "INTERNAL"


DEFAULT_STATUS: dict[ErrorCode, int] = {
    ErrorCode.WINDOW_NOT_OPEN: 409,
    ErrorCode.WINDOW_CLOSED: 409,
    ErrorCode.NOT_WINNER: 403,
    ErrorCode.HOLD_EXPIRED: 410,
    ErrorCode.ALREADY_CLAIMED: 409,
    ErrorCode.UNAUTHENTICATED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.CHALLENGE_REQUIRED: 403,
    ErrorCode.REJECTED: 403,
    ErrorCode.VALIDATION_ERROR: 422,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.INTERNAL: 500,
}


class ApiError(Exception):
    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        status: int | None = None,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status if status is not None else DEFAULT_STATUS[code]
        self.details = details
        self.headers = headers or {}

    def body(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code.value, "message": self.message}
        if self.details is not None:
            out["details"] = self.details
        return out


def _resp(err: ApiError) -> JSONResponse:
    return JSONResponse(err.body(), status_code=err.status, headers=err.headers or None)


def clean_validation_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # pydantic's `ctx` can hold exception objects that are not JSON serialisable.
    return [{"loc": list(e.get("loc", [])), "msg": str(e.get("msg", "")), "type": str(e.get("type", ""))} for e in errors]


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return _resp(exc)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        return _resp(
            ApiError(
                ErrorCode.VALIDATION_ERROR,
                "request validation failed",
                details={"errors": clean_validation_errors(exc.errors())},
            )
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        mapping = {401: ErrorCode.UNAUTHENTICATED, 403: ErrorCode.FORBIDDEN, 404: ErrorCode.NOT_FOUND}
        code = mapping.get(exc.status_code, ErrorCode.INTERNAL if exc.status_code >= 500 else ErrorCode.VALIDATION_ERROR)
        return _resp(ApiError(code, str(exc.detail), status=exc.status_code))

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        # Never leak internals to clients.
        log.exception("unhandled error", exc_info=exc)
        return _resp(ApiError(ErrorCode.INTERNAL, "internal error"))
