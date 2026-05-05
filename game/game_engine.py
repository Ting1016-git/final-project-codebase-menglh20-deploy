"""GameEngine — orchestrates rounds, selects mini-game types, manages phase
transitions, and applies scoring.

Concurrency model
-----------------
The engine runs as a daemon thread alongside the Streamlit human-IO thread
and three AIAgent daemon threads.  All coordination happens through
``SharedState``:

* ``broadcast_event`` / ``send_event_to_player`` deliver ``GameEvent``
  objects to player inboxes so AI agents and the UI can react.
* ``submit_choice`` (called by AI agents and the human-IO thread) unblocks
  ``_wait_for_choice`` via a per-player ``threading.Event``.
* ``threading.Event.wait(timeout=...)`` is used throughout so the engine
  blocks without holding any lock and without starving the GIL — other
  threads continue running freely while the engine waits.

Round sequence
--------------
For each round::

    select game type → reset send budgets → broadcast phase_change(playing)
    → run mini-game → apply scores → broadcast round_end
    → broadcast phase_change(reveal) → pause _REVEAL_DURATION seconds

After all rounds::

    set phase = "game_over" → broadcast game_over event
"""

from __future__ import annotations

import random
import threading
import time
from typing import Any, Dict, List, Optional

from game.shared_state import GameEvent, SharedState

# ── constants ────────────────────────────────────────────────────────────────

# Rotation order for mini-game types.
_GAME_TYPES: List[str] = ["guess_the_word", "who_wrote_it", "poison_bottle"]

# Bottle colours used in Poison Bottle.
_BOTTLES: List[str] = ["Red", "Blue", "Green", "Yellow"]

# Fall-back Chinese words used when a player times out on a write-word prompt.
_FALLBACK_WORDS: List[str] = [
    "苹果", "天空", "月亮", "星星", "海洋", "山脉", "花朵", "云彩",
    "时间", "梦想", "希望", "自由",
]

# Seconds the engine pauses on the reveal phase before the next round.
_REVEAL_DURATION: float = 5.0


def _random_word() -> str:
    return random.choice(_FALLBACK_WORDS)


# ── engine ───────────────────────────────────────────────────────────────────

class GameEngine(threading.Thread):
    """Daemon thread that drives the full game loop.

    Parameters
    ----------
    state:
        The shared ``SharedState`` instance used by all threads.
    total_rounds:
        Number of rounds to play (e.g. 3, 5, 7, or 10).
    choice_timeout:
        Seconds to wait for each player's in-game decision before
        falling back to a random default.  Default: 60 s.
    """

    def __init__(
        self,
        state: SharedState,
        total_rounds: int,
        choice_timeout: float = 60.0,
        reveal_duration: float = _REVEAL_DURATION,
    ) -> None:
        super().__init__(daemon=True, name="GameEngine")
        self.state = state
        self.total_rounds = total_rounds
        self.choice_timeout = choice_timeout
        self.reveal_duration = reveal_duration
        self._stop_event = threading.Event()
        self._type_index: int = 0   # cursor into _GAME_TYPES for rotation

    # ── lifecycle ────────────────────────────────────────────────────────────

    def stop(self) -> None:
        """Signal the engine to exit after the current wait completes."""
        self._stop_event.set()

    def run(self) -> None:
        self.state.set_round_info(0, self.total_rounds, "")
        self.state.set_phase("playing")

        for round_num in range(1, self.total_rounds + 1):
            if self._stop_event.is_set():
                break
            self._run_round(round_num)

        # ── game over ────────────────────────────────────────────────────────
        self.state.set_phase("game_over")
        final_scores = {
            pid: self.state.get_score(pid) for pid in self.state.player_ids
        }
        self.state.broadcast_event(
            GameEvent(
                event_type="game_over",
                payload={"final_scores": final_scores},
            )
        )

    # ── round orchestration ──────────────────────────────────────────────────

    def _run_round(self, round_num: int) -> None:
        game_type = self._select_game_type()

        # Fresh send budgets for this round.
        self.state.reset_send_budgets()

        self.state.set_round_info(round_num, self.total_rounds, game_type)
        self.state.set_phase("playing")

        self.state.broadcast_event(
            GameEvent(
                event_type="phase_change",
                payload={
                    "phase": "playing",
                    "round": round_num,
                    "total_rounds": self.total_rounds,
                    "game_type": game_type,
                },
            )
        )

        # Dispatch to the appropriate mini-game runner.
        if game_type == "guess_the_word":
            results = self._run_guess_the_word(round_num)
        elif game_type == "who_wrote_it":
            results = self._run_who_wrote_it(round_num)
        else:
            results = self._run_poison_bottle(round_num)

        # Apply score deltas atomically.
        for pid, delta in results.get("score_deltas", {}).items():
            if delta != 0:
                self.state.update_score(pid, delta)

        self.state.broadcast_event(
            GameEvent(
                event_type="round_end",
                payload={
                    "round": round_num,
                    "game_type": game_type,
                    "results": results,
                },
            )
        )

        # Reveal phase — UI shows results, players can still chat.
        self.state.set_phase("reveal")
        self.state.broadcast_event(
            GameEvent(
                event_type="phase_change",
                payload={"phase": "reveal", "round": round_num},
            )
        )

        # Non-blocking pause: _stop_event.wait() yields the GIL so AI agents
        # and Streamlit threads continue running during the reveal window.
        # If stop() is called, the wait returns immediately.
        self._stop_event.wait(timeout=self.reveal_duration)

    # ── game type rotation ───────────────────────────────────────────────────

    def _select_game_type(self) -> str:
        """Cycle through game types in a fixed order, avoiding consecutive repeats."""
        game_type = _GAME_TYPES[self._type_index % len(_GAME_TYPES)]
        self._type_index += 1
        return game_type

    # ── core blocking primitive ───────────────────────────────────────────────

    def _wait_for_choice(self, player_id: str, timeout: float) -> Any:
        """Block until *player_id* submits a choice, *timeout* expires, or the
        engine is stopped.

        Polls in short slices so a ``stop()`` call can interrupt the wait
        promptly without busy-spinning.  The GIL is released during each
        ``Event.wait()`` call, so other threads (AI agents, human IO,
        Streamlit) continue running unimpeded.

        Returns the submitted choice, or ``None`` on timeout / stop.
        """
        event = self.state.get_choice_event(player_id)
        deadline = time.monotonic() + timeout
        _POLL = 0.25    # seconds between stop-event checks

        while not self._stop_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if event.wait(timeout=min(remaining, _POLL)):
                break   # choice submitted

        return self.state.get_choice(player_id)

    # ── mini-game: Guess the Word ─────────────────────────────────────────────

    def _run_guess_the_word(self, round_num: int) -> Dict[str, Any]:
        """Run one round of Guess the Word and return a results dict."""
        from game.scoring import score_guess_the_word

        all_players = self.state.player_ids
        writer_id = random.choice(all_players)
        guessers = [p for p in all_players if p != writer_id]

        # Phase 1 — writer submits a secret word.
        self.state.reset_choices([writer_id])
        self.state.send_event_to_player(
            writer_id,
            GameEvent(
                event_type="make_decision",
                payload={
                    "game_type": "guess_the_word",
                    "action": "write_word",
                    "round": round_num,
                    "your_role": "writer",
                },
            ),
        )
        writer_word = self._wait_for_choice(writer_id, timeout=self.choice_timeout)
        if not writer_word:
            writer_word = _random_word()

        # Phase 2 — all guessers submit simultaneously.
        self.state.reset_choices(guessers)
        for guesser in guessers:
            self.state.send_event_to_player(
                guesser,
                GameEvent(
                    event_type="make_decision",
                    payload={
                        "game_type": "guess_the_word",
                        "action": "guess_word",
                        "round": round_num,
                        "writer": writer_id,
                        "your_role": "guesser",
                    },
                ),
            )

        guesses: Dict[str, str] = {}
        for guesser in guessers:
            choice = self._wait_for_choice(guesser, timeout=self.choice_timeout)
            guesses[guesser] = choice if choice else _random_word()

        deltas = score_guess_the_word(writer_id, writer_word, guesses)
        return {
            "writer": writer_id,
            "word": writer_word,
            "guesses": guesses,
            "score_deltas": deltas,
        }

    # ── mini-game: Who Wrote It ───────────────────────────────────────────────

    def _run_who_wrote_it(self, round_num: int) -> Dict[str, Any]:
        """Run one round of Who Wrote It and return a results dict."""
        from game.scoring import score_who_wrote_it

        all_players = self.state.player_ids

        # Phase 1 — everyone writes a word simultaneously.
        self.state.reset_choices(all_players)
        for pid in all_players:
            self.state.send_event_to_player(
                pid,
                GameEvent(
                    event_type="make_decision",
                    payload={
                        "game_type": "who_wrote_it",
                        "action": "write_word",
                        "round": round_num,
                    },
                ),
            )

        words: Dict[str, str] = {}
        for pid in all_players:
            choice = self._wait_for_choice(pid, timeout=self.choice_timeout)
            words[pid] = choice if choice else _random_word()

        # Phase 2 — everyone guesses authors simultaneously.
        # Each guesser sees the words written by other players (not their own),
        # sorted by author player_id so the engine can reconstruct the mapping.
        self.state.reset_choices(all_players)
        author_order: Dict[str, List[str]] = {}
        for guesser in all_players:
            other_authors = sorted(p for p in all_players if p != guesser)
            author_order[guesser] = other_authors
            self.state.send_event_to_player(
                guesser,
                GameEvent(
                    event_type="make_decision",
                    payload={
                        "game_type": "who_wrote_it",
                        "action": "guess_authors",
                        "round": round_num,
                        # words listed in author_order so the agent can return
                        # guessed IDs in the same positional order
                        "words": [words[a] for a in other_authors],
                        "candidate_authors": other_authors,
                    },
                ),
            )

        # Parse each guesser's submission: "authorA,authorB,authorC"
        attributions: Dict[str, Dict[str, str]] = {}
        for guesser in all_players:
            choice = self._wait_for_choice(guesser, timeout=self.choice_timeout)
            attributions[guesser] = _parse_attribution(
                choice, author_order[guesser]
            )

        deltas = score_who_wrote_it(words, attributions)
        return {
            "words": words,
            "attributions": attributions,
            "score_deltas": deltas,
        }

    # ── mini-game: Poison Bottle ──────────────────────────────────────────────

    def _run_poison_bottle(self, round_num: int) -> Dict[str, Any]:
        """Run one round of Poison Bottle and return a results dict."""
        from game.scoring import get_poison_bottle_order, score_poison_bottle

        poisoned = random.choice(_BOTTLES)
        all_players = self.state.player_ids
        scores = {pid: self.state.get_score(pid) for pid in all_players}
        selection_order = get_poison_bottle_order(all_players, scores)

        available = list(_BOTTLES)
        choices: Dict[str, str] = {}

        # Players choose one at a time in score-ranked order (highest first).
        for position, pid in enumerate(selection_order):
            self.state.reset_choices([pid])
            self.state.send_event_to_player(
                pid,
                GameEvent(
                    event_type="make_decision",
                    payload={
                        "game_type": "poison_bottle",
                        "action": "choose_bottle",
                        "round": round_num,
                        "available_bottles": list(available),
                        "selection_order": selection_order,
                        "your_position": position + 1,
                    },
                ),
            )

            choice = self._wait_for_choice(pid, timeout=self.choice_timeout)
            if choice not in available:
                choice = random.choice(available)
            choices[pid] = choice
            available.remove(choice)

        deltas = score_poison_bottle(choices, poisoned)
        return {
            "poisoned_bottle": poisoned,
            "choices": choices,
            "selection_order": selection_order,
            "score_deltas": deltas,
        }


# ── helpers ───────────────────────────────────────────────────────────────────

def _parse_attribution(
    submission: Optional[str],
    expected_authors: List[str],
) -> Dict[str, str]:
    """Parse a comma-separated attribution string into a mapping.

    Expected format: ``"author_id1,author_id2,author_id3"`` — one guessed
    author per position matching *expected_authors*.  Falls back to a random
    shuffle when the submission is missing or malformed (e.g. timeout).

    Parameters
    ----------
    submission:
        Raw string from the agent/player, or ``None`` on timeout.
    expected_authors:
        The ordered list of true author IDs that the guesser was shown words
        for.

    Returns
    -------
    ``{true_author_id: guessed_author_id}``
    """
    if submission:
        parts = [p.strip() for p in submission.split(",")]
        if len(parts) == len(expected_authors):
            return dict(zip(expected_authors, parts))
    # Timeout or malformed — random attribution.
    shuffled = list(expected_authors)
    random.shuffle(shuffled)
    return dict(zip(expected_authors, shuffled))
