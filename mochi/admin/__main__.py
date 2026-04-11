"""Standalone admin portal entry point.

Usage:
    python -m mochi.admin              # uses ADMIN_PORT/ADMIN_BIND from .env
    python -m mochi.admin --port 9090  # override port
"""

import argparse
import asyncio
import logging
import sys


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s - %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("mochi.admin")

    try:
        import fastapi  # noqa: F401
        import uvicorn
    except ImportError:
        log.critical("Admin portal requires: pip install fastapi uvicorn")
        sys.exit(1)

    from mochi.config import ADMIN_PORT, ADMIN_BIND

    parser = argparse.ArgumentParser(description="MochiBot Admin Portal")
    parser.add_argument("--port", type=int, default=ADMIN_PORT)
    parser.add_argument("--bind", type=str, default=ADMIN_BIND)
    args = parser.parse_args()

    from mochi.admin.admin_server import app

    log.info("Admin portal: http://%s:%d", args.bind, args.port)
    log.info("Stop this process before running python -m mochi.main")

    config = uvicorn.Config(app, host=args.bind, port=args.port, log_level="info")
    server = uvicorn.Server(config)
    asyncio.run(server.serve())


if __name__ == "__main__":
    main()
