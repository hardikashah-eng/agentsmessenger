"""`amsg add`: add an agent by answering a few questions."""

from __future__ import annotations

import argparse
import getpass
import shutil
import subprocess
import sys
from typing import Any, Callable

from .client import Client, MessengerError
from .onboard import KINDS, SKILLS, apply, card, setup, slug
from .signing import generate_keypair

DEFAULT_SKILLS = {"claude-code": ["coding"], "codex": ["coding"], "claude-desktop": ["writing", "research"],
                  "claude-bot": ["general"], "openai-bot": ["general"]}


def _choose(prompt: str, options: dict[str, dict[str, Any]], ask: Callable[[str], str]) -> str:
    keys = list(options)
    print(prompt)
    for i, k in enumerate(keys, 1):
        print(f"  {i}. {options[k]['label']}")
    while True:
        raw = ask("Choose a number: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(keys):
            return keys[int(raw) - 1]
        if raw in options:
            return raw


def _choose_many(prompt: str, options: dict[str, dict[str, Any]], default: list[str], ask: Callable[[str], str]) -> list[str]:
    keys = list(options)
    print(prompt)
    for i, k in enumerate(keys, 1):
        print(f"  {i}. {options[k]['label']}")
    raw = ask(f"Numbers separated by commas [{', '.join(str(keys.index(d) + 1) for d in default)}]: ").strip()
    if not raw:
        return default
    picked = [keys[int(x) - 1] for x in raw.replace(" ", "").split(",") if x.isdigit() and 1 <= int(x) <= len(keys)]
    return picked or default


def _yes(prompt: str, default: bool, ask: Callable[[str], str]) -> bool:
    raw = ask(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    return default if not raw else raw.startswith("y")


def run_add(a: argparse.Namespace, ask: Callable[[str], str] = input) -> int:  # pylint: disable=too-many-branches,too-many-statements
    interactive = sys.stdin.isatty() and not a.yes
    print("Add an agent to Agents Messenger\n")
    kind = a.kind or _choose("What kind of agent is it?", KINDS, ask)

    if kind == "a2a":
        remote = a.remote or ask("The remote agent's URL: ").strip()
        name = f" --name {slug(a.name)}" if a.name else ""
        print("\nRun this (it reads the remote agent's card, registers it, and forwards work to it):\n")
        print(f"  amsg a2a-proxy {remote}{name} --url {a.url}\n")
        return 0

    name = slug(a.name or (ask(f"Give it a short name [my-{kind}]: ").strip() if interactive else "") or f"my-{kind}")
    skills = a.skill or (_choose_many("What is it good at?", SKILLS, DEFAULT_SKILLS[kind], ask) if interactive
                         else DEFAULT_SKILLS[kind])
    review = a.review
    if review is None:
        default = KINDS[kind]["always_on"]
        review = _yes("Can it check other agents' work? (every result is checked by a different agent)", default, ask) \
            if interactive else default
    token = a.observer_token or getpass.getpass("Observer token (printed by `amsg serve`): ")

    priv, pub = generate_keypair()
    c = card(name, kind, skills, can_review=review, cost_hint_usd=a.cost, public_key=pub)
    client = Client(a.url, token)
    try:
        out = client.register(**c)
    except MessengerError as e:
        print(f"\nCould not register {name}: {e}", file=sys.stderr)
        if e.status == 401:
            print("Check the observer token, and that `amsg serve` is running at " + a.url, file=sys.stderr)
        return 1
    except OSError as e:
        print(f"\nCould not reach the messenger at {a.url}: {e}. Is `amsg serve` running?", file=sys.stderr)
        return 1

    plan = setup(kind, url=a.url, name=name, token=out["token"], private_key=priv, caps=c["capabilities"], agent_card=c)
    print(f"\n✓ {name} joined. It offers: {', '.join(c['capabilities'])}. Its messages are signed.")
    print("  The router sends it matching work" + (" and asks it to check others' results." if review else "."))
    print(f"\n{plan['title']}:")
    for step in plan["steps"]:
        print(f"  - {step}")
    _finish(plan, kind, a, interactive, ask)
    return 0


def _finish(plan: dict[str, Any], kind: str, a: argparse.Namespace, interactive: bool, ask: Callable[[str], str]) -> None:
    if a.print_only:
        for key in ("command", "snippet"):
            if plan.get(key):
                print("\n" + plan[key])
        return
    if kind == "claude-code":
        if shutil.which("claude") and (a.yes or (interactive and _yes("Run it now?", True, ask))):
            done = subprocess.run(plan["argv"], check=False)
            print("✓ Claude Code is connected." if done.returncode == 0 else "That didn't work; run the command above.")
        else:
            print("\n" + plan["command"])
        return
    files = [v["path"] for k, v in plan.items() if k in ("merge_json", "append_file", "write_file")]
    if a.yes or (interactive and _yes(f"Make these changes for you ({', '.join(files)})?", True, ask)):
        for line in apply(plan):
            print(f"✓ {line}")
        if plan.get("command") and kind.endswith("-bot"):
            print(f"\nStart it with:\n  {plan['command']}")
    else:
        if plan.get("credentials"):
            print("\nTo run the bot without the saved credentials file, set AMSG_TOKEN and AMSG_SIGNING_KEY:")
            print(f"  AMSG_TOKEN={plan['credentials']['token']}\n  AMSG_SIGNING_KEY={plan['credentials']['private_key']}")
        print("\n" + (plan.get("snippet") or plan.get("command") or plan.get("write_file", {}).get("content", "")))
