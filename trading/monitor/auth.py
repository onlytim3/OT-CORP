"""Security layer for the dashboard UI and API."""

import hmac
import logging

from flask import request, jsonify, redirect

from trading.config import DASHBOARD_PIN

log = logging.getLogger(__name__)

# Opaque session token store: token → validated PIN value.
# Populated by auth_login() in web.py; checked here.
# Process-local (single gunicorn worker) — adequate for this deployment.
_session_store: dict[str, str] = {}


def check_dashboard_auth() -> bool:
    """Verify if the request has a valid session token.

    Checks:
    1. HTTP Cookie 'session_token' (for browser SPA)
    2. HTTP Authorization Bearer token (for API callers)

    Fail-closed: if DASHBOARD_PIN is not set, always returns False.
    """
    if not DASHBOARD_PIN:
        return False

    # Cookie path (browser SPA — SameSite=Strict prevents CSRF)
    cookie_token = request.cookies.get("session_token")
    if cookie_token:
        stored = _session_store.get(cookie_token)
        if stored and hmac.compare_digest(stored, DASHBOARD_PIN):
            return True

    # Bearer token path (API callers / programmatic access)
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        bearer = auth_header[7:]
        stored = _session_store.get(bearer)
        if stored and hmac.compare_digest(stored, DASHBOARD_PIN):
            return True

    return False


def get_auth_middleware(app):
    """Register BEFORE_REQUEST middleware on the Flask app."""

    @app.before_request
    def require_auth():
        # Health check and version are always open (used for deployment verification)
        if request.path.startswith("/api/health") or request.path == "/api/version":
            return None
        if request.path in ("/login", "/api/auth/login", "/api/auth/logout"):
            return None

        if not check_dashboard_auth():
            if request.path.startswith("/api/"):
                return jsonify({"error": "Unauthorized. Invalid or missing PIN."}), 401
            return redirect("/login")

        return None
