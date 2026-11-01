"""payment.pay - an approval-gated agent (SIMULATED: nothing is paid).

Shows the human-in-the-loop pattern: every payment waits for the observer
to answer yes/no in the viewer. Approved payments are appended to a local
ledger file instead of moving money.

payload: {"payee": "City Utilities", "amount": 84.12, "currency": "USD", "memo": "..."}
"""

import json
import os
import time
from pathlib import Path

from agentsmessenger.agent import Agent, HumanDeclined, Result

LEDGER = Path(os.environ.get("AMSG_LEDGER", Path.home() / ".amsg" / "ledger.jsonl"))
agent = Agent.connect("payments", ["payment.pay"], vendor="local",
                      description="Pays bills after human approval (simulated)", owner=os.environ.get("AMSG_OWNER", ""))


@agent.handles("payment.pay")
def pay(payload, task):
    payee, amount = payload["payee"], float(payload["amount"])
    currency = payload.get("currency", "USD")
    if amount <= 0:
        raise ValueError("amount must be positive")
    if not task.confirm(f"Pay {amount:.2f} {currency} to {payee}?", payee=payee, amount=amount, currency=currency):
        raise HumanDeclined("payment not approved")
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": time.time(), "payee": payee, "amount": amount, "currency": currency,
             "memo": payload.get("memo"), "thread": task.thread_id, "simulated": True}
    with LEDGER.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    return Result({"status": "recorded", "simulated": True, **entry}, text=f"Recorded {amount:.2f} {currency} to {payee} (simulated)")


if __name__ == "__main__":
    agent.run()
