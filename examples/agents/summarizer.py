"""text.summarize - summarizes text.

Uses the Anthropic API when ANTHROPIC_API_KEY is set (model from
AMSG_SUMMARY_MODEL, default claude-haiku-5-5); otherwise a simple
extractive summary so the demo runs offline.

payload: {"text": "...", "max_words": 60}
"""

import os
import re

import httpx

from agentsmessenger.agent import Agent, Result

agent = Agent.connect("summarizer", ["text.summarize"], vendor="anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "local",
                      description="Summarizes text", owner=os.environ.get("AMSG_OWNER", ""))


def llm_summary(text: str, max_words: int) -> tuple[str, float]:
    r = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"},
        json={
            "model": os.environ.get("AMSG_SUMMARY_MODEL", "claude-haiku-5-5"),
            "max_tokens": 400,
            "messages": [{"role": "user", "content": f"Summarize in at most {max_words} words:\n\n{text}"}],
        },
        timeout=60,
    )
    r.raise_for_status()
    body = r.json()
    return body["content"][0]["text"].strip(), 0.0  # fill in your own cost model


def extractive(text: str, max_words: int) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    out: list[str] = []
    for s in sentences:
        if len(" ".join(out + [s]).split()) > max_words:
            break
        out.append(s)
    return " ".join(out) or " ".join(text.split()[:max_words])


@agent.handles("text.summarize")
def summarize(payload, task):
    text = payload.get("text") or task.text or ""
    if not text.strip():
        raise ValueError("payload.text is empty")
    max_words = int(payload.get("max_words", 60))
    task.progress(f"Summarizing {len(text.split())} words")
    if os.environ.get("ANTHROPIC_API_KEY"):
        summary, cost = llm_summary(text, max_words)
    else:
        summary, cost = extractive(text, max_words), 0.0
    return Result({"summary": summary, "words_in": len(text.split())}, text=summary, cost_usd=cost)


if __name__ == "__main__":
    agent.run()
