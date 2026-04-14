"""Thread-safe shared state for the multi-agent game."""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Union


@dataclass
class ChatMessage:
    sender: str
    recipient: str
    text: str
    timestamp: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


@dataclass
class GameEvent:
    event_type: str
    payload: Any
    timestamp: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class SharedState:
    """
    Central state shared across all Streamlit and background AI threads.

    All mutations are protected by a single RLock so concurrent score
    updates, message sends, and broadcasts never race.

    Parameters
    ----------
    player_ids:
        List of player identifiers to register at construction time,
        e.g. ["ai_0", "ai_1", "ai_2"].
    initial_score:
        Starting score for every player (default 0).
    initial_send_budget:
        Number of messages each player may send before the budget hits 0
        (default 10).  Pass None for unlimited.
    """

    def __init__(
        self,
        player_ids: List[str],
        initial_score: int = 0,
        initial_send_budget: int | None = 10,
    ) -> None:
        self._lock = threading.RLock()
        self._scores: Dict[str, int] = {pid: initial_score for pid in player_ids}
        self._send_budgets: Dict[str, int | None] = {
            pid: initial_send_budget for pid in player_ids
        }
        # Each player gets their own inbox queue.
        self._inboxes: Dict[str, queue.Queue] = {
            pid: queue.Queue() for pid in player_ids
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_player(self, player_id: str) -> None:
        if player_id not in self._inboxes:
            raise KeyError(f"Unknown player: {player_id!r}")

    # ------------------------------------------------------------------
    # Messaging
    # ------------------------------------------------------------------

    def send_message(self, sender: str, recipient: str, text: str) -> None:
        """
        Deliver a ChatMessage to *recipient*'s inbox and deduct one unit
        from *sender*'s send_budget.

        Raises
        ------
        KeyError
            If sender or recipient is not a registered player.
        RuntimeError
            If sender has exhausted their send_budget.
        """
        with self._lock:
            self._require_player(sender)
            self._require_player(recipient)

            budget = self._send_budgets[sender]
            if budget is not None:
                if budget <= 0:
                    raise RuntimeError(
                        f"Player {sender!r} has no remaining send_budget."
                    )
                self._send_budgets[sender] = budget - 1

            msg = ChatMessage(sender=sender, recipient=recipient, text=text)
            self._inboxes[recipient].put(msg)

    def get_my_messages(self, player_id: str) -> List[Union[ChatMessage, GameEvent]]:
        """
        Drain and return all items currently queued in *player_id*'s inbox.
        Does NOT deduct send_budget.
        """
        with self._lock:
            self._require_player(player_id)
            inbox = self._inboxes[player_id]

        messages: List[Union[ChatMessage, GameEvent]] = []
        # Drain outside the main lock — queue.Queue is thread-safe internally.
        while True:
            try:
                messages.append(inbox.get_nowait())
            except queue.Empty:
                break
        return messages

    def broadcast_event(self, event: GameEvent) -> None:
        """Put *event* into every registered player's inbox."""
        with self._lock:
            inboxes = list(self._inboxes.values())
        for inbox in inboxes:
            inbox.put(event)

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------

    def get_score(self, player_id: str) -> int:
        with self._lock:
            self._require_player(player_id)
            return self._scores[player_id]

    def update_score(self, player_id: str, delta: int) -> int:
        """
        Thread-safely add *delta* to *player_id*'s score.

        Returns the new score.
        """
        with self._lock:
            self._require_player(player_id)
            self._scores[player_id] += delta
            return self._scores[player_id]

    # ------------------------------------------------------------------
    # Budget
    # ------------------------------------------------------------------

    def get_send_budget(self, player_id: str) -> int | None:
        with self._lock:
            self._require_player(player_id)
            return self._send_budgets[player_id]

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def player_ids(self) -> List[str]:
        with self._lock:
            return list(self._inboxes.keys())
