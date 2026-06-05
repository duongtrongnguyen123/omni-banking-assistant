"""Prometheus text-format exposition endpoint.

Single GET endpoint at ``/api/metrics`` — no auth (it's a demo). The
returned body is whatever ``services.metrics.REGISTRY.render()``
produces, with the ``text/plain; version=0.0.4`` content type Prometheus
scrapers expect.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from ..services import metrics as _metrics

router = APIRouter(prefix="/api", tags=["metrics"])


@router.get("/metrics", response_class=PlainTextResponse)
def metrics() -> PlainTextResponse:
    """Return the live registry as Prometheus text exposition.

    ``version=0.0.4`` in the Content-Type is what scrapers historically
    used to negotiate the format — keeping it for compatibility, though
    most modern Prometheus servers no-op on the version token.
    """
    body = _metrics.REGISTRY.render()
    return PlainTextResponse(
        content=body,
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
