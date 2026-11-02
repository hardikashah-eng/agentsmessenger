"""verify + code.review - checks other agents' results before a thread is done.

With ANTHROPIC_API_KEY set, Claude reviews the result against the task
(model from AMSG_REVIEW_MODEL, default claude-sonnet-5-5). Without it, the
agent runs basic offline checks only: the result is non-empty, reports no
error, and any Python code in it compiles. Offline checks catch obvious
failures, not wrong answers; use a model (or a human) for real review.
"""

import ast
import json
import os
import re

import httpx

from agentsmessenger.agent import Agent, Verdict

agent = Agent.connect("reviewer", ["verify", "code.review"],
                      vendor="anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "local",
                      description="Checks other agents' work", owner=os.environ.get("AMSG_OWNER", ""))


def llm_review(task: dict, result: dict) -> Verdict:
    prompt = ("You are reviewing another AI agent's work. Reply with APPROVE or REJECT on the first line, "
              "then one short paragraph explaining why. Reject anything wrong, incomplete or unsafe.\n\n"
              f"Task:\n{json.dumps(task, indent=2)[:6000]}\n\nResult:\n{json.dumps(result, indent=2)[:8000]}")
    r = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"},
        json={"model": os.environ.get("AMSG_REVIEW_MODEL", "claude-sonnet-5-5"), "max_tokens": 600,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=90,
    )
    r.raise_for_status()
    first, _, rest = r.json()["content"][0]["text"].strip().partition("\n")
    return Verdict(first.strip().upper().startswith("APPROVE"), rest.strip()[:1000])


def offline_review(result: dict, category: str | None) -> Verdict:
    body = result.get("payload") or {}
    if not body and not result.get("text"):
        return Verdict(False, "the result is empty")
    if "error" in body:
        return Verdict(False, f"the result reports an error: {body['error']}")
    if category == "code":
        blob = json.dumps(body)
        for code in [v for v in body.values() if isinstance(v, str)] + re.findall(r"```(?:python)?\n(.*?)```", blob, re.S):
            if re.search(r"^\s*(def |class |import |from )", code, re.M):
                try:
                    ast.parse(code)
                except SyntaxError as e:
                    return Verdict(False, f"the Python code does not compile: {e}")
    return Verdict(True, "basic offline checks passed (non-empty, no error, code compiles); not a full review")


@agent.reviews("verify", "code.review")
def review(payload, _task):
    if os.environ.get("ANTHROPIC_API_KEY"):
        return llm_review(payload["task"], payload["result"])
    return offline_review(payload["result"], payload.get("category"))


if __name__ == "__main__":
    agent.run()
