"""Two heterogeneous agents cooperate over the messenger.

Start the server first:
    AMSG_OBSERVER_TOKEN=obs_dev amsg serve
Then:
    AMSG_OBSERVER_TOKEN=obs_dev python examples/two_agents.py
Open http://127.0.0.1:8765, paste obs_dev, and watch the thread.

The "agents" here are plain functions so the demo runs without API keys;
swap the bodies for Claude / OpenAI / local-model calls.
"""

import os
import threading
import time

from agentsmessenger import Client

URL = os.environ.get("AMSG_URL", "http://127.0.0.1:8765")
OBS = os.environ["AMSG_OBSERVER_TOKEN"]


def register(name, caps, vendor):
    c = Client(URL, OBS)
    try:
        c.register(name, caps, vendor=vendor, owner="me")
    except Exception:
        # Already registered from an earlier run: register under a fresh name.
        c = Client(URL, OBS)
        c.register(f"{name}-{int(time.time())}", caps, vendor=vendor, owner="me")
    return c


def summarizer_agent(client: Client, stop: threading.Event):
    """Offers text.summarize. Claims requests, works, returns a result."""
    def handle(d):
        e = d["envelope"]
        if e["intent"] != "request":
            return
        tid = e["thread_id"]
        try:
            client.claim(tid)
        except Exception:
            return  # someone else got it
        client.set_state(tid, "working")
        client.reply(e, "inform", {"progress": 0.5}, text="Reading the document…")
        words = e["payload"].get("text", "").split()
        summary = " ".join(words[:12]) + ("…" if len(words) > 12 else "")
        client.reply(e, "result", {"summary": summary, "words": len(words)},
                     text=f"Summary: {summary}", cost_usd=0.003)
        client.set_state(tid, "done")

    client.listen(handle, wait=2, stop=stop.is_set)


def main():
    planner = register("planner", [], vendor="anthropic")
    summarizer = register("summarizer", ["text.summarize"], vendor="local")

    stop = threading.Event()
    threading.Thread(target=summarizer_agent, args=(summarizer, stop), daemon=True).start()

    print("Directory:", [a["name"] for a in planner.find("text.summarize")])
    req = planner.request(
        "text.summarize",
        {"text": "Agent native messaging treats agents as the primary users. Humans observe, "
                 "step in when asked, and can pause anything. Messages are typed envelopes."},
        subject="Summarize the design note", budget_usd=0.10,
    )
    tid = req["thread_id"]
    for _ in range(40):
        if planner.thread(tid)["state"] == "done":
            break
        time.sleep(0.25)
    ctx = planner.context(tid)
    print(ctx["summary"])
    print("Result:", ctx["latest_results"][-1]["payload"])
    stop.set()


if __name__ == "__main__":
    main()
