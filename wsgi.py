"""WSGI entry point: ``make run`` / ``flask --app wsgi run`` / ``python wsgi.py``."""

import os

from app import create_app
from app.config import DEFAULT_PORT

app = create_app()

if __name__ == "__main__":
    # Bound to loopback on purpose: the page shows balances and has no login of its own.
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", DEFAULT_PORT)))
