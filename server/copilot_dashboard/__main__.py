"""Entrypoint: `python -m copilot_dashboard` or `copilot-dashboard`."""
from __future__ import annotations

import argparse
import os

import uvicorn

from .app import create_app


def main() -> None:
    p = argparse.ArgumentParser(prog="copilot-dashboard")
    p.add_argument("--host", default=os.environ.get("CD_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("CD_PORT", "8770")))
    p.add_argument("--reload", action="store_true", help="dev mode")
    args = p.parse_args()
    app = create_app()
    uvicorn.run(
        app if not args.reload else "copilot_dashboard.app:create_app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        factory=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
