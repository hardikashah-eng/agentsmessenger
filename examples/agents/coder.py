"""code.task - runs a coding task with the Claude Code CLI in a workspace.

Asks the human observer before touching any directory. Workspaces must be
inside AMSG_CODER_ROOT (default ~/amsg-workspaces).

payload: {"prompt": "...", "workspace": "my-repo"}
"""

import os
import shutil
import subprocess
from pathlib import Path

from agentsmessenger.agent import Agent, HumanDeclined, Result

ROOT = Path(os.environ.get("AMSG_CODER_ROOT", Path.home() / "amsg-workspaces")).resolve()
agent = Agent.connect("coder", ["code.task"], vendor="anthropic", description="Runs coding tasks with Claude Code",
                      owner=os.environ.get("AMSG_OWNER", ""))


@agent.handles("code.task")
def code(payload, task):
    prompt = payload["prompt"]
    ws = (ROOT / payload.get("workspace", "scratch")).resolve()
    if ROOT not in ws.parents and ws != ROOT:
        raise ValueError("workspace must be inside AMSG_CODER_ROOT")
    if not shutil.which("claude"):
        raise RuntimeError("the `claude` CLI is not installed on this machine")
    if not task.confirm(f"Run Claude Code in {ws} with: {prompt!r}?", workspace=str(ws), prompt=prompt):
        raise HumanDeclined("not approved")
    ws.mkdir(parents=True, exist_ok=True)
    task.progress("Running Claude Code")
    proc = subprocess.run(["claude", "-p", prompt], cwd=ws, capture_output=True, text=True,
                          timeout=int(payload.get("timeout", 900)))
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[-2000:] or f"exit code {proc.returncode}")
    out = proc.stdout.strip()
    return Result({"workspace": str(ws), "output": out[-8000:]}, text=out[-2000:])


if __name__ == "__main__":
    agent.run()
