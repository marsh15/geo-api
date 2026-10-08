import logging
from typing import Any
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class GeoAPIError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        file_id: UUID | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}
        self.file_id = file_id
        self.headers = headers or {}


def error_body(
    request_id: str,
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
    file_id: UUID | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {
        "code": code,
        "message": message,
        "request_id": request_id,
        "details": details or {},
    }
    if file_id is not None:
        error["file_id"] = str(file_id)
    return {"error": error}


async def geo_api_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, GeoAPIError)
    request_id = request.scope.get("state", {}).get("request_id", "unknown")
    return JSONResponse(
        error_body(
            request_id,
            exc.code,
            exc.message,
            details=exc.details,
            file_id=exc.file_id,
        ),
        status_code=exc.status_code,
        headers=exc.headers,
    )


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    request_id = request.scope.get("state", {}).get("request_id", "unknown")
    details = [
        {
            "location": list(item.get("loc", ())),
            "message": item.get("msg", "Invalid value"),
            "type": item.get("type", "value_error"),
        }
        for item in exc.errors()
    ]
    return JSONResponse(
        error_body(
            request_id,
            "REQUEST_VALIDATION_ERROR",
            "Request validation failed.",
            details={"errors": details},
        ),
        status_code=422,
    )


async def http_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    request_id = request.scope.get("state", {}).get("request_id", "unknown")
    code, message = {
        404: ("NOT_FOUND", "The requested resource was not found."),
        405: ("METHOD_NOT_ALLOWED", "The request method is not supported."),
    }.get(exc.status_code, ("HTTP_ERROR", "The request could not be completed."))
    return JSONResponse(error_body(request_id, code, message), status_code=exc.status_code)


async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
    request_id = request.scope.get("state", {}).get("request_id", "unknown")
    logger.exception("Unhandled request failure request_id=%s", request_id)
    return JSONResponse(
        error_body(request_id, "INTERNAL_ERROR", "The request could not be completed."),
        status_code=500,
    )


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(GeoAPIError, geo_api_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_error_handler)
    app.add_exception_handler(Exception, unexpected_error_handler)
