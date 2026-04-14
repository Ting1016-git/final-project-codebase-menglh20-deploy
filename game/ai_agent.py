"""AIAgent — autonomous AI player running as a daemon thread.

Each agent runs a tight behavior loop:
  1. Drain inbox  →  reply to any ChatMessage
  2. Proactive action  →  initiate chat based on personality probability
  3. Sleep 1–3 s  →  repeat

Strategic game decisions (Poison Bottle, guessing) are triggered externally
via GameEvent objects placed in the agent's inbox by the GameEngine.
"""

from __future__ import annotations

import random
import threading
import time
import types
from typing import TYPE_CHECKING

from game.shared_state import ChatMessage, GameEvent

if TYPE_CHECKING:
    from game.shared_state import SharedState

# Maximum number of (role, content) pairs kept as rolling context per agent.
_MAX_CONTEXT_PAIRS = 10


class AIAgent(threading.Thread):
    """Autonomous AI player.

    Parameters
    ----------
    player_id:
        Must match one of the IDs registered in *state* (e.g. "ai_0").
    persona:
        A module or object exposing: NAME, EMOJI, SYSTEM_PROMPT,
        DEFAULT_REPLY, CHAT_INITIATIVE_PROB.
    state:
        The shared SharedState instance.
    """

    def __init__(
        self,
        player_id: str,
        persona: types.ModuleType,
        state: "SharedState",
    ) -> None:
        super().__init__(daemon=True, name=f"AIAgent-{player_id}")
        self.player_id = player_id
        self.persona = persona
        self.state = state
        self._stop_event = threading.Event()
        # Rolling chat context: list of {"role": ..., "content": ...} dicts.
        self._context: list[dict[str, str]] = []

    # ------------------------------------------------------------------
    # Thread lifecycle
    # ------------------------------------------------------------------

    def stop(self) -> None:
        """Signal the agent to exit its run loop after the current tick."""
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.is_set():
            self._tick()
            time.sleep(random.uniform(1.0, 3.0))

    # ------------------------------------------------------------------
    # Main tick
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        # 1. Process inbox.
        for item in self.state.get_my_messages(self.player_id):
            if isinstance(item, ChatMessage):
                self._handle_chat(item)
            elif isinstance(item, GameEvent):
                self._handle_event(item)

        # 2. Maybe take a proactive action if budget remains.
        if self._has_budget():
            if random.random() < self.persona.CHAT_INITIATIVE_PROB:
                self._proactive_chat()

    # ------------------------------------------------------------------
    # Inbox handlers
    # ------------------------------------------------------------------

    def _handle_chat(self, msg: ChatMessage) -> None:
        """Generate and send a reply to an incoming ChatMessage."""
        if not self._has_budget():
            return

        from prompts.templates import build_reply_prompt

        user_turn = build_reply_prompt(
            persona_name=self.persona.NAME,
            sender=msg.sender,
            incoming_text=msg.text,
            score=self.state.get_score(self.player_id),
            send_budget=self.state.get_send_budget(self.player_id),
        )

        reply = self._call_chat([{"role": "user", "content": user_turn}])
        self._append_context("user", user_turn)
        self._append_context("assistant", reply)

        try:
            self.state.send_message(self.player_id, msg.sender, reply)
        except RuntimeError:
            pass  # budget just ran out between check and send — drop silently

    def _handle_event(self, event: GameEvent) -> None:
        """React to a GameEvent (e.g. round_start, make_decision)."""
        if event.event_type == "make_decision":
            self._make_game_decision(event.payload or {})
        # Other event types can be handled here as the game engine grows.

    # ------------------------------------------------------------------
    # Proactive chat
    # ------------------------------------------------------------------

    def _proactive_chat(self) -> None:
        """Initiate an unprompted message to a random other player."""
        others = [p for p in self.state.player_ids if p != self.player_id]
        if not others:
            return
        target = random.choice(others)

        from prompts.templates import build_proactive_prompt

        user_turn = build_proactive_prompt(
            persona_name=self.persona.NAME,
            target=target,
            score=self.state.get_score(self.player_id),
            send_budget=self.state.get_send_budget(self.player_id),
        )

        text = self._call_chat([{"role": "user", "content": user_turn}])
        self._append_context("user", user_turn)
        self._append_context("assistant", text)

        try:
            self.state.send_message(self.player_id, target, text)
        except RuntimeError:
            pass

    # ------------------------------------------------------------------
    # Strategic decision
    # ------------------------------------------------------------------

    def _make_game_decision(self, situation: dict) -> None:
        """Use Sonnet to make a strategic in-game choice."""
        from game.llm_client import call_strategic
        from prompts.templates import build_game_decision_prompt

        user_turn = build_game_decision_prompt(
            persona_name=self.persona.NAME,
            game_type=situation.get("game_type", "未知游戏"),
            situation=situation,
            score=self.state.get_score(self.player_id),
            send_budget=self.state.get_send_budget(self.player_id),
        )

        decision = call_strategic(
            system_prompt=self._system_prompt(),
            messages=self._recent_context() + [{"role": "user", "content": user_turn}],
            default_reply=self.persona.DEFAULT_REPLY,
        )
        self._append_context("user", user_turn)
        self._append_context("assistant", decision)
        # The decision string is returned via the agent's own inbox so the
        # GameEngine can pick it up — post it as a special event reply.
        # (GameEngine reads it from a separate decision_queue if wired up.)

    # ------------------------------------------------------------------
    # Context management
    # ------------------------------------------------------------------

    def _build_context(self, incoming: ChatMessage | None = None) -> str:
        """Return a plain-text summary of current state for internal use."""
        score = self.state.get_score(self.player_id)
        budget = self.state.get_send_budget(self.player_id)
        budget_str = str(budget) if budget is not None else "无限制"
        ctx = (
            f"我是{self.persona.NAME}{self.persona.EMOJI}。"
            f"当前得分：{score}。剩余发言次数：{budget_str}。"
        )
        if incoming:
            ctx += f"\n收到来自 {incoming.sender} 的消息：\u201c{incoming.text}\u201d"
        return ctx

    def _recent_context(self) -> list[dict[str, str]]:
        """Return the most recent N message pairs for API context."""
        return self._context[-(2 * _MAX_CONTEXT_PAIRS):]

    def _append_context(self, role: str, content: str) -> None:
        self._context.append({"role": role, "content": content})
        # Trim to keep memory bounded.
        if len(self._context) > 2 * _MAX_CONTEXT_PAIRS + 4:
            self._context = self._context[-(2 * _MAX_CONTEXT_PAIRS):]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _system_prompt(self) -> str:
        return self.persona.SYSTEM_PROMPT

    def _has_budget(self) -> bool:
        budget = self.state.get_send_budget(self.player_id)
        return budget is None or budget > 0

    def _call_chat(self, extra_messages: list[dict[str, str]]) -> str:
        from game.llm_client import call_llm
        return call_llm(
            system_prompt=self._system_prompt(),
            messages=self._recent_context() + extra_messages,
            default_reply=self.persona.DEFAULT_REPLY,
        )
