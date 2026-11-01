"""Send a few requests to the reference agents and print what comes back.

    export AMSG_OBSERVER_TOKEN=obs_dev
    amsg serve &
    python examples/agents/run_all.py &
    python examples/demo.py

The payment request waits for you: open http://127.0.0.1:8765 and answer "yes".
"""

import time

from agentsmessenger.agent import Agent

me = Agent.connect("demo-planner", [], description="Sends demo requests").client


def wait(tid, timeout=600):
    deadline = time.time() + timeout
    announced = False
    while time.time() < deadline:
        t = me.thread(tid)
        if t["state"] == "waiting_human" and not announced:
            print(f"  … waiting for you in the viewer (thread {tid})")
            announced = True
        if t["state"] in ("done", "failed"):
            return me.context(tid)
        time.sleep(0.5)
    return me.context(tid)


jobs = [
    ("text.summarize", {"text": "Agent-native messaging treats agents as the primary users of the system. "
                                "Humans observe, answer when an agent asks, and can pause anything. Messages are "
                                "typed envelopes with intents, payloads, budgets and signatures.", "max_words": 20}),
    ("web.fetch", {"url": "https://example.com"}),
    ("payment.pay", {"payee": "City Utilities", "amount": 84.12, "memo": "October electricity"}),
]

for cap, payload in jobs:
    print(f"→ {cap}")
    req = me.request(cap, payload, subject=f"Demo: {cap}", budget_usd=0.25)
    ctx = wait(req["thread_id"])
    print("  ", ctx["summary"])
    if ctx["latest_results"]:
        print("   result:", ctx["latest_results"][-1]["text"] or ctx["latest_results"][-1]["payload"])
