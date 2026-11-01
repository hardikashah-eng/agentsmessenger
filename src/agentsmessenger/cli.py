"""amsg - command line for the messenger."""

from __future__ import annotations

import argparse
import json
import os
import sys


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="amsg", description="Agents Messenger")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the messenger server")
    s.add_argument("--db", default=os.environ.get("AMSG_DB", "agentsmessenger.db"))
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--open-registration", action="store_true", help="let agents register without the observer token")

    r = sub.add_parser("register", help="register an agent and print its token")
    r.add_argument("name")
    r.add_argument("--cap", action="append", default=[], help="capability, repeatable")
    r.add_argument("--vendor", default="")
    r.add_argument("--owner", default="")
    r.add_argument("--description", default="")
    r.add_argument("--protocol", action="append", default=[])
    r.add_argument("--url", default=os.environ.get("AMSG_URL", "http://127.0.0.1:8765"))
    r.add_argument("--observer-token", default=os.environ.get("AMSG_OBSERVER_TOKEN"))

    m = sub.add_parser("mcp", help="run the MCP adapter (stdio) for one agent")
    m.add_argument("--url", default=os.environ.get("AMSG_URL", "http://127.0.0.1:8765"))
    m.add_argument("--token", default=os.environ.get("AMSG_TOKEN"))

    a = p.parse_args(argv)

    if a.cmd == "serve":
        import secrets

        import uvicorn

        from .bus import Bus
        from .server import create_app
        from .store import Store

        token = os.environ.get("AMSG_OBSERVER_TOKEN") or ("obs_" + secrets.token_urlsafe(24))
        app = create_app(Bus(Store(a.db)), observer_token=token, open_registration=a.open_registration or None)
        print(f"Agents Messenger on http://{a.host}:{a.port}", file=sys.stderr)
        if not os.environ.get("AMSG_OBSERVER_TOKEN"):
            print(f"Observer token (set AMSG_OBSERVER_TOKEN to keep it fixed): {token}", file=sys.stderr)
        uvicorn.run(app, host=a.host, port=a.port, log_level="warning")
        return 0

    if a.cmd == "register":
        from .client import Client, MessengerError

        c = Client(a.url, a.observer_token)
        try:
            out = c.register(a.name, a.cap, vendor=a.vendor, owner=a.owner, description=a.description,
                             protocols=a.protocol or ["http"])
        except MessengerError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(json.dumps(out, indent=2, default=str))
        return 0

    if a.cmd == "mcp":
        if not a.token:
            print("error: --token or AMSG_TOKEN required", file=sys.stderr)
            return 2
        from .mcp_adapter import run

        run(a.url, a.token)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
