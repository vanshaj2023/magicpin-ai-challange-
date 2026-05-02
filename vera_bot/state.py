"""In-memory state for the HTTP server: contexts, conversations, suppression."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Literal


Scope = Literal["category", "merchant", "customer", "trigger"]


@dataclass
class StoredContext:
    version: int
    payload: dict
    stored_at: float


@dataclass
class ConversationTurn:
    ts: float
    from_role: Literal["bot", "merchant", "customer"]
    body: str


@dataclass
class ConversationState:
    conversation_id: str
    merchant_id: str
    customer_id: str | None
    trigger_id: str | None
    send_as: str
    turns: list[ConversationTurn] = field(default_factory=list)
    auto_reply_count: int = 0
    nudge_count: int = 0
    status: Literal["active", "waiting", "ended"] = "active"
    last_bot_body: str = ""

    def add_turn(self, role: str, body: str) -> None:
        self.turns.append(ConversationTurn(time.time(), role, body))  # type: ignore[arg-type]


class Store:
    """Thread-safe state store. Single global instance per process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._contexts: dict[tuple[Scope, str], StoredContext] = {}
        self._conversations: dict[str, ConversationState] = {}
        self._suppression: dict[str, float] = {}  # key -> expires_at unix ts
        self._started_at = time.time()

    # ----- contexts ---------------------------------------------------------

    def upsert_context(self, scope: Scope, context_id: str, version: int, payload: dict) -> tuple[bool, int]:
        """Returns (accepted, current_version). Higher version replaces lower."""
        with self._lock:
            key = (scope, context_id)
            existing = self._contexts.get(key)
            if existing and existing.version >= version:
                return (existing.version == version), existing.version
            self._contexts[key] = StoredContext(version=version, payload=payload, stored_at=time.time())
            return True, version

    def get_context(self, scope: Scope, context_id: str) -> dict | None:
        with self._lock:
            ctx = self._contexts.get((scope, context_id))
            return ctx.payload if ctx else None

    def context_counts(self) -> dict[str, int]:
        with self._lock:
            counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
            for (scope, _id) in self._contexts:
                counts[scope] = counts.get(scope, 0) + 1
            return counts

    def all_triggers(self) -> list[dict]:
        with self._lock:
            return [c.payload for (s, _), c in self._contexts.items() if s == "trigger"]

    # ----- conversations ----------------------------------------------------

    def get_or_create_conversation(
        self,
        conversation_id: str,
        *,
        merchant_id: str,
        customer_id: str | None,
        trigger_id: str | None,
        send_as: str,
    ) -> ConversationState:
        with self._lock:
            conv = self._conversations.get(conversation_id)
            if conv is None:
                conv = ConversationState(
                    conversation_id=conversation_id,
                    merchant_id=merchant_id,
                    customer_id=customer_id,
                    trigger_id=trigger_id,
                    send_as=send_as,
                )
                self._conversations[conversation_id] = conv
            return conv

    def get_conversation(self, conversation_id: str) -> ConversationState | None:
        with self._lock:
            return self._conversations.get(conversation_id)

    # ----- suppression ------------------------------------------------------

    def is_suppressed(self, key: str) -> bool:
        if not key:
            return False
        now = time.time()
        with self._lock:
            exp = self._suppression.get(key)
            if exp is None:
                return False
            if exp < now:
                self._suppression.pop(key, None)
                return False
            return True

    def suppress(self, key: str, ttl_seconds: int = 3600 * 24) -> None:
        if not key:
            return
        with self._lock:
            self._suppression[key] = time.time() + ttl_seconds

    # ----- meta -------------------------------------------------------------

    def uptime_seconds(self) -> int:
        return int(time.time() - self._started_at)


_store: Store | None = None


def get_store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store


def reset_store_for_tests() -> None:
    global _store
    _store = Store()
