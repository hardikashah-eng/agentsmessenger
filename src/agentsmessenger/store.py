"""SQLite persistence. One file, no external services."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import uuid
from datetime import datetime
from typing import Any, Iterable

from .models import AgentCard, AgentRecord, Envelope, EnvelopeIn, MemoryItem, Thread, ThreadState, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
  id TEXT PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  card TEXT NOT NULL,
  token_hash TEXT NOT NULL,
  paused INTEGER NOT NULL DEFAULT 0,
  registered_at TEXT NOT NULL,
  last_seen TEXT
);
CREATE TABLE IF NOT EXISTS capabilities (
  agent_id TEXT NOT NULL,
  capability TEXT NOT NULL,
  PRIMARY KEY (agent_id, capability)
);
CREATE TABLE IF NOT EXISTS threads (
  id TEXT PRIMARY KEY,
  data TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS participants (
  thread_id TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  PRIMARY KEY (thread_id, agent_id)
);
CREATE TABLE IF NOT EXISTS messages (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id TEXT NOT NULL,
  sender TEXT NOT NULL,
  to_agent TEXT,
  to_capability TEXT,
  idem_key TEXT,
  data TEXT NOT NULL,
  UNIQUE (sender, idem_key)
);
CREATE INDEX IF NOT EXISTS ix_messages_thread ON messages(thread_id, seq);
CREATE INDEX IF NOT EXISTS ix_messages_agent ON messages(to_agent, seq);
CREATE INDEX IF NOT EXISTS ix_messages_cap ON messages(to_capability, seq);
CREATE TABLE IF NOT EXISTS memory (
  id TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  key TEXT NOT NULL,
  visibility TEXT NOT NULL,
  rev INTEGER NOT NULL,
  deleted INTEGER NOT NULL DEFAULT 0,
  data TEXT NOT NULL,
  UNIQUE (owner, key)
);
CREATE INDEX IF NOT EXISTS ix_memory_rev ON memory(rev);
CREATE TABLE IF NOT EXISTS memory_grants (
  memory_id TEXT NOT NULL,
  kind TEXT NOT NULL,      -- agent | thread
  target TEXT NOT NULL,
  PRIMARY KEY (memory_id, kind, target)
);
CREATE INDEX IF NOT EXISTS ix_grants_target ON memory_grants(kind, target);
CREATE TABLE IF NOT EXISTS agent_stats (
  agent_id TEXT PRIMARY KEY,
  assigned INTEGER NOT NULL DEFAULT 0,
  approved INTEGER NOT NULL DEFAULT 0,
  rejected INTEGER NOT NULL DEFAULT 0,
  failed INTEGER NOT NULL DEFAULT 0,
  timeouts INTEGER NOT NULL DEFAULT 0,
  reviews INTEGER NOT NULL DEFAULT 0,
  cost_usd REAL NOT NULL DEFAULT 0
);
"""

CREATE_MARKET = """
CREATE TABLE IF NOT EXISTS reputation (
  agent_id TEXT NOT NULL,
  kind TEXT NOT NULL,           -- type of work: code, writing, ...
  role TEXT NOT NULL,           -- worker | reviewer
  alpha REAL NOT NULL DEFAULT 1,
  beta REAL NOT NULL DEFAULT 1,
  tasks INTEGER NOT NULL DEFAULT 0,
  approved INTEGER NOT NULL DEFAULT 0,
  rejected INTEGER NOT NULL DEFAULT 0,
  failed INTEGER NOT NULL DEFAULT 0,
  earned_usd REAL NOT NULL DEFAULT 0,
  updated_at TEXT,
  PRIMARY KEY (agent_id, kind, role)
);
CREATE TABLE IF NOT EXISTS ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  thread_id TEXT,
  account TEXT NOT NULL,
  delta_usd REAL NOT NULL,
  memo TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ledger_account ON ledger(account, id);
"""
REP_COUNTERS = ("tasks", "approved", "rejected", "failed", "earned_usd")

STAT_FIELDS = ("assigned", "approved", "rejected", "failed", "timeouts", "reviews", "cost_usd")
ACTIVE_STATES = ("claimed", "working", "verifying")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _dt(v: str | None) -> datetime | None:
    return datetime.fromisoformat(v) if v else None


class _Result:
    def __init__(self, rows: list[Any], lastrowid: int | None) -> None:
        self._rows, self.lastrowid = rows, lastrowid

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[Any]:
        return self._rows

    def __iter__(self):
        return iter(self._rows)


class _LockedConn:
    """One SQLite connection shared across server threads, serialized by a lock."""

    def __init__(self, path: str, lock: threading.RLock) -> None:
        self._c = sqlite3.connect(path, check_same_thread=False)
        self._c.row_factory = sqlite3.Row
        self._lock = lock

    def execute(self, sql: str, params: Any = ()) -> _Result:
        with self._lock:
            cur = self._c.execute(sql, params)
            return _Result(cur.fetchall(), cur.lastrowid)

    def executemany(self, sql: str, seq: Any) -> None:
        with self._lock:
            self._c.executemany(sql, seq)

    def executescript(self, sql: str) -> None:
        with self._lock:
            self._c.executescript(sql)

    def __enter__(self) -> "_LockedConn":
        self._lock.acquire()
        return self

    def __exit__(self, exc_type: Any, *_: Any) -> None:
        try:
            if exc_type is None:
                self._c.commit()
            else:
                self._c.rollback()
        finally:
            self._lock.release()


class Store:
    def __init__(self, path: str = ":memory:") -> None:
        self._lock = threading.RLock()
        # Held by the bus for a whole state change, so readers never see one half-done
        # (e.g. a routed message before its routing note).
        self.lock = self._lock
        self._db = _LockedConn(path, self._lock)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._db.executescript(CREATE_MARKET)
        self.last_seq = self.max_seq()  # lets streams skip queries when nothing is new

    # ------------------------------------------------------------- agents
    def register_agent(self, card: AgentCard) -> tuple[AgentRecord, str]:
        token = "am_" + secrets.token_urlsafe(32)
        agent_id = "agt_" + uuid.uuid4().hex[:12]
        now = utcnow()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO agents(id, name, card, token_hash, registered_at) VALUES (?,?,?,?,?)",
                (agent_id, card.name, card.model_dump_json(), _hash(token), now.isoformat()),
            )
            self._db.executemany(
                "INSERT INTO capabilities(agent_id, capability) VALUES (?,?)",
                [(agent_id, c) for c in card.capabilities],
            )
        return self.get_agent(agent_id), token  # type: ignore[return-value]

    def update_card(self, agent_id: str, card: AgentCard) -> AgentRecord:
        with self._lock, self._db:
            self._db.execute("UPDATE agents SET card=?, name=? WHERE id=?", (card.model_dump_json(), card.name, agent_id))
            self._db.execute("DELETE FROM capabilities WHERE agent_id=?", (agent_id,))
            self._db.executemany(
                "INSERT INTO capabilities(agent_id, capability) VALUES (?,?)",
                [(agent_id, c) for c in card.capabilities],
            )
        return self.get_agent(agent_id)  # type: ignore[return-value]

    def _agent_from_row(self, r: sqlite3.Row) -> AgentRecord:
        card = json.loads(r["card"])
        return AgentRecord(
            **card,
            id=r["id"],
            paused=bool(r["paused"]),
            registered_at=_dt(r["registered_at"]),  # type: ignore[arg-type]
            last_seen=_dt(r["last_seen"]),
        )

    def get_agent(self, agent_id: str) -> AgentRecord | None:
        r = self._db.execute("SELECT * FROM agents WHERE id=? OR name=?", (agent_id, agent_id)).fetchone()
        return self._agent_from_row(r) if r else None

    def agent_by_token(self, token: str) -> AgentRecord | None:
        r = self._db.execute("SELECT * FROM agents WHERE token_hash=?", (_hash(token),)).fetchone()
        return self._agent_from_row(r) if r else None

    def touch(self, agent_id: str) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE agents SET last_seen=? WHERE id=?", (utcnow().isoformat(), agent_id))

    def set_agent_paused(self, agent_id: str, paused: bool) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE agents SET paused=? WHERE id=?", (int(paused), agent_id))

    def list_agents(self, capability: str | None = None) -> list[AgentRecord]:
        if capability:
            rows = self._db.execute(
                "SELECT a.* FROM agents a JOIN capabilities c ON c.agent_id=a.id WHERE c.capability=? ORDER BY a.name",
                (capability,),
            ).fetchall()
        else:
            rows = self._db.execute("SELECT * FROM agents ORDER BY name").fetchall()
        return [self._agent_from_row(r) for r in rows]

    def capabilities_of(self, agent_id: str) -> list[str]:
        return [r[0] for r in self._db.execute("SELECT capability FROM capabilities WHERE agent_id=?", (agent_id,))]

    # ------------------------------------------------------------ threads
    def create_thread(self, *, subject: str, creator: str, capability: str | None,
                      budget_usd: float | None, deadline: datetime | None) -> Thread:
        now = utcnow()
        t = Thread(
            id="thr_" + uuid.uuid4().hex[:12], subject=subject, state=ThreadState.open,
            creator=creator, capability=capability, participants=[creator],
            budget_usd=budget_usd, created_at=now, updated_at=now, deadline=deadline,
        )
        with self._lock, self._db:
            self._db.execute("INSERT INTO threads(id, data, updated_at) VALUES (?,?,?)",
                             (t.id, t.model_dump_json(), now.isoformat()))
            self._db.execute("INSERT OR IGNORE INTO participants VALUES (?,?)", (t.id, creator))
        return t

    def get_thread(self, thread_id: str) -> Thread | None:
        r = self._db.execute("SELECT data FROM threads WHERE id=?", (thread_id,)).fetchone()
        return Thread.model_validate_json(r[0]) if r else None

    def save_thread(self, t: Thread) -> Thread:
        t.updated_at = utcnow()
        with self._lock, self._db:
            self._db.execute("UPDATE threads SET data=?, updated_at=? WHERE id=?",
                             (t.model_dump_json(), t.updated_at.isoformat(), t.id))
        return t

    def add_participant(self, t: Thread, agent_id: str) -> Thread:
        if agent_id not in t.participants:
            t.participants.append(agent_id)
            with self._lock, self._db:
                self._db.execute("INSERT OR IGNORE INTO participants VALUES (?,?)", (t.id, agent_id))
            self.save_thread(t)
        return t

    def list_threads(self, limit: int = 100) -> list[Thread]:
        rows = self._db.execute("SELECT data FROM threads ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [Thread.model_validate_json(r[0]) for r in rows]

    # ----------------------------------------------------------- messages
    def find_idempotent(self, sender: str, key: str) -> Envelope | None:
        r = self._db.execute("SELECT seq, data FROM messages WHERE sender=? AND idem_key=?", (sender, key)).fetchone()
        return self._env(r) if r else None

    def append(self, env_in: EnvelopeIn, *, thread_id: str, sender: str, sender_kind: str,
               verified: bool = False, signer: Any = None, to_agent_id: str | None = None) -> Envelope:
        data: dict[str, Any] = env_in.model_dump(mode="json")
        data.update(thread_id=thread_id, sender=sender, sender_kind=sender_kind,
                    created_at=utcnow().isoformat(), verified=verified)
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO messages(thread_id, sender, to_agent, to_capability, idem_key, data) VALUES (?,?,?,?,?,?)",
                (thread_id, sender, to_agent_id or env_in.to.agent, env_in.to.capability, env_in.idempotency_key, json.dumps(data)),
            )
            seq = cur.lastrowid
            env = Envelope(**data, seq=seq)
            if signer is not None:
                env.server_sig = signer.sign(env)
                self._db.execute("UPDATE messages SET data=? WHERE seq=?",
                                 (json.dumps(env.model_dump(mode="json", exclude={"seq"})), seq))
            self.last_seq = max(self.last_seq, seq or 0)
        return env

    def max_seq(self) -> int:
        r = self._db.execute("SELECT COALESCE(MAX(seq), 0) FROM messages").fetchone()
        return int(r[0])

    @staticmethod
    def _env(r: sqlite3.Row | tuple) -> Envelope:
        seq, data = r[0], r[1]
        return Envelope(**json.loads(data), seq=seq)

    def thread_messages(self, thread_id: str, after: int = 0, limit: int = 500) -> list[Envelope]:
        rows = self._db.execute(
            "SELECT seq, data FROM messages WHERE thread_id=? AND seq>? ORDER BY seq LIMIT ?",
            (thread_id, after, limit),
        ).fetchall()
        return [self._env(r) for r in rows]

    def get_message(self, seq: int) -> Envelope | None:
        r = self._db.execute("SELECT seq, data FROM messages WHERE seq=?", (seq,)).fetchone()
        return self._env(r) if r else None

    def last_messages(self, thread_id: str, n: int) -> list[Envelope]:
        rows = self._db.execute(
            "SELECT seq, data FROM messages WHERE thread_id=? ORDER BY seq DESC LIMIT ?", (thread_id, n)
        ).fetchall()
        return [self._env(r) for r in reversed(rows)]

    def inbox(self, agent_id: str, after: int, limit: int = 100) -> list[Envelope]:
        """Messages for this agent: addressed to it, to one of its capabilities
        (while the thread is unclaimed or claimed by it), or posted into a
        thread it participates in. Its own messages are excluded."""
        caps = self.capabilities_of(agent_id)
        cap_clause = f" OR m.to_capability IN ({','.join('?' * len(caps))})" if caps else ""
        rows = self._db.execute(
            f"""
            SELECT m.seq, m.data FROM messages m
            WHERE m.sender != ?
              AND (m.to_agent = ?{cap_clause}
                   OR m.thread_id IN (SELECT thread_id FROM participants WHERE agent_id = ?))
              AND m.seq > ?
            ORDER BY m.seq LIMIT ?
            """,
            [agent_id, agent_id, *caps, agent_id, after, limit],
        ).fetchall()
        out = []
        for r in rows:
            env = self._env(r)
            # Capability broadcasts stop going to others once someone else owns the thread.
            if env.to.capability and env.to.agent is None:
                t = self.get_thread(env.thread_id)
                taken_by = t.owner or t.assigned_to if t else None
                if t and taken_by and taken_by != agent_id and agent_id not in t.participants:
                    continue
            out.append(env)
        return out

    def messages_since(self, after: int, limit: int = 200) -> Iterable[Envelope]:
        rows = self._db.execute("SELECT seq, data FROM messages WHERE seq>? ORDER BY seq LIMIT ?", (after, limit)).fetchall()
        return [self._env(r) for r in rows]

    # ------------------------------------------------------------- memory
    def _mem(self, r: sqlite3.Row) -> MemoryItem:
        item = MemoryItem.model_validate_json(r["data"])
        grants = self._db.execute("SELECT kind, target FROM memory_grants WHERE memory_id=? ORDER BY target",
                                  (item.id,)).fetchall()
        item.shared_with = [g[1] for g in grants if g[0] == "agent"]
        item.threads = [g[1] for g in grants if g[0] == "thread"]
        return item

    def get_memory(self, memory_id: str) -> MemoryItem | None:
        r = self._db.execute("SELECT * FROM memory WHERE id=?", (memory_id,)).fetchone()
        return self._mem(r) if r else None

    def memory_by_key(self, owner: str, key: str) -> MemoryItem | None:
        r = self._db.execute("SELECT * FROM memory WHERE owner=? AND key=?", (owner, key)).fetchone()
        return self._mem(r) if r else None

    def count_memory(self, owner: str) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM memory WHERE owner=? AND deleted=0", (owner,)).fetchone()[0])

    def save_memory(self, item: MemoryItem) -> MemoryItem:
        """Insert or replace, bump the global revision, and rewrite grants."""
        with self._lock, self._db:
            rev = int(self._db.execute("SELECT COALESCE(MAX(rev), 0) + 1 FROM memory").fetchone()[0])
            item.rev = rev
            data = item.model_dump_json(exclude={"shared_with", "threads"})
            self._db.execute(
                "INSERT INTO memory(id, owner, key, visibility, rev, deleted, data) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET visibility=excluded.visibility, rev=excluded.rev, "
                "deleted=excluded.deleted, data=excluded.data",
                (item.id, item.owner, item.key, item.visibility.value, rev, int(item.deleted), data),
            )
            self._db.execute("DELETE FROM memory_grants WHERE memory_id=?", (item.id,))
            self._db.executemany("INSERT OR IGNORE INTO memory_grants VALUES (?,?,?)",
                                 [(item.id, "agent", a) for a in item.shared_with]
                                 + [(item.id, "thread", t) for t in item.threads])
        return item

    def visible_memory(self, reader: str | None, *, q: str | None = None, tag: str | None = None,
                       visibility: str | None = None, owner: str | None = None, thread_id: str | None = None,
                       since: int | None = None, limit: int = 50) -> list[MemoryItem]:
        """Items `reader` may read (reader=None is the observer: everything).
        Deleted items only appear as tombstones when `since` is given."""
        where, params = [], []
        if reader is not None:
            where.append(
                "(m.owner = ? OR m.visibility = 'public' OR (m.visibility = 'shared' AND m.id IN ("
                " SELECT memory_id FROM memory_grants WHERE (kind='agent' AND target=?)"
                " OR (kind='thread' AND target IN (SELECT thread_id FROM participants WHERE agent_id=?)))))")
            params += [reader, reader, reader]
        if thread_id:
            where.append("m.id IN (SELECT memory_id FROM memory_grants WHERE kind='thread' AND target=?)")
            params.append(thread_id)
        if visibility:
            where.append("m.visibility = ?")
            params.append(visibility)
        if owner:
            where.append("m.owner = ?")
            params.append(owner)
        if since is not None:
            where.append("m.rev > ?")
            params.append(since)
        else:
            where.append("m.deleted = 0")
        if q:
            where.append("(m.key LIKE ? OR json_extract(m.data, '$.summary') LIKE ?)")
            params += [f"%{q}%", f"%{q}%"]
        if tag:
            where.append("EXISTS (SELECT 1 FROM json_each(json_extract(m.data, '$.tags')) WHERE value = ?)")
            params.append(tag)
        sql = "SELECT m.* FROM memory m" + (" WHERE " + " AND ".join(where) if where else "")
        sql += " ORDER BY m.rev " + ("ASC" if since is not None else "DESC") + " LIMIT ?"
        return [self._mem(r) for r in self._db.execute(sql, [*params, limit]).fetchall()]

    def is_participant(self, thread_id: str, agent_id: str) -> bool:
        return self._db.execute("SELECT 1 FROM participants WHERE thread_id=? AND agent_id=?",
                                (thread_id, agent_id)).fetchone() is not None

    # -------------------------------------------------------- agent stats
    def bump_stats(self, agent_id: str, **deltas: float) -> None:
        """Add to an agent's track record (used by the router)."""
        bad = set(deltas) - set(STAT_FIELDS)
        if bad:
            raise ValueError(f"unknown stats: {bad}")
        sets = ", ".join(f"{k} = {k} + ?" for k in deltas)
        with self._lock, self._db:
            self._db.execute("INSERT OR IGNORE INTO agent_stats(agent_id) VALUES (?)", (agent_id,))
            self._db.execute(f"UPDATE agent_stats SET {sets} WHERE agent_id = ?", [*deltas.values(), agent_id])

    def stats(self, agent_id: str) -> dict[str, float]:
        r = self._db.execute("SELECT * FROM agent_stats WHERE agent_id=?", (agent_id,)).fetchone()
        return {k: (r[k] if r else 0) for k in STAT_FIELDS}

    def load(self, agent_id: str) -> int:
        """Threads this agent is assigned to or working on right now."""
        r = self._db.execute(
            "SELECT COUNT(*) FROM threads WHERE json_extract(data, '$.state') IN (?,?,?,?) AND "
            "(json_extract(data, '$.owner') = ? OR (json_extract(data, '$.assigned_to') = ? "
            " AND json_extract(data, '$.owner') IS NULL))",
            ("open", *ACTIVE_STATES, agent_id, agent_id),
        ).fetchone()
        return int(r[0])

    def threads_in_states(self, states: Iterable[str], limit: int = 500) -> list[Thread]:
        states = list(states)
        rows = self._db.execute(
            f"SELECT data FROM threads WHERE json_extract(data, '$.state') IN ({','.join('?' * len(states))}) "
            "ORDER BY updated_at LIMIT ?", [*states, limit]).fetchall()
        return [Thread.model_validate_json(r[0]) for r in rows]

    # --------------------------------------------------------- reputation
    def reputation(self, agent_id: str, kind: str, role: str = "worker") -> dict[str, Any]:
        r = self._db.execute("SELECT * FROM reputation WHERE agent_id=? AND kind=? AND role=?",
                             (agent_id, kind, role)).fetchone()
        if not r:
            return {"agent_id": agent_id, "kind": kind, "role": role, "alpha": 1.0, "beta": 1.0,
                    **{k: 0 for k in REP_COUNTERS}, "updated_at": None}
        return dict(r)

    def update_reputation(self, agent_id: str, kind: str, role: str, *, good: float = 0.0, bad: float = 0.0,
                          decay: float = 1.0, **counters: float) -> dict[str, Any]:
        """Decay the old evidence toward the neutral prior, then add the new evidence."""
        with self._lock, self._db:
            cur = self.reputation(agent_id, kind, role)
            alpha = 1 + (cur["alpha"] - 1) * decay + good
            beta = 1 + (cur["beta"] - 1) * decay + bad
            vals = {k: cur[k] + counters.get(k, 0) for k in REP_COUNTERS}
            self._db.execute(
                "INSERT INTO reputation(agent_id, kind, role, alpha, beta, tasks, approved, rejected, failed, earned_usd,"
                " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(agent_id, kind, role) DO UPDATE SET "
                "alpha=excluded.alpha, beta=excluded.beta, tasks=excluded.tasks, approved=excluded.approved, "
                "rejected=excluded.rejected, failed=excluded.failed, earned_usd=excluded.earned_usd, "
                "updated_at=excluded.updated_at",
                (agent_id, kind, role, alpha, beta, *vals.values(), utcnow().isoformat()))
        return self.reputation(agent_id, kind, role)

    def all_reputation(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._db.execute("SELECT * FROM reputation ORDER BY kind, role").fetchall()]

    # ------------------------------------------------------------- ledger
    def post_ledger(self, entries: list[tuple[str, float, str]], thread_id: str | None) -> None:
        """Entries are (account, delta_usd, memo); written together."""
        now = utcnow().isoformat()
        with self._lock, self._db:
            self._db.executemany("INSERT INTO ledger(at, thread_id, account, delta_usd, memo) VALUES (?,?,?,?,?)",
                                 [(now, thread_id, a, d, m) for a, d, m in entries])

    def ledger(self, account: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        if account:
            rows = self._db.execute("SELECT * FROM ledger WHERE account=? ORDER BY id DESC LIMIT ?", (account, limit))
        else:
            rows = self._db.execute("SELECT * FROM ledger ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows.fetchall()]

    def balance(self, account: str) -> float:
        r = self._db.execute("SELECT COALESCE(SUM(delta_usd), 0) FROM ledger WHERE account=?", (account,)).fetchone()
        return round(float(r[0]), 6)
