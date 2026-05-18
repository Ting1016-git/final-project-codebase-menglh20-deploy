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
_HUMAN_ID = "human"
_RESERVED_REPLY_BUDGET = 3


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
        # 1. Process inbox (prioritise human chat over other chat/events).
        inbox_items = self.state.get_my_messages(self.player_id)
        chats = [i for i in inbox_items if isinstance(i, ChatMessage)]
        events = [i for i in inbox_items if isinstance(i, GameEvent)]
        chats.sort(key=lambda m: m.sender != _HUMAN_ID)

        for item in chats:
            self._handle_chat(item)
        for item in events:
            self._handle_event(item)

        # 2. Maybe take a proactive action if budget remains.
        if self._can_initiate_proactive():
            if random.random() < self.persona.CHAT_INITIATIVE_PROB:
                self._proactive_chat()

    # ------------------------------------------------------------------
    # Inbox handlers
    # ------------------------------------------------------------------

    def _handle_chat(self, msg: ChatMessage) -> None:
        """Generate and send a reply to an incoming ChatMessage."""
        if not self._has_budget():
            if msg.sender == _HUMAN_ID:
                # Human gets explicit feedback instead of silent drop.
                self.state.send_system_message(
                    sender=self.player_id,
                    recipient=_HUMAN_ID,
                    text=f"{self.persona.NAME}本回合发言次数已用完，下一回合会恢复。",
                )
            return

        from prompts.templates import build_reply_prompt

        user_turn = build_reply_prompt(
            persona_name=self.persona.NAME,
            sender=msg.sender,
            incoming_text=msg.text,
            score=self.state.get_score(self.player_id),
            send_budget=self.state.get_send_budget(self.player_id),
            sender_name=self._player_label(msg.sender),
        )

        reply = self._call_chat([{"role": "user", "content": user_turn}])
        self._append_context("user", user_turn)
        self._append_context("assistant", reply)

        try:
            self.state.send_message(self.player_id, msg.sender, reply)
        except RuntimeError:
            pass  # budget just ran out between check and send — drop silently

    def _handle_event(self, event: GameEvent) -> None:
        """React to a GameEvent delivered by the game engine."""
        if event.event_type != "make_decision":
            # phase_change / round_end / game_over — no action needed here.
            return

        payload = event.payload or {}
        action = payload.get("action", "")

        if action == "write_word":
            self._decide_write_word(payload)
        elif action == "guess_word":
            self._decide_guess_word(payload)
        elif action == "guess_authors":
            self._decide_guess_authors(payload)
        elif action == "choose_bottle":
            self._decide_choose_bottle(payload)
        else:
            # Unknown action — still call the LLM but don't submit a choice,
            # so legacy callers continue to work without crashing.
            self._make_game_decision(payload)

    # ------------------------------------------------------------------
    # Proactive chat
    # ------------------------------------------------------------------

    def _proactive_chat(self) -> None:
        """Initiate an unprompted message to a random other player."""
        others = [p for p in self.state.player_ids if p != self.player_id]
        if not others:
            return
        # Prefer human target when possible to keep chat meaningful.
        weights = [3 if pid == _HUMAN_ID else 1 for pid in others]
        target = random.choices(others, weights=weights, k=1)[0]

        from prompts.templates import build_proactive_prompt
        round_info = self.state.get_round_info()

        user_turn = build_proactive_prompt(
            persona_name=self.persona.NAME,
            target=target,
            score=self.state.get_score(self.player_id),
            send_budget=self.state.get_send_budget(self.player_id),
            target_name=self._player_label(target),
            current_round=round_info.get("current_round"),
            game_type=round_info.get("game_type"),
        )

        text = self._call_chat([{"role": "user", "content": user_turn}])
        if not text.strip() or text.strip() == self.persona.DEFAULT_REPLY:
            return
        self._append_context("user", user_turn)
        self._append_context("assistant", text)

        try:
            self.state.send_message(self.player_id, target, text)
        except RuntimeError:
            pass

    # ------------------------------------------------------------------
    # Action-specific decision handlers
    # ------------------------------------------------------------------

    def _decide_write_word(self, payload: dict) -> None:
        """Write a secret word (used in Guess the Word and Who Wrote It)."""
        import random
        situation = {
            **payload,
            "instruction": "请写一个中文词语（2-4个字）作为你的答案，只输出词语本身。",
        }
        raw = self._make_game_decision(situation)
        # Keep only the first whitespace-separated token as the word.
        word = raw.strip().split()[0] if raw.strip() else random.choice(
            ["苹果", "天空", "月亮", "星星"]
        )
        self.state.submit_choice(self.player_id, word)

    def _decide_guess_word(self, payload: dict) -> None:
        """Guess the writer's hidden word (Guess the Word)."""
        import random
        situation = {
            **payload,
            "instruction": "请猜测写词者写的是什么词（2-4个字），只输出词语本身。",
        }
        raw = self._make_game_decision(situation)
        word = raw.strip().split()[0] if raw.strip() else random.choice(
            ["苹果", "天空", "月亮", "星星"]
        )
        self.state.submit_choice(self.player_id, word)

    def _decide_guess_authors(self, payload: dict) -> None:
        """Guess who wrote each word in Who Wrote It.

        The payload contains:
        * ``words``             — ordered list of words to attribute
        * ``candidate_authors`` — ordered list of possible author IDs

        The agent returns a comma-separated list of guessed author IDs in the
        same positional order as ``words``, e.g. ``"ai_0,ai_1,ai_2"``.
        """
        words = payload.get("words", [])
        candidates = payload.get("candidate_authors", [])
        situation = {
            **payload,
            "instruction": (
                f"请猜测以下词语分别是谁写的。词语列表：{words}。"
                f"候选玩家：{candidates}。"
                f"请按词语顺序依次输出玩家ID，用英文逗号分隔，"
                f"例如：ai_0,ai_1,ai_2"
            ),
        }
        raw = self._make_game_decision(situation)
        self.state.submit_choice(self.player_id, raw.strip())

    def _decide_choose_bottle(self, payload: dict) -> None:
        """Choose a bottle in Poison Bottle."""
        import random
        available = payload.get("available_bottles", ["Red", "Blue", "Green", "Yellow"])
        situation = {
            **payload,
            "instruction": (
                f"请从以下瓶子中选一个：{available}。"
                f"只输出瓶子颜色（英文），例如：Red"
            ),
        }
        raw = self._make_game_decision(situation)
        choice = raw.strip().split()[0] if raw.strip() else ""
        if choice not in available:
            choice = random.choice(available)
        self.state.submit_choice(self.player_id, choice)

    # ------------------------------------------------------------------
    # Core LLM decision call
    # ------------------------------------------------------------------

    def _make_game_decision(self, situation: dict) -> str:
        """Call Sonnet to make a strategic in-game choice.

        Returns the raw decision string (≤ 40 chars).  Callers are responsible
        for parsing and submitting the result via ``state.submit_choice``.
        """
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
        return decision

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

    def _can_initiate_proactive(self) -> bool:
        """Use only surplus budget for proactive chat, reserve some for replies."""
        budget = self.state.get_send_budget(self.player_id)
        if budget is None:
            return True
        return budget > _RESERVED_REPLY_BUDGET

    def _player_label(self, player_id: str) -> str:
        """Resolve internal player IDs to human-readable names."""
        try:
            return self.state.get_display_name(player_id)
        except Exception:
            fallback = {
                "ai_0": "小白",
                "ai_1": "狐狸",
                "ai_2": "铁面",
                _HUMAN_ID: "你",
            }
            return fallback.get(player_id, player_id)

    def _call_chat(self, extra_messages: list[dict[str, str]]) -> str:
        from game.llm_client import call_llm
        return call_llm(
            system_prompt=self._system_prompt(),
            messages=self._recent_context() + extra_messages,
            default_reply=self.persona.DEFAULT_REPLY,
        )
