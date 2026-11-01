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
    s.add_argument("--require-signatures", action="store_true", help="every agent must register a key and sign envelopes")
    s.add_argument("--server-key", default=os.environ.get("AMSG_SERVER_KEY_FILE", "amsg-server.key"),
                   help="file holding the server's Ed25519 key (created if missing)")

    sub.add_parser("keygen", help="print a new Ed25519 key pair for an agent")

    x = sub.add_parser("a2a-proxy", help="expose a remote A2A agent on the messenger")
    x.add_argument("remote", help="remote A2A endpoint URL (its card is read from /.well-known/agent-card.json)")
    x.add_argument("--name", help="messenger name for the proxy (default: from the remote card)")
    x.add_argument("--remote-token", default=os.environ.get("A2A_REMOTE_TOKEN"))
    x.add_argument("--url", default=os.environ.get("AMSG_URL", "http://127.0.0.1:8765"))
    x.add_argument("--observer-token", default=os.environ.get("AMSG_OBSERVER_TOKEN"))

    r = sub.add_parser("register", help="register an agent and print its token")
    r.add_argument("name")
    r.add_argument("--cap", action="append", default=[], help="capability, repeatable")
    r.add_argument("--vendor", default="")
    r.add_argument("--owner", default="")
    r.add_argument("--description", default="")
    r.add_argument("--protocol", action="append", default=[])
    r.add_argument("--public-key", help="Ed25519 public key from `amsg keygen`; the agent must then sign")
    r.add_argument("--url", default=os.environ.get("AMSG_URL", "http://127.0.0.1:8765"))
    r.add_argument("--observer-token", default=os.environ.get("AMSG_OBSERVER_TOKEN"))

    m = sub.add_parser("mcp", help="run the MCP adapter (stdio) for one agent")
    m.add_argument("--url", default=os.environ.get("AMSG_URL", "http://127.0.0.1:8765"))
    m.add_argument("--token", default=os.environ.get("AMSG_TOKEN"))
    m.add_argument("--signing-key", default=os.environ.get("AMSG_SIGNING_KEY"))

    a = p.parse_args(argv)

    if a.cmd == "serve":
        import secrets

        import uvicorn

        from .bus import Bus
        from .server import create_app
        from .signing import ServerSigner
        from .store import Store

        token = os.environ.get("AMSG_OBSERVER_TOKEN") or ("obs_" + secrets.token_urlsafe(24))
        bus = Bus(Store(a.db), signer=ServerSigner(key_file=a.server_key), require_signatures=a.require_signatures)
        app = create_app(bus, observer_token=token, open_registration=a.open_registration or None)
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
                             protocols=a.protocol or ["http"], public_key=a.public_key)
        except MessengerError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(json.dumps(out, indent=2, default=str))
        return 0

    if a.cmd == "keygen":
        from .signing import generate_keypair

        priv, pub = generate_keypair()
        print(json.dumps({"private_key": priv, "public_key": pub}, indent=2))
        print("Register with --public-key <public_key>; give the agent the private_key (AMSG_SIGNING_KEY).", file=sys.stderr)
        return 0

    if a.cmd == "a2a-proxy":
        import re

        from .a2a import A2AClient, A2AProxy
        from .client import Client, MessengerError

        remote = A2AClient(a.remote, a.remote_token)
        card = remote.card()
        name = a.name or re.sub(r"[^a-z0-9._-]+", "-", card.get("name", "remote").lower()).strip("-") or "remote"
        caps = [re.sub(r"[^A-Za-z0-9._-]+", "-", s["id"]) for s in card.get("skills", []) if s.get("id")]
        c = Client(a.url, a.observer_token)
        try:
            c.register(name, caps, vendor="a2a", protocols=["a2a"], description=card.get("description", ""),
                       a2a_url=a.remote)
        except MessengerError as e:
            print(f"error registering proxy: {e}", file=sys.stderr)
            return 1
        print(f"[{name}] proxying {a.remote} skills={caps}", file=sys.stderr)
        A2AProxy(c, remote).run()
        return 0

    if a.cmd == "mcp":
        if not a.token:
            print("error: --token or AMSG_TOKEN required", file=sys.stderr)
            return 2
        from .mcp_adapter import run

        run(a.url, a.token, a.signing_key)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
