#!/usr/bin/env python3
"""Start the Jev Harness web UI and API.

    python3 app.py                # http://127.0.0.1:8765
    python3 app.py --port 9000
    python3 app.py --host 0.0.0.0 # exposes the server; only do this knowingly

No third-party packages are required. Python 3.9 or newer.
"""
from __future__ import annotations

import argparse
import sys
import webbrowser

from jevharness.config import load_config
from jevharness.server import serve


def main(argv=None) -> int:
    config = load_config()
    parser = argparse.ArgumentParser(description="Run the Jev Harness locally.")
    parser.add_argument("--host", default=config.host,
                        help="bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=config.port,
                        help="port (default 8765)")
    parser.add_argument("--open", action="store_true",
                        help="open the UI in a browser once the server is up")
    args = parser.parse_args(argv)

    config.host = args.host
    config.port = args.port

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"  warning: binding to {args.host} makes the harness reachable from "
            "your network.\n  Anyone who can reach it can spend whatever key is "
            "in your .env.\n"
        )
    if args.open:
        threading_timer(f"http://{args.host}:{args.port}")
    serve(config)
    return 0


def threading_timer(url: str) -> None:
    import threading

    threading.Timer(0.7, lambda: webbrowser.open(url)).start()


if __name__ == "__main__":
    sys.exit(main())
