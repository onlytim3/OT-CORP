"""Security layer for the dashboard UI and API."""

import logging
from flask import request, jsonify, redirect

from trading.config import DASHBOARD_PIN

log = logging.getLogger(__name__)

def check_dashboard_auth():
    """Verify if the request has the correct PIN.
    
    Checks:
    1. HTTP Authorization Bearer token (for API calls)
    2. HTTP Cookie 'dashboard_pin' (for UI)
    
    If DASHBOARD_PIN is not set in the environment, returns True (unlocked).
    """
    if not DASHBOARD_PIN:
        log.warning("DASHBOARD_PIN is not set in the environment. Dashboard is UNLOCKED.")
        return True
        
    pin_cookie = request.cookies.get("dashboard_pin")
    if pin_cookie == DASHBOARD_PIN:
        return True
        
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header == f"Bearer {DASHBOARD_PIN}":
        return True
        
    return False


def get_auth_middleware(app):
    """Register BEFORE_REQUEST middleware on the Flask app."""
    
    @app.before_request
    def require_auth():
        # Do not block health checks or the login page itself
        if request.path.startswith("/api/health"):
            return None
        if request.path in ("/login", "/api/auth/login"):
            return None
            
        if not check_dashboard_auth():
            # If API call and not authorized, return 401
            if request.path.startswith("/api/"):
                return jsonify({"error": "Unauthorized. Invalid or missing PIN."}), 401
                
            # For all UI requests (/, /app, etc.), redirect to login
            return redirect("/login")
            
        return None
