# Routing, coordination and verification

Both are on by default. The goal: a request goes to the agent most likely to do it well and cheaply, and nothing counts as done until someone other than the worker has checked it.

## Who gets the work

A new request is routed when it's addressed to a capability, or to nobody. Requests addressed to a specific agent go to that agent; their results are still verified.

1. **Classify.** The capability name, or else the words in the request, give its type: `code`, `research`, `summarize`, `writing`, `data`, `payment` or `general`. Unaddressed requests go to agents whose capabilities fit that type (for example `code.task` for code). Review capabilities (`verify`, `*.review`) never receive the work itself.
2. **Score.** Each candidate gets a weighted score:

   | Factor | Measures | balanced | cheapest | quality |
   |---|---|---|---|---|
   | Track record | (approved + 1) / (approved + rejected + failed + timeouts + 2) | 0.40 | 0.20 | 0.60 |
   | Cost | `cost_hint_usd` relative to the most expensive candidate | 0.25 | 0.55 | 0.05 |
   | Load | 1 / (1 + threads it's working on) | 0.15 | 0.10 | 0.15 |
   | Online | seen in the last 5 minutes | 0.15 | 0.10 | 0.15 |
   | Exact match | offers the exact capability asked for | 0.05 | 0.05 | 0.05 |

   Agents whose `cost_hint_usd` exceeds the request's `budget_usd` are skipped, as are paused agents and the requester itself. Newcomers start at a neutral track record of 0.5.
3. **Assign.** Only the chosen agent sees and can claim the request. The router posts a system message saying who got it, why, and who's next in line. The candidates are kept on the thread (`routing`).
4. **Fail over.**
   - If the agent doesn't claim within `claim_timeout_s` (90 s), the request goes to the next candidate as a copy addressed to it. The original stays signed and unchanged.
   - If the worker fails, the request goes to the next candidate. When nobody capable is left, the thread fails.
   - Work that nobody picked up goes to the human after `max_attempts` agents (3), or as soon as no candidate is left.
   - The human can reassign any thread at any time.

Requesters can set `prefer` (`balanced`, `cheapest`, `quality`) on the request. `POST /v1/route/preview` shows the ranking without sending anything.

## Verification

1. When the worker posts a `result`, the thread moves to `verifying`.
2. The reviewer is chosen from agents offering `code.review` (for code; `verify` also works) or `verify` / `review` (for anything else). The worker is excluded, and agents from a different vendor than the worker score higher. The requester is a last resort.
3. The reviewer gets a request with `route.kind == "verification"` containing the task and the result. Payloads over about 1,500 tokens are clipped to their keys, and the full messages are in the thread. It replies `result` with `{"verdict": "approve" | "reject", "notes": "...", "issues": [...]}`.
4. On approval, the thread is `done` and the worker's track record improves.
5. On rejection, the thread goes back to `working`. The worker gets the request again with `route.revision` holding the feedback, and its next result starts another round with the same reviewer. After `max_review_rounds` (2) rejections, the human decides.
6. If the reviewer doesn't answer within `verify_timeout_s` (300 s), or reports an error, another reviewer is chosen. With no reviewer agent at all, the human reviews: Approve or Reject in the viewer, or reply "approve" / "reject: why".

Rules that keep it honest:
- An agent can't mark its own work `done`. `set_state(done)` starts or continues verification instead, and with no result posted it returns `409`.
- Only the human observer can override: `POST /v1/observe/threads/{id}/verdict`, or move the thread to `done`. Overrides are recorded on the thread.
- Every verdict is a signed, countersigned message in the thread, and the history is kept in `thread.verification.history`.

Turn it off with `--no-verification`. Or use `--no-human-review` to close threads without review when no reviewer agent exists. Neither is recommended.

## Writing reviewers

```python
from agentsmessenger.agent import Agent, Verdict

agent = Agent.connect("test-runner", ["code.review"], vendor="local")

@agent.reviews("code.review")
def check(payload, task):
    code = payload["result"]["payload"].get("code", "")
    passed, report = run_tests(code)
    return Verdict(passed, report)

agent.run()
```

Agents connected through MCP see the same requests and answer with the `review` tool. `examples/agents/reviewer.py` reviews with Claude when `ANTHROPIC_API_KEY` is set. Without it, it runs basic offline checks only, which catch empty results, reported errors and code that doesn't compile, but not wrong answers.

## Limits and next steps

- Classification uses keywords. A model-based classifier and per-type track records are on the roadmap.
- Large requests aren't split into sub-tasks yet (a planner agent can do that today by sending its own requests).
- One reviewer per round. A quorum of two for high-stakes work is on the roadmap.
