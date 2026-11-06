"""Add an agent in plain language: pick what kind it is and what it's good at.

Used by `amsg add` (which can also write the config files for you) and by
the viewer's "Add agent" form (which shows copy-paste setup).
"""

from __future__ import annotations

import json
import os
import platform
import re
import shlex
from pathlib import Path
from typing import Any

# What people say -> capabilities the router understands.
SKILLS: dict[str, dict[str, Any]] = {
    "coding": {"label": "Writing and fixing code", "caps": ["code.task"]},
    "code-review": {"label": "Reviewing code", "caps": ["code.review"]},
    "research": {"label": "Research and fact-finding", "caps": ["research"]},
    "writing": {"label": "Writing and editing", "caps": ["text.write"]},
    "summarizing": {"label": "Summarizing", "caps": ["text.summarize"]},
    "data": {"label": "Data analysis", "caps": ["data.analyze"]},
    "checking": {"label": "Checking other agents' work", "caps": ["verify"]},
    "general": {"label": "Anything else", "caps": ["general"]},
}

KINDS: dict[str, dict[str, Any]] = {
    "claude-code": {"label": "Claude Code (acts when you prompt it)", "vendor": "anthropic", "always_on": False},
    "claude-desktop": {"label": "Claude Desktop app (acts when you prompt it)", "vendor": "anthropic", "always_on": False},
    "codex": {"label": "OpenAI Codex CLI (acts when you prompt it)", "vendor": "openai", "always_on": False},
    "claude-bot": {"label": "Always-on bot using the Claude API", "vendor": "anthropic", "always_on": True},
    "openai-bot": {"label": "Always-on bot using the OpenAI API", "vendor": "openai", "always_on": True},
    "a2a": {"label": "A remote agent that speaks A2A", "vendor": "a2a", "always_on": True},
}


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9._-]+", "-", name.strip().lower()).strip("-.")
    return (s or "agent")[:64]


def capabilities(skills: list[str], can_review: bool) -> list[str]:
    caps: list[str] = []
    for s in skills:
        if s not in SKILLS:
            raise ValueError(f"unknown skill {s!r}; choose from {', '.join(SKILLS)}")
        caps += SKILLS[s]["caps"]
    if can_review:
        caps.append("verify")
        if "coding" in skills or "code-review" in skills:
            caps.append("code.review")
    return sorted(set(caps))


def card(name: str, kind: str, skills: list[str], *, can_review: bool | None = None, cost_hint_usd: float | None = None,
         description: str = "", owner: str = "", public_key: str | None = None) -> dict[str, Any]:
    """The agent card to register. Always-on bots review others' work by default."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}; choose from {', '.join(KINDS)}")
    review = KINDS[kind]["always_on"] if can_review is None else can_review
    caps = capabilities(skills, review)
    rates = {c: cost_hint_usd for c in caps if c not in ("verify", "code.review")} if cost_hint_usd is not None else {}
    return {"name": slug(name), "capabilities": caps, "vendor": KINDS[kind]["vendor"], "rates": rates,
            "description": description or ", ".join(SKILLS[s]["label"] for s in skills),
            "owner": owner, "cost_hint_usd": cost_hint_usd, "public_key": public_key,
            "protocols": ["mcp"] if not KINDS[kind]["always_on"] else ["http"]}


# ------------------------------------------------------------------ setup

def claude_desktop_config_path() -> Path:
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if system == "Windows":
        return Path(os.environ.get("APPDATA", Path.home())) / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def setup(kind: str, *, url: str, name: str, token: str, private_key: str | None, caps: list[str],
          agent_card: dict[str, Any] | None = None) -> dict[str, Any]:
    """How to connect this agent: steps, plus a command or a file to write."""
    server_name = f"agents-messenger-{name}"
    env = {"AMSG_URL": url, "AMSG_TOKEN": token}
    if private_key:
        env["AMSG_SIGNING_KEY"] = private_key
    if kind == "claude-code":
        argv = ["claude", "mcp", "add", server_name]
        for k, v in env.items():
            argv += ["-e", f"{k}={v}"]
        argv += ["--", "amsg", "mcp"]
        return {"title": "Connect Claude Code", "command": " ".join(shlex.quote(x) for x in argv), "argv": argv,
                "steps": ["Run this command once (it registers the messenger as an MCP server for Claude Code).",
                          "Then ask Claude Code, for example: \"Check my messenger inbox and handle anything for you.\""]}
    if kind == "claude-desktop":
        entry = {"command": "amsg", "args": ["mcp"], "env": env}
        return {"title": "Connect Claude Desktop", "merge_json": {"path": str(claude_desktop_config_path()),
                                                                  "key": ["mcpServers", server_name], "value": entry},
                "snippet": json.dumps({"mcpServers": {server_name: entry}}, indent=2),
                "steps": [f"Add this to {claude_desktop_config_path()} (Settings > Developer > Edit Config).",
                          "Restart Claude Desktop. If it can't find `amsg`, use the full path from `which amsg`."]}
    if kind == "codex":
        lines = [f"[mcp_servers.{server_name}]", 'command = "amsg"', 'args = ["mcp"]',
                 "env = { " + ", ".join(f'{k} = "{v}"' for k, v in env.items()) + " }"]
        return {"title": "Connect the Codex CLI", "append_file": {"path": str(Path.home() / ".codex" / "config.toml"),
                                                                  "content": "\n" + "\n".join(lines) + "\n"},
                "snippet": "\n".join(lines),
                "steps": ["Add this to ~/.codex/config.toml.",
                          "Then ask Codex: \"Check my messenger inbox and handle anything for you.\""]}
    if kind in ("claude-bot", "openai-bot"):
        path = Path.home() / ".amsg" / "bots" / f"{name}.py"
        key_var = "ANTHROPIC_API_KEY" if kind == "claude-bot" else "OPENAI_API_KEY"
        pkg = "anthropic" if kind == "claude-bot" else "openai"
        return {"title": "Run the always-on bot",
                "write_file": {"path": str(path), "content": bot_script(kind, name, url, caps, agent_card or {})},
                "credentials": {"url": url, "name": name, "token": token, "private_key": private_key,
                                "public_key": (agent_card or {}).get("public_key")},
                "command": f"pip install {pkg} && {key_var}=... python {path}",
                "steps": [f"Set {key_var} to your API key.", f"Run: python {path}",
                          "It picks up work the router sends it, and reviews others' work if it offers `verify`."]}
    raise ValueError(f"no setup for {kind!r}")


_BOT = '''"""{name}: an always-on messenger agent using the {provider} API. Generated by `amsg add`."""

import os

from agentsmessenger import Client
from agentsmessenger.agent import Agent, Result, Verdict

{client_setup}

URL = os.environ.get("AMSG_URL", {url!r})
if os.environ.get("AMSG_TOKEN"):  # credentials from the environment (setup from the viewer)
    agent = Agent(Client(URL, os.environ["AMSG_TOKEN"], signing_key=os.environ.get("AMSG_SIGNING_KEY")))
else:  # credentials saved by `amsg add` in ~/.amsg/agents.json
    agent = Agent.connect({name!r}, {caps!r}, url=URL, vendor={vendor!r}, description={description!r},
                          cost_hint_usd={cost!r})


def work(payload, task):
    prompt = task.text or str(payload)
    if task.revision:  # a reviewer rejected the last attempt
        prompt += "\\n\\nA reviewer rejected your previous answer: " + task.revision["feedback"] + ". Fix it."
    return Result({{"answer": ask(prompt)}})


def review(payload, task):
    verdict = ask(
        "You are checking another agent's work. Answer APPROVE or REJECT on the first line, then why.\\n\\n"
        f"Task: {{payload['task']}}\\n\\nResult: {{payload['result']}}"
    )
    first, _, rest = verdict.partition("\\n")
    return Verdict(first.strip().upper().startswith("APPROVE"), rest.strip()[:1000])


for cap in agent.client.me()["capabilities"]:
    if cap in ("verify", "code.review"):
        agent.reviews(cap)(review)
    else:
        agent.handles(cap)(work)

if __name__ == "__main__":
    agent.run()
'''

_CLAUDE = '''import anthropic

llm = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
MODEL = os.environ.get("AMSG_MODEL", "claude-sonnet-5-5")


def ask(prompt):
    msg = llm.messages.create(model=MODEL, max_tokens=2000, messages=[{"role": "user", "content": prompt}])
    return msg.content[0].text'''

_OPENAI = '''from openai import OpenAI

llm = OpenAI()  # reads OPENAI_API_KEY
MODEL = os.environ["AMSG_MODEL"]  # the OpenAI model to use


def ask(prompt):
    return llm.responses.create(model=MODEL, input=prompt).output_text'''


def bot_script(kind: str, name: str, url: str, caps: list[str], agent_card: dict[str, Any]) -> str:
    return _BOT.format(name=name, url=url, caps=caps, provider="Claude" if kind == "claude-bot" else "OpenAI",
                       client_setup=_CLAUDE if kind == "claude-bot" else _OPENAI,
                       vendor=agent_card.get("vendor", ""), description=agent_card.get("description", ""),
                       cost=agent_card.get("cost_hint_usd"))


def apply(plan: dict[str, Any]) -> list[str]:
    """Write the files a setup plan describes (used by `amsg add`). Returns what changed."""
    done = []
    if "merge_json" in plan:
        m = plan["merge_json"]
        p = Path(m["path"])
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        if p.exists():
            p.with_suffix(p.suffix + ".bak").write_text(json.dumps(data, indent=2), encoding="utf-8")
        node = data
        for k in m["key"][:-1]:
            node = node.setdefault(k, {})
        node[m["key"][-1]] = m["value"]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")
        done.append(f"updated {p}" + (" (backup: .bak)" if p.with_suffix(p.suffix + ".bak").exists() else ""))
    if "append_file" in plan:
        p = Path(plan["append_file"]["path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(plan["append_file"]["content"])
        done.append(f"appended to {p}")
    if "write_file" in plan:
        p = Path(plan["write_file"]["path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(plan["write_file"]["content"], encoding="utf-8")
        done.append(f"wrote {p}")
    if "credentials" in plan:
        from .agent import _load_creds, _save_creds  # pylint: disable=import-outside-toplevel  # avoids a cycle
        c = plan["credentials"]
        creds = _load_creds()
        creds[f"{c['url']}|{c['name']}"] = {"token": c["token"], "private_key": c["private_key"],
                                            "public_key": c.get("public_key")}
        _save_creds(creds)
        done.append("saved the bot's credentials to ~/.amsg/agents.json")
    return done
