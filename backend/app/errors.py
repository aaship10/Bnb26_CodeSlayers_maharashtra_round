"""Uniform error envelope: every non-2xx response body is

    {"code": "<MACHINE_CODE>", "message": "<human text>", "details": {...}?}

Codes are part of the public contract (frontend and simulator branch on them);
add new ones, never rename existing ones.
"""
from __future__ import annotations

import logging
from enum import StrEnum
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("fairdrop.errors")


class ErrorCode(StrEnum):
    # entry / claim flow
    WINDOW_NOT_OPEN = "WINDOW_NOT_OPEN"
    WINDOW_CLOSED = "WINDOW_CLOSED"
    NOT_WINNER = "NOT_WINNER"
    HOLD_EXPIRED = "HOLD_EXPIRED"
    ALREADY_CLAIMED = "ALREADY_CLAIMED"
    # defence layer (Member B)
    RATE_LIMITED = "RATE_LIMITED"
    CHALLENGE_REQUIRED = "CHALLENGE_REQUIRED"
    REJECTED = "REJECTED"
    # generic
    UNAUTHENTICATED = "UNAUTHENTICATED"
    FORBIDDEN = "FORBIDDEN"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    INVALID_PHASE = "INVALID_PHASE"
    IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"
    INTERNAL = "INTERNAL"


DEFAULT_STATUS: dict[ErrorCode, int] = {
    ErrorCode.WINDOW_NOT_OPEN: 409,
    ErrorCode.WINDOW_CLOSED: 409,
    ErrorCode.NOT_WINNER: 409,
    ErrorCode.HOLD_EXPIRED: 409,
    ErrorCode.ALREADY_CLAIMED: 409,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.CHALLENGE_REQUIRED: 403,
    ErrorCode.REJECTED: 403,
    ErrorCode.UNAUTHENTICATED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.VALIDATION_ERROR: 422,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.INVALID_PHASE: 409,
    ErrorCode.IDEMPOTENCY_KEY_REUSED: 422,
    ErrorCode.INTERNAL: 500,
}


class ApiError(Exception):
    def __init__(self, code: ErrorCode, message: str, *, status: int | None = None,
                 details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status or DEFAULT_STATUS[code]
        self.details = details

    def body(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": str(self.code), "message": self.message}
        if self.details is not None:
            out["details"] = self.details
        return out


def error_response(err: ApiError) -> JSONResponse:
    return JSONResponse(status_code=err.status, content=err.body())


_HTTP_TO_CODE = {
    401: ErrorCode.UNAUTHENTICATED,
    403: ErrorCode.FORBIDDEN,
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.NOT_FOUND,
    422: ErrorCode.VALIDATION_ERROR,
    429: ErrorCode.RATE_LIMITED,
}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError):
        return error_response(exc)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        errs = [
            {"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")}
            for e in exc.errors()
        ]
        return error_response(ApiError(ErrorCode.VALIDATION_ERROR, "Request validation failed",
                                       details={"errors": errs}))

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException):
        code = _HTTP_TO_CODE.get(exc.status_code, ErrorCode.INTERNAL)
        return error_response(ApiError(code, str(exc.detail), status=exc.status_code))

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception):
        log.exception("unhandled error", exc_info=exc)
        return error_response(ApiError(ErrorCode.INTERNAL, "Internal server error"))
