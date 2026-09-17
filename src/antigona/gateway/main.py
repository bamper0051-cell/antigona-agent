"""Gateway service entry point — run via `python -m antigona.gateway`."""

from __future__ import annotations

import os

import uvicorn

from antigona.gateway.api import create_gateway_app


def main() -> None:
    """Start the Antigona Gateway FastAPI service.

    Port is read from ``ANTIGONA_GATEWAY_PORT`` (default 8090) so the gateway
    is reproducible and multi-instance capable (clean-room / alt ports).

    Host defaults to ``127.0.0.1``. On Windows, clients that resolve
    ``localhost`` to ``::1`` will miss an IPv4-only bind — prefer URL
    ``http://127.0.0.1:8090`` (set ``ANTIGONA_GATEWAY_URL``). Set
    ``ANTIGONA_GATEWAY_HOST=0.0.0.0`` only if you intentionally need LAN.
    """
    app = create_gateway_app()
    host = os.environ.get("ANTIGONA_GATEWAY_HOST", "127.0.0.1")
    port = int(os.environ.get("ANTIGONA_GATEWAY_PORT", "8090"))
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
