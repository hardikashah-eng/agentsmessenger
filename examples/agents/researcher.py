"""web.fetch - fetches a public web page and returns its title and text.

payload: {"url": "https://...", "max_chars": 4000}
"""

import os
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx

from agentsmessenger.agent import Agent, Result

agent = Agent.connect("researcher", ["web.fetch"], vendor="local", description="Fetches web pages",
                      owner=os.environ.get("AMSG_OWNER", ""))


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head"}

    def __init__(self):
        super().__init__()
        self.title, self.parts, self._skip, self._in_title = "", [], 0, False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip and data.strip():
            self.parts.append(data.strip())


@agent.handles("web.fetch")
def fetch(payload, task):
    url = payload["url"]
    if urlparse(url).scheme not in ("http", "https"):
        raise ValueError("only http(s) URLs")
    task.progress(f"Fetching {url}")
    r = httpx.get(url, follow_redirects=True, timeout=30, headers={"User-Agent": "agentsmessenger-researcher/0.1"})
    r.raise_for_status()
    p = _Text()
    p.feed(r.text)
    text = " ".join(p.parts)[: int(payload.get("max_chars", 4000))]
    return Result({"url": str(r.url), "status": r.status_code, "title": p.title.strip(), "text": text},
                  text=f"{p.title.strip() or url}: {len(text)} chars")


if __name__ == "__main__":
    agent.run()
