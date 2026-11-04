"""Hardening for a localhost-only app that serves financial data."""

from urllib.parse import urlsplit

from flask import Flask, Response, abort, request

from .config import Config

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# No inline scripts/styles anywhere, so the policy can be strict.
CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "connect-src 'self'",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
    ]
)


def register_security(app: Flask, config: Config) -> None:
    allowed_hosts = config.allowed_hosts

    @app.before_request
    def _check_request() -> None:
        # DNS-rebinding defence: a hostile web page can point its own hostname at 127.0.0.1 and
        # then read this API as "same origin". Only answer to the host names we expect.
        hostname = urlsplit(f"//{request.host}").hostname
        if hostname is None or hostname.lower() not in allowed_hosts:
            abort(400, description="Untrusted Host header.")

        # CSRF defence for state-changing calls: a browser cannot attach a custom header to a
        # cross-site request without a CORS preflight, which this app never approves.
        if (
            request.method not in SAFE_METHODS
            and request.headers.get("X-Requested-With") != "fetch"
        ):
            abort(403, description="Missing X-Requested-With header.")

    @app.after_request
    def _add_headers(response: Response) -> Response:
        response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        # Balances must never sit in a cache. Routes that set their own policy (the backtest
        # results, which are revalidated with an ETag) keep it.
        if request.path.startswith("/api/") and "Cache-Control" not in response.headers:
            response.headers["Cache-Control"] = "no-store"
        return response
