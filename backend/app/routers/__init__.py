from app.schemas import ErrorBody

# Shared OpenAPI error documentation.
ERRORS = {
    401: {"model": ErrorBody, "description": "UNAUTHENTICATED"},
    403: {"model": ErrorBody, "description": "FORBIDDEN / REJECTED / CHALLENGE_REQUIRED"},
    404: {"model": ErrorBody, "description": "NOT_FOUND"},
    409: {"model": ErrorBody, "description": "Conflict with current state (see code)"},
    422: {"model": ErrorBody, "description": "VALIDATION_ERROR / IDEMPOTENCY_KEY_REUSED"},
}
