"""Operator dashboard shell — the static bundle the API process serves.

The dashboard is a vanilla HTML/JS/CSS single-page app. It is mounted on a
prefix of its own so it cannot shadow an operator API route, and it carries no
auth of its own: its calls carry the operator key as a Bearer token and are
authenticated by the existing operator-realm dependency, exactly as the
operator API already is.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.types import Scope

logger = logging.getLogger("palimpsest.dashboard")

# Deliberately not "/operator": that prefix is reserved for the operator API
# (keys today; accounts and config alongside it), and a mount there would
# compete with those routes for every path below it.
DASHBOARD_MOUNT_PATH = "/dashboard"

DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "static" / "dashboard"

# The bundle holds the operator credential in sessionStorage and loads nothing
# from elsewhere, so it can run under a strict policy: same-origin only, no
# framing (clickjacking), no plugins, and forms may only post back to itself.
_SECURITY_HEADERS = {
    "Cache-Control": "no-cache",
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; object-src 'none'; "
        "frame-ancestors 'none'; form-action 'self'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


class DashboardAssets(StaticFiles):
    """Static files that browsers must revalidate rather than reuse.

    ``StaticFiles`` emits ``ETag``/``Last-Modified`` but no ``Cache-Control``,
    which leaves browsers free to apply a heuristic freshness lifetime and serve
    a **stale bundle** after the gateway is updated. An operator who changes a
    setting at runtime and reloads would then be looking at yesterday's
    dashboard — precisely the outcome runtime configuration exists to prevent.

    The same response also carries the security headers above. A missing asset
    (404) gets them too, so a missing file is never heuristically cached.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            response = await super().get_response(path, scope)
        except HTTPException as exc:
            exc.headers = {**(exc.headers or {}), **_SECURITY_HEADERS}
            raise
        response.headers.update(_SECURITY_HEADERS)
        return response


def mount_dashboard(app: FastAPI) -> None:
    """Serve the dashboard bundle from the API app.

    ``html=True`` serves ``index.html`` for the mount root and returns 404 for
    any other missing asset. A bundle missing from the image (a packaging
    mistake) disables the dashboard and logs it, rather than failing the import
    of the whole API.

    Args:
        app: The application that also serves the API.
    """
    if not DASHBOARD_DIR.is_dir():
        logger.error("dashboard bundle missing at %s; /dashboard is disabled", DASHBOARD_DIR)
        return
    app.mount(
        DASHBOARD_MOUNT_PATH,
        DashboardAssets(directory=DASHBOARD_DIR, html=True),
        name="dashboard",
    )
