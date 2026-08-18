"""Request-ID propagation shared by dlserver and lbserver.

A single id follows one request across nginx -> lbserver -> dlserver so an
operator-visible failure can be traced with one grep instead of correlating
wall-clock timestamps across four logs.

Install order matters: `install_request_id_logging()` must run before any log
record is emitted, because LOG_FORMAT references %(request_id)s and a record
without that attribute raises during formatting. Using a LogRecord factory
(rather than a logging.Filter) guarantees *every* record has the attribute,
including ones from third-party libraries that never pass through our handlers.
"""

from __future__ import annotations

import contextvars
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import Request, Response

REQUEST_ID_HEADER = "x-request-id"
NO_REQUEST_ID = "-"

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default=NO_REQUEST_ID
)

_installed = False


def install_request_id_logging() -> None:
    """Make every LogRecord carry `request_id`. Idempotent."""
    global _installed
    if _installed:
        return
    old_factory = logging.getLogRecordFactory()

    def factory(*args: object, **kwargs: object) -> logging.LogRecord:
        record = old_factory(*args, **kwargs)
        record.request_id = request_id_var.get()
        return record

    logging.setLogRecordFactory(factory)
    _installed = True


def new_request_id() -> str:
    return uuid.uuid4().hex


async def request_id_middleware(
    request: "Request", call_next: Callable[["Request"], Awaitable["Response"]]
) -> "Response":
    """Bind the inbound X-Request-ID (or mint one) for the duration of the request."""
    rid = request.headers.get(REQUEST_ID_HEADER) or new_request_id()
    token = request_id_var.set(rid)
    try:
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = rid
        return response
    finally:
        request_id_var.reset(token)
