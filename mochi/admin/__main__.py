"""Standalone admin portal entry point.

Usage:
    python -m mochi.admin              # uses ADMIN_PORT/ADMIN_BIND from .env
    python -m mochi.admin --port 9090  # override port
"""

import argparse
import asyncio
import logging
import os
import secrets
import sys
import webbrowser


def _ensure_admin_token(log) -> str:
    """Ensure ADMIN_TOKEN is set. Auto-generate if missing."""
    from mochi.admin.admin_env import read_env_value, _bootstrap_write_env

    token = read_env_value("ADMIN_TOKEN")
    if token:
        os.environ["ADMIN_TOKEN"] = token
        log.info("Using existing ADMIN_TOKEN from .env")
        return token

    token = secrets.token_urlsafe(32)
    _bootstrap_write_env("ADMIN_TOKEN", token)
    os.environ["ADMIN_TOKEN"] = token
    log.info("Generated new ADMIN_TOKEN (saved to .env)")
    log.info("Token: %s", token)
    return token


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s - %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("mochi.admin")

    # Warn if not running inside a virtual environment
    if sys.prefix == sys.base_prefix:
        log.warning(
            "Not running inside a virtual environment. "
            "Bot subprocess may fail due to missing packages. "
            "Run setup.bat / setup.sh to create and use a venv."
        )

    try:
        import fastapi  # noqa: F401
        import uvicorn
    except ImportError:
        log.critical("Admin portal requires: pip install fastapi uvicorn")
        sys.exit(1)

    # Auto-generate ADMIN_TOKEN before importing config (so config reads it)
    token = _ensure_admin_token(log)

    from mochi.config import ADMIN_PORT, ADMIN_BIND

    parser = argparse.ArgumentParser(description="MochiBot Admin Portal")
    parser.add_argument("--port", type=int, default=ADMIN_PORT)
    parser.add_argument("--bind", type=str, default=ADMIN_BIND)
    parser.add_argument("--no-browser", action="store_true",
                        help="Don't auto-open browser on startup")
    args = parser.parse_args()

    # Warn if binding to a non-localhost address
    _LOCALHOST = {"127.0.0.1", "localhost", "::1"}
    if args.bind not in _LOCALHOST:
        log.warning(
            "=" * 60 + "\n"
            "  WARNING: Binding to %s (network-accessible)\n"
            "  The admin portal will be exposed to your network.\n"
            "  Consider using a reverse proxy with HTTPS.\n" +
            "=" * 60, args.bind
        )

    from mochi.admin.admin_server import app

    url = f"http://{args.bind}:{args.port}?token={token}"
    log.info("Admin portal: %s", url)
    log.info("Stop this process before running python -m mochi.main")

    if not args.no_browser:
        webbrowser.open(url)

    config = uvicorn.Config(app, host=args.bind, port=args.port, log_level="info")
    server = uvicorn.Server(config)
    asyncio.run(server.serve())


if __name__ == "__main__":
    main()
