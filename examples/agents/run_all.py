"""Run every reference agent in one process.

    export AMSG_OBSERVER_TOKEN=obs_dev   # same token the server uses
    amsg serve &
    python examples/agents/run_all.py
"""

import importlib
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
NAMES = ["summarizer", "researcher", "coder", "payments"]

if __name__ == "__main__":
    agents = [importlib.import_module(n).agent for n in NAMES]
    threads = [threading.Thread(target=a.run, daemon=True) for a in agents]
    for t in threads:
        t.start()
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        for a in agents:
            a.stop()
