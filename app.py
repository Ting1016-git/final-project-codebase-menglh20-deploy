"""How to Fool AI — main Streamlit entry point.

Screens
-------
setup     → player enters name + round count, then clicks Start Game
game      → auto-refreshing game area (left) + chat panel (right)
game_over → final scores revealed, Play Again button

Thread model
------------
* GameEngine daemon thread — drives rounds and mini-game phases.
* Three AIAgent daemon threads — autonomous AI players.
* Streamlit main thread — this file.

All threads share a single SharedState instance stored in st.session_state.
Fragments refresh independently:
  _game_area_fragment  run_every=3 s — phase, round info, pending decisions
  _chat_fragment       run_every=2 s — chat history, send form, whispers
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Dict, List

import streamlit as st

from game.ai_agent import AIAgent
from game.game_engine import GameEngine
from game.shared_state import ChatMessage, GameEvent, SharedState
import prompts.bai as bai
import prompts.fox as fox
import prompts.ironface as ironface

# ── constants ─────────────────────────────────────────────────────────────────

HUMAN_ID = "human"
ALL_PLAYERS = [HUMAN_ID, "ai_0", "ai_1", "ai_2"]

# Display metadata for each AI opponent.
AI_META: Dict[str, Dict[str, str]] = {
    "ai_0": {
        "name": "小白",
        "emoji": "🐰",
        "trait": "Naive & Kind",
        "desc": "Trusting and chatty. Believes almost everything you tell them — easy to manipulate, but their enthusiasm makes them a wildcard.",
        "persona": bai,
    },
    "ai_1": {
        "name": "狐狸",
        "emoji": "🦊",
        "trait": "Cunning & Strategic",
        "desc": "Suspicious by default. Sets traps, cross-validates your claims, and strikes when you least expect it.",
        "persona": fox,
    },
    "ai_2": {
        "name": "铁面",
        "emoji": "🗿",
        "trait": "Cold & Rational",
        "desc": "Speaks only when necessary. Trusts almost no one. Their rare messages carry decisive weight.",
        "persona": ironface,
    },
}

GAME_TYPE_LABELS: Dict[str, str] = {
    "guess_the_word": "🔮 Guess the Word",
    "who_wrote_it": "🎭 Who Wrote It",
    "poison_bottle": "☠️ Poison Bottle",
}

PHASE_LABELS: Dict[str, str] = {
    "setup": "Setting up…",
    "playing": "▶ Playing",
    "reveal": "🔍 Reveal",
    "game_over": "🏁 Game Over",
}

ROUND_OPTIONS = [3, 5, 7, 10]


# ── session-state initialisation ─────────────────────────────────────────────

def _init_session() -> None:
    ss = st.session_state
    ss.setdefault("page", "setup")
    ss.setdefault("player_name", "Player")
    ss.setdefault("pending_decision", None)   # dict payload from make_decision event
    ss.setdefault("last_round_results", None) # dict payload from round_end event
    ss.setdefault("chat_rendered_up_to", 0)   # index into chat history already shown


# ── thread lifecycle ──────────────────────────────────────────────────────────

def _start_threads(state: SharedState, total_rounds: int) -> None:
    """Start GameEngine + three AIAgent daemon threads (idempotent)."""
    ss = st.session_state

    # Agents — one per AI player.
    if "agents" not in ss:
        agents: List[AIAgent] = []
        for pid, meta in AI_META.items():
            agent = AIAgent(
                player_id=pid,
                persona=meta["persona"],
                state=state,
            )
            agent.start()
            agents.append(agent)
        ss.agents = agents

    # Engine — starts the round loop.
    if "engine" not in ss:
        engine = GameEngine(state, total_rounds=total_rounds)
        engine.start()
        ss.engine = engine


def _stop_threads() -> None:
    """Signal all daemon threads to stop cleanly."""
    ss = st.session_state
    if engine := ss.pop("engine", None):
        engine.stop()
    for agent in ss.pop("agents", []):
        agent.stop()


# ── page: Setup ───────────────────────────────────────────────────────────────

def render_setup() -> None:
    st.set_page_config(
        page_title="How to Fool AI", page_icon="🎭", layout="centered"
    )

    st.title("🎭 How to Fool AI")
    st.markdown(
        "A social-deduction game where you face three AI opponents across "
        "a series of mini-games. Scores are hidden. Private messages may be lies. "
        "Every alliance is temporary."
    )

    st.divider()

    with st.form("setup_form"):
        player_name = st.text_input(
            "Your display name",
            value=st.session_state.player_name,
            max_chars=20,
            placeholder="Enter your name…",
        )
        total_rounds = st.select_slider(
            "Number of rounds",
            options=ROUND_OPTIONS,
            value=5,
        )
        st.markdown("&nbsp;")
        start = st.form_submit_button("🚀 Start Game", use_container_width=True)

    if start:
        if not player_name.strip():
            st.error("Please enter a display name.")
            return
        _launch_game(player_name.strip(), int(total_rounds))

    st.divider()
    st.subheader("Your opponents")
    cols = st.columns(3)
    for col, (pid, meta) in zip(cols, AI_META.items()):
        with col:
            st.markdown(f"### {meta['emoji']} {meta['name']}")
            st.caption(f"*{meta['trait']}*")
            st.write(meta["desc"])


def _launch_game(player_name: str, total_rounds: int) -> None:
    """Create SharedState, start threads, transition to game screen."""
    ss = st.session_state

    # Build a fresh SharedState for this game session.
    state = SharedState(
        ALL_PLAYERS,
        initial_score=0,
        initial_send_budget=10,
        human_player_id=HUMAN_ID,
    )
    ss.state = state
    ss.player_name = player_name
    ss.total_rounds = total_rounds
    ss.pending_decision = None
    ss.last_round_results = None
    ss.chat_rendered_up_to = 0

    # Discard any leftover threads from a previous game.
    _stop_threads()
    _start_threads(state, total_rounds)

    ss.page = "game"
    st.rerun()


# ── page: Game ────────────────────────────────────────────────────────────────

def render_game() -> None:
    st.set_page_config(
        page_title="How to Fool AI — Game", page_icon="🎭", layout="wide"
    )

    state: SharedState = st.session_state.state

    # Top bar: player info + own score (only own score visible per SPEC).
    score = state.get_score(HUMAN_ID)
    top_left, top_right = st.columns([3, 1])
    with top_left:
        st.markdown(f"### 🎭 How to Fool AI &nbsp; — &nbsp; {st.session_state.player_name}")
    with top_right:
        st.metric("Your score", score)

    st.divider()

    col_game, col_chat = st.columns([2, 1], gap="large")
    with col_game:
        _game_area_fragment()
    with col_chat:
        _chat_fragment()


@st.fragment(run_every=3)
def _game_area_fragment() -> None:
    """Auto-refreshes every 3 s: drains human inbox, shows round/phase/decision."""
    state: SharedState = st.session_state.state

    # ── drain human inbox ────────────────────────────────────────────────────
    for item in state.get_my_messages(HUMAN_ID):
        if not isinstance(item, GameEvent):
            continue
        etype = item.event_type
        if etype == "make_decision":
            st.session_state.pending_decision = item.payload
        elif etype == "round_end":
            st.session_state.last_round_results = item.payload
            # Clear pending decision at the end of each round.
            st.session_state.pending_decision = None
        elif etype == "phase_change":
            # Clear pending decision when entering reveal phase.
            if item.payload.get("phase") == "reveal":
                st.session_state.pending_decision = None
        elif etype == "game_over":
            st.session_state.page = "game_over"
            st.rerun()
            return

    # ── phase / round header ─────────────────────────────────────────────────
    phase = state.get_phase()
    if phase == "game_over":
        st.session_state.page = "game_over"
        st.rerun()
        return

    info = state.get_round_info()
    current = info["current_round"]
    total = info["total_rounds"]
    game_type = info["game_type"]

    if current == 0:
        st.info("⏳ Game is starting…")
        return

    phase_label = PHASE_LABELS.get(phase, phase)
    game_label = GAME_TYPE_LABELS.get(game_type, game_type)

    col_r, col_p, col_g = st.columns(3)
    col_r.metric("Round", f"{current} / {total}")
    col_p.metric("Phase", phase_label)
    col_g.metric("Mini-game", game_label)

    st.divider()

    # ── reveal: show last round results ──────────────────────────────────────
    if phase == "reveal" and st.session_state.last_round_results:
        _render_round_results(st.session_state.last_round_results)
        return

    # ── playing: show pending decision or waiting message ────────────────────
    if phase == "playing":
        decision = st.session_state.pending_decision
        if decision:
            _render_decision_ui(decision, state)
        else:
            st.info("💬 Use the chat panel to exchange messages while waiting for your turn.")


def _render_round_results(results: dict) -> None:
    """Show results after a round ends (reveal phase)."""
    game_type = results.get("game_type", "")
    st.subheader("🔍 Round Results")

    if game_type == "guess_the_word":
        writer = results.get("writer", "?")
        word = results.get("word", "?")
        writer_name = _player_display(writer)
        st.write(f"**Writer:** {writer_name}  |  **Word:** `{word}`")
        st.write("**Guesses:**")
        for pid, guess in results.get("guesses", {}).items():
            correct = guess.strip().lower() == word.strip().lower()
            icon = "✅" if correct else "❌"
            st.write(f"  {icon} {_player_display(pid)}: `{guess}`")

    elif game_type == "who_wrote_it":
        st.write("**Words written:**")
        for pid, word in results.get("words", {}).items():
            st.write(f"  {_player_display(pid)}: `{word}`")

    elif game_type == "poison_bottle":
        poisoned = results.get("poisoned_bottle", "?")
        st.write(f"**Poisoned bottle:** {poisoned}")
        for pid, bottle in results.get("choices", {}).items():
            poisoned_flag = " ☠️" if bottle.lower() == poisoned.lower() else ""
            st.write(f"  {_player_display(pid)}: **{bottle}**{poisoned_flag}")

    # Score deltas
    deltas = results.get("score_deltas", {})
    if any(d != 0 for d in deltas.values()):
        st.write("**Score changes this round:**")
        for pid, delta in deltas.items():
            if delta != 0:
                sign = "+" if delta > 0 else ""
                st.write(f"  {_player_display(pid)}: {sign}{delta}")


def _render_decision_ui(decision: dict, state: SharedState) -> None:
    """Show the appropriate choice widget based on the pending decision action."""
    action = decision.get("action", "")
    game_type = decision.get("game_type", "")

    st.subheader(f"Your turn — {GAME_TYPE_LABELS.get(game_type, game_type)}")

    if action == "write_word":
        role = decision.get("your_role", "writer")
        st.write("✍️ **Write a word** (2–4 Chinese characters):")
        word = st.text_input("Your word", key="decision_write_word", max_chars=8)
        if st.button("Submit word", key="btn_write_word") and word.strip():
            state.submit_choice(HUMAN_ID, word.strip())
            st.session_state.pending_decision = None
            st.success("Submitted!")

    elif action == "guess_word":
        writer_name = _player_display(decision.get("writer", "?"))
        st.write(f"🔮 **Guess {writer_name}'s secret word:**")
        guess = st.text_input("Your guess", key="decision_guess_word", max_chars=8)
        if st.button("Submit guess", key="btn_guess_word") and guess.strip():
            state.submit_choice(HUMAN_ID, guess.strip())
            st.session_state.pending_decision = None
            st.success("Submitted!")

    elif action == "guess_authors":
        words = decision.get("words", [])
        candidates = decision.get("candidate_authors", [])
        st.write("🎭 **Guess who wrote each word:**")
        guesses = []
        for i, word in enumerate(words):
            options = [_player_display(c) for c in candidates]
            choice = st.selectbox(
                f"`{word}` was written by…",
                options,
                key=f"decision_author_{i}",
            )
            # Map display name back to player_id
            guesses.append(candidates[options.index(choice)])
        if st.button("Submit attributions", key="btn_guess_authors"):
            state.submit_choice(HUMAN_ID, ",".join(guesses))
            st.session_state.pending_decision = None
            st.success("Submitted!")

    elif action == "choose_bottle":
        available = decision.get("available_bottles", [])
        position = decision.get("your_position", "?")
        st.write(f"☠️ **Choose a bottle** (you pick #{position} in selection order):")
        st.caption("One bottle is poisoned. The player who drinks it loses 1 point.")
        bottle_cols = st.columns(len(available))
        bottle_emojis = {"Red": "🔴", "Blue": "🔵", "Green": "🟢", "Yellow": "🟡"}
        for col, bottle in zip(bottle_cols, available):
            emoji = bottle_emojis.get(bottle, "🍾")
            if col.button(f"{emoji} {bottle}", key=f"bottle_{bottle}"):
                state.submit_choice(HUMAN_ID, bottle)
                st.session_state.pending_decision = None
                st.rerun()

    else:
        st.warning(f"Unknown action: `{action}`")


# ── page: Chat fragment ───────────────────────────────────────────────────────

@st.fragment(run_every=2)
def _chat_fragment() -> None:
    """Auto-refreshes every 2 s: shows conversation history, send form, whispers."""
    state: SharedState = st.session_state.state
    budget = state.get_send_budget(HUMAN_ID)

    # Header with send-count badge.
    budget_display = str(budget) if budget is not None else "∞"
    st.subheader("💬 Chat")
    if budget is not None and budget <= 0:
        st.warning("📭 No sends remaining this round.", icon="⚠️")
    else:
        st.caption(f"Sends remaining: **{budget_display} / 10**")

    # Chat target selector (persisted in session state across reruns).
    ai_ids = [pid for pid in ALL_PLAYERS if pid != HUMAN_ID]
    ai_labels = [f"{AI_META[pid]['emoji']} {AI_META[pid]['name']}" for pid in ai_ids]
    target_label = st.selectbox("Talk to:", ai_labels, key="chat_target_select")
    target_id = ai_ids[ai_labels.index(target_label)]

    # Scrollable chat history.
    history = state.get_chat_history()
    with st.container(height=340, border=True):
        if not history:
            st.caption("*No messages yet. Start the conversation!*")
        for entry in history:
            _render_chat_entry(entry)

    # Message input — disabled when budget is exhausted.
    send_disabled = budget is not None and budget <= 0
    msg = st.chat_input(
        "Type a message…",
        key="chat_input_widget",
        disabled=send_disabled,
    )
    if msg and msg.strip():
        try:
            state.send_message(HUMAN_ID, target_id, msg.strip())
        except RuntimeError as exc:
            st.error(str(exc))


def _render_chat_entry(entry: dict) -> None:
    """Render a single chat-history entry (chat message or whisper notification)."""
    if entry["type"] == "whisper":
        sender_name = _ai_display_name(entry["sender"])
        recipient_name = _ai_display_name(entry["recipient"])
        st.caption(f"🤫 *{sender_name} and {recipient_name} are whispering…*")

    elif entry["type"] == "chat":
        sender = entry["sender"]
        recipient = entry["recipient"]
        text = entry["text"]

        if sender == HUMAN_ID:
            # Human sent → right-side feel using chat_message "user".
            target_name = _ai_display_name(recipient)
            with st.chat_message("user"):
                st.markdown(f"**→ {target_name}:** {text}")
        else:
            # AI sent to human.
            meta = AI_META.get(sender, {})
            emoji = meta.get("emoji", "🤖")
            name = meta.get("name", sender)
            with st.chat_message("assistant", avatar=emoji):
                st.markdown(f"**{name}:** {text}")


# ── page: Game Over ───────────────────────────────────────────────────────────

def render_game_over() -> None:
    st.set_page_config(
        page_title="How to Fool AI — Results", page_icon="🏁", layout="centered"
    )

    state: SharedState = st.session_state.state

    st.title("🏁 Game Over")
    st.markdown("All scores are now revealed.")
    st.divider()

    # Build rankings.
    scores = {pid: state.get_score(pid) for pid in ALL_PLAYERS}
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)

    medal = ["🥇", "🥈", "🥉"] + ["  "] * 10
    rows = []
    for rank, (pid, score) in enumerate(ranked):
        name = (
            f"{st.session_state.player_name} (You)"
            if pid == HUMAN_ID
            else f"{AI_META[pid]['emoji']} {AI_META[pid]['name']}"
        )
        rows.append((medal[rank], name, score))

    st.subheader("Final Rankings")
    for m, name, score in rows:
        cols = st.columns([1, 4, 1])
        cols[0].markdown(f"### {m}")
        cols[1].markdown(f"**{name}**")
        cols[2].markdown(f"**{score} pts**")

    # Winner announcement.
    winner_pid, winner_score = ranked[0]
    human_score = scores[HUMAN_ID]
    st.divider()
    if winner_pid == HUMAN_ID:
        st.success(
            f"🎉 You win with **{human_score} point{'s' if human_score != 1 else ''}**!"
            " You successfully outfoxed the AIs."
        )
    else:
        winner_name = f"{AI_META[winner_pid]['emoji']} {AI_META[winner_pid]['name']}"
        st.info(
            f"The AIs win this round — **{winner_name}** takes first place "
            f"with **{winner_score} pts**.  Better luck next time!"
        )

    st.divider()
    if st.button("🔄 Play Again", use_container_width=True):
        _stop_threads()
        # Reset relevant session state without clearing player_name.
        for key in ("state", "pending_decision", "last_round_results",
                    "chat_rendered_up_to"):
            st.session_state.pop(key, None)
        st.session_state.page = "setup"
        st.rerun()


# ── helpers ───────────────────────────────────────────────────────────────────

def _player_display(player_id: str) -> str:
    """Human-readable label for any player ID."""
    if player_id == HUMAN_ID:
        return f"**{st.session_state.get('player_name', 'You')}** (You)"
    meta = AI_META.get(player_id)
    if meta:
        return f"{meta['emoji']} {meta['name']}"
    return player_id


def _ai_display_name(player_id: str) -> str:
    """Short display name for an AI (used in whisper notifications)."""
    meta = AI_META.get(player_id)
    if meta:
        return f"{meta['emoji']} {meta['name']}"
    return player_id


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    _init_session()
    page = st.session_state.page
    if page == "setup":
        render_setup()
    elif page == "game":
        render_game()
    elif page == "game_over":
        render_game_over()
    else:
        st.error(f"Unknown page: {page!r}")


main()
