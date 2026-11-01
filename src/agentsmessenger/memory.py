"""Agent memory with three scopes, shared by reference.

- private: only the owner can read it.
- shared:  the owner, agents it names, and the participants of threads it
           names can read it.
- public:  every agent on the messenger can read it.

Only the owner writes. The human observer can read and delete everything.

Agents share context by reference instead of pasting it into messages: an
envelope lists `memory_refs`, and recipients see a stub (key, one-line
summary, size in tokens). They open the full value, or one field of it with
`path`, only when they need it. `since=<rev>` returns only what changed.
Memory written by another agent is untrusted data, like its messages.
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any

from .guards import GuardError, UNTRUSTED_NOTICE
from .models import (
    MAX_MEMORY_BYTES, AgentRecord, Envelope, MemoryItem, MemoryShare, MemoryStub, MemoryWrite, Visibility,
    estimate_tokens, utcnow,
)

if TYPE_CHECKING:
    from .store import Store

MAX_ITEMS_PER_AGENT = 1000


def auto_summary(value: Any) -> str:
    if isinstance(value, str):
        s = " ".join(value.split())
        return s if len(s) <= 160 else s[:157] + "..."
    if isinstance(value, dict):
        keys = list(value)
        more = f" (+{len(keys) - 8} more)" if len(keys) > 8 else ""
        return "object with keys: " + ", ".join(map(str, keys[:8])) + more
    if isinstance(value, list):
        return f"list of {len(value)} items"
    return json.dumps(value)[:160]


def select_path(value: Any, path: str) -> Any:
    """Dotted path into a JSON value: 'findings.0.title'."""
    cur = value
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.lstrip("-").isdigit() and -len(cur) <= int(part) < len(cur):
            cur = cur[int(part)]
        else:
            raise GuardError("bad_path", f"path {path!r} not found in value", 404)
    return cur


class MemoryMixin:
    store: "Store"

    # Provided by Bus.
    def resolve_agent(self, ref: str) -> AgentRecord: ...  # type: ignore[empty-body]

    # --------------------------------------------------------------- access
    def can_read_memory(self, reader_id: str | None, item: MemoryItem) -> bool:
        if reader_id is None or item.owner == reader_id or item.visibility == Visibility.public:
            return True
        if item.visibility == Visibility.private:
            return False
        if reader_id in item.shared_with:
            return True
        return any(self.store.is_participant(t, reader_id) for t in item.threads)

    def _view(self, reader_id: str | None, item: MemoryItem, full: bool) -> MemoryItem:
        """Hide the grant lists from non-owners and flag other agents' memory as untrusted."""
        out = item.model_copy()
        if reader_id is not None and reader_id != item.owner:
            out.shared_with, out.threads = [], []
            out.untrusted = True
            if full:
                out.notice = UNTRUSTED_NOTICE
        if not full:
            out.value = None
        return out

    def _item(self, memory_id: str) -> MemoryItem:
        item = self.store.get_memory(memory_id)
        if not item or item.deleted:
            from .bus import NotFound
            raise NotFound(f"no memory {memory_id!r}")
        return item

    def _resolve_ids(self, refs: list[str]) -> list[str]:
        return sorted({self.resolve_agent(r).id for r in refs})

    def _check_threads(self, owner: str, threads: list[str]) -> list[str]:
        out = sorted(set(threads))
        for t in out:
            if not self.store.get_thread(t):
                from .bus import NotFound
                raise NotFound(f"no thread {t!r}")
            if not self.store.is_participant(t, owner):
                from .bus import Forbidden
                raise Forbidden(f"you can only share memory into threads you participate in ({t})")
        return out

    # --------------------------------------------------------------- writes
    def remember(self, agent: AgentRecord, w: MemoryWrite) -> MemoryItem:
        size = len(json.dumps(w.value, default=str).encode())
        if size > MAX_MEMORY_BYTES:
            raise GuardError("memory_too_large", f"value is {size} bytes; the limit is {MAX_MEMORY_BYTES}. "
                             "Store large content as an artifact and remember its URI.", 413)
        if w.visibility != Visibility.shared and (w.shared_with or w.threads):
            raise GuardError("bad_visibility", "shared_with and threads apply only to visibility='shared'", 422)
        prior = self.store.memory_by_key(agent.id, w.key)
        if (prior is None or prior.deleted) and self.store.count_memory(agent.id) >= MAX_ITEMS_PER_AGENT:
            raise GuardError("memory_full", f"an agent may keep at most {MAX_ITEMS_PER_AGENT} memory items", 409)
        now = utcnow()
        item = MemoryItem(
            id=prior.id if prior else "mem_" + uuid.uuid4().hex[:12],
            key=w.key, owner=agent.id, value=w.value, summary=w.summary or auto_summary(w.value),
            tags=sorted(set(w.tags)), visibility=w.visibility, version=(prior.version + 1) if prior else 1,
            rev=0, tokens=estimate_tokens(w.value), updated_at=now,
            created_at=prior.created_at if prior and not prior.deleted else now,
            shared_with=self._resolve_ids(w.shared_with), threads=self._check_threads(agent.id, w.threads),
            source=w.source,
        )
        return self._view(agent.id, self.store.save_memory(item), full=True)

    def share_memory(self, agent: AgentRecord, memory_id: str, s: MemoryShare) -> MemoryItem:
        item = self._item(memory_id)
        if item.owner != agent.id:
            from .bus import Forbidden
            raise Forbidden("only the owner can change who may read a memory item")
        if s.visibility is not None:
            item.visibility = s.visibility
        agents = (set(item.shared_with) | set(self._resolve_ids(s.add_agents))) - set(self._resolve_ids(s.remove_agents))
        threads = (set(item.threads) | set(self._check_threads(agent.id, s.add_threads))) - set(s.remove_threads)
        if item.visibility != Visibility.shared:
            agents, threads = set(), set()  # grants mean nothing for private/public; drop them
        item.shared_with, item.threads = sorted(agents), sorted(threads)
        item.updated_at = utcnow()
        return self._view(agent.id, self.store.save_memory(item), full=False)

    def forget(self, agent: AgentRecord | None, memory_id: str) -> MemoryStub:
        """Delete (owner or observer). Leaves a tombstone so `since` readers can evict their copies."""
        item = self._item(memory_id)
        if agent is not None and item.owner != agent.id:
            from .bus import Forbidden
            raise Forbidden("only the owner (or the observer) can delete a memory item")
        item.deleted, item.value, item.shared_with, item.threads = True, None, [], []
        item.summary, item.tokens, item.updated_at = "(deleted)", 0, utcnow()
        return self.store.save_memory(item).stub()

    # ---------------------------------------------------------------- reads
    def read_memory(self, reader: AgentRecord | None, memory_id: str, path: str | None = None) -> MemoryItem:
        item = self._item(memory_id)
        rid = reader.id if reader else None
        if not self.can_read_memory(rid, item):
            from .bus import Forbidden
            raise Forbidden("you may not read this memory item")
        out = self._view(rid, item, full=True)
        if path:
            out.value, out.path = select_path(item.value, path), path
            out.tokens = estimate_tokens(out.value)
        return out

    def list_memory(self, reader: AgentRecord | None, *, include_values: bool = False,
                    max_tokens: int | None = None, **filters: Any) -> list[MemoryItem]:
        """Stubs by default. include_values=True inlines values while they fit max_tokens."""
        rid = reader.id if reader else None
        items = self.store.visible_memory(rid, **filters)
        out, spent = [], 0
        for it in items:
            v = self._view(rid, it, full=include_values and not it.deleted)
            if include_values and max_tokens is not None and v.value is not None:
                if spent + it.tokens > max_tokens:
                    v.value = None  # still listed as a stub; open it individually
                else:
                    spent += it.tokens
            out.append(v)
        return out

    def memory_stubs_for(self, reader_id: str | None, refs: list[str]) -> list[MemoryStub]:
        out = []
        for ref in refs:
            item = self.store.get_memory(ref)
            if not item or item.deleted:
                continue
            if self.can_read_memory(reader_id, item):
                out.append(self._view(reader_id, item, full=False).stub())
            else:
                out.append(MemoryStub(id=item.id, key="", owner=item.owner, summary="(not shared with you)",
                                      visibility=item.visibility, version=0, rev=0, tokens=0,
                                      updated_at=item.updated_at, accessible=False))
        return out

    # ------------------------------------------------- sharing via messages
    def check_memory_refs(self, sender_id: str | None, refs: list[str]) -> None:
        for ref in refs:
            item = self.store.get_memory(ref)
            if not item or item.deleted:
                from .bus import NotFound
                raise NotFound(f"no memory {ref!r}")
            if not self.can_read_memory(sender_id, item):
                from .bus import Forbidden
                raise Forbidden(f"you may not reference memory {ref!r}")
            if item.owner == sender_id and item.visibility == Visibility.private:
                raise GuardError("private_memory", f"memory {ref!r} is private; make it shared or public before "
                                 "referencing it in a message", 422)

    def grant_refs_to_thread(self, sender_id: str | None, env: Envelope) -> None:
        """Referencing your own shared item in a thread shares it with that thread's participants."""
        for ref in env.memory_refs:
            item = self.store.get_memory(ref)
            if item and item.owner == sender_id and item.visibility == Visibility.shared and env.thread_id not in item.threads:
                item.threads = sorted({*item.threads, env.thread_id})
                self.store.save_memory(item)
