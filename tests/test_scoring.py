"""Tests for game/scoring.py"""

import pytest

from game.scoring import (
    BOTTLES,
    get_poison_bottle_order,
    score_guess_the_word,
    score_poison_bottle,
    score_who_wrote_it,
)

PLAYERS = ["human", "ai_0", "ai_1", "ai_2"]


# ---------------------------------------------------------------------------
# score_guess_the_word
# ---------------------------------------------------------------------------

def test_all_guessers_correct_writer_no_bonus():
    """Writer earns 0 when *all* guessers are correct."""
    deltas = score_guess_the_word(
        writer_id="human",
        word="苹果",
        guesses={"ai_0": "苹果", "ai_1": "苹果", "ai_2": "苹果"},
    )
    assert deltas["human"] == 0
    assert deltas["ai_0"] == 1
    assert deltas["ai_1"] == 1
    assert deltas["ai_2"] == 1


def test_no_guesser_correct_writer_no_bonus():
    """Writer earns 0 when *no* guesser is correct."""
    deltas = score_guess_the_word(
        writer_id="human",
        word="苹果",
        guesses={"ai_0": "天空", "ai_1": "月亮", "ai_2": "星星"},
    )
    assert deltas["human"] == 0
    for pid in ("ai_0", "ai_1", "ai_2"):
        assert deltas[pid] == 0


def test_partial_correct_writer_gets_bonus():
    """Writer earns +1 when at least one—but not all—guessers are correct."""
    deltas = score_guess_the_word(
        writer_id="human",
        word="苹果",
        guesses={"ai_0": "苹果", "ai_1": "天空", "ai_2": "月亮"},
    )
    assert deltas["human"] == 1
    assert deltas["ai_0"] == 1
    assert deltas["ai_1"] == 0
    assert deltas["ai_2"] == 0


def test_case_insensitive_word_match():
    deltas = score_guess_the_word(
        writer_id="human",
        word="Apple",
        guesses={"ai_0": "apple", "ai_1": "APPLE"},
    )
    assert deltas["ai_0"] == 1
    assert deltas["ai_1"] == 1


def test_writer_is_included_in_deltas():
    deltas = score_guess_the_word(
        writer_id="human",
        word="月亮",
        guesses={"ai_0": "月亮"},
    )
    assert "human" in deltas


# ---------------------------------------------------------------------------
# score_who_wrote_it
# ---------------------------------------------------------------------------

def test_best_guesser_gets_point():
    words = {"human": "梦想", "ai_0": "苹果", "ai_1": "天空", "ai_2": "月亮"}
    # human correctly identifies all three others; everyone else guesses randomly wrong
    attributions = {
        "human": {"ai_0": "ai_0", "ai_1": "ai_1", "ai_2": "ai_2"},  # 3 correct
        "ai_0": {"human": "ai_1", "ai_1": "human", "ai_2": "ai_2"},  # 1 correct
        "ai_1": {"human": "ai_2", "ai_0": "human", "ai_2": "ai_2"},  # 1 correct
        "ai_2": {"human": "ai_0", "ai_0": "ai_1", "ai_1": "human"}, # 0 correct
    }
    deltas = score_who_wrote_it(words, attributions)
    assert deltas["human"] >= 1   # best guesser


def test_tied_best_guessers_both_get_point():
    words = {"human": "A", "ai_0": "B", "ai_1": "C", "ai_2": "D"}
    # human and ai_0 each get 2 correct; ai_1 and ai_2 get 0
    attributions = {
        "human": {"ai_0": "ai_0", "ai_1": "ai_1", "ai_2": "ai_2"},     # 3 correct
        "ai_0":  {"human": "human", "ai_1": "ai_1", "ai_2": "ai_2"},   # 3 correct
        "ai_1":  {"human": "ai_2", "ai_0": "human", "ai_2": "ai_0"},   # 0 correct
        "ai_2":  {"human": "ai_1", "ai_0": "ai_2", "ai_1": "human"},   # 0 correct
    }
    deltas = score_who_wrote_it(words, attributions)
    assert deltas["human"] >= 1
    assert deltas["ai_0"] >= 1


def test_writer_partial_guess_bonus():
    """A writer whose word is guessed by some (not all) guessers gets +1."""
    words = {"human": "梦", "ai_0": "海", "ai_1": "山", "ai_2": "云"}
    # Only human correctly identified ai_0's word; the other two got it wrong.
    attributions = {
        "human": {"ai_0": "ai_0", "ai_1": "ai_2", "ai_2": "ai_1"},
        "ai_1":  {"human": "ai_0", "ai_0": "ai_2", "ai_2": "ai_2"},
        "ai_2":  {"human": "ai_1", "ai_0": "ai_1", "ai_1": "human"},
        # ai_0 is guessing
        "ai_0":  {"human": "human", "ai_1": "ai_2", "ai_2": "ai_1"},
    }
    deltas = score_who_wrote_it(words, attributions)
    assert deltas["ai_0"] >= 1   # partial-guess bonus


def test_writer_no_bonus_when_all_eligible_guessers_correct():
    """Writer gets 0 when every eligible guesser identifies their word."""
    words = {"human": "梦想", "ai_0": "苹果", "ai_1": "天空", "ai_2": "月亮"}
    attributions = {
        "human": {"ai_0": "ai_0", "ai_1": "ai_1", "ai_2": "ai_2"},
        "ai_1": {"human": "human", "ai_0": "ai_0", "ai_2": "ai_2"},
        "ai_2": {"human": "human", "ai_0": "ai_0", "ai_1": "ai_1"},
        # ai_0 does not guess ai_0's own word and gets 0 as guesser.
        "ai_0": {"human": "ai_2", "ai_1": "human", "ai_2": "ai_1"},
    }
    deltas = score_who_wrote_it(words, attributions)
    assert deltas["ai_0"] == 0


def test_no_best_guesser_bonus_when_all_zero():
    words = {"human": "X", "ai_0": "Y"}
    attributions = {
        "human": {"ai_0": "human"},   # wrong
        "ai_0":  {"human": "ai_0"},   # wrong
    }
    deltas = score_who_wrote_it(words, attributions)
    # Nobody gets the guesser bonus (max correct == 0).
    assert deltas["human"] == 0 or deltas["ai_0"] == 0   # at most writer bonus


# ---------------------------------------------------------------------------
# score_poison_bottle
# ---------------------------------------------------------------------------

def test_poison_drinker_loses_point():
    choices = {"human": "Red", "ai_0": "Blue", "ai_1": "Green", "ai_2": "Yellow"}
    deltas = score_poison_bottle(choices, "Red")
    assert deltas["human"] == -1
    assert deltas["ai_0"] == 0
    assert deltas["ai_1"] == 0
    assert deltas["ai_2"] == 0


def test_case_insensitive_bottle_match():
    choices = {"human": "red", "ai_0": "BLUE"}
    deltas = score_poison_bottle(choices, "Red")
    assert deltas["human"] == -1
    assert deltas["ai_0"] == 0


def test_no_poison_drinker_if_bottle_not_chosen():
    choices = {"human": "Blue", "ai_0": "Green"}
    deltas = score_poison_bottle(choices, "Red")
    assert all(d == 0 for d in deltas.values())


# ---------------------------------------------------------------------------
# get_poison_bottle_order
# ---------------------------------------------------------------------------

def test_highest_score_picks_first():
    scores = {"human": 5, "ai_0": 3, "ai_1": 1, "ai_2": 0}
    order = get_poison_bottle_order(list(scores.keys()), scores)
    assert order[0] == "human"
    assert order[-1] == "ai_2"


def test_tied_scores_randomised():
    """With all-equal scores, any permutation is valid."""
    scores = {"human": 2, "ai_0": 2, "ai_1": 2, "ai_2": 2}
    orders = {tuple(get_poison_bottle_order(list(scores.keys()), scores)) for _ in range(50)}
    # At least two different orderings should appear with equal scores.
    assert len(orders) > 1


def test_order_includes_all_players():
    scores = {"human": 0, "ai_0": 0, "ai_1": 0, "ai_2": 0}
    order = get_poison_bottle_order(list(scores.keys()), scores)
    assert set(order) == {"human", "ai_0", "ai_1", "ai_2"}
