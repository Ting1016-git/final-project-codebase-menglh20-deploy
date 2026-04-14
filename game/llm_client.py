"""LLM client for AI agent calls.

API key resolution order:
1. st.secrets["ANTHROPIC_API_KEY"]  (Streamlit deployment / local secrets.toml)
2. os.environ["ANTHROPIC_API_KEY"]  (CI / shell environment)

Model assignments (per SPEC.md):
- call_llm()        → claude-haiku-4-5   (chat replies, fast & cheap)
- call_strategic()  → claude-sonnet-4-6  (game decisions, stronger reasoning)
"""

from __future__ import annotations

import os
from typing import Any

import anthropic

_HAIKU_MODEL = "claude-haiku-4-5-20251001"
_SONNET_MODEL = "claude-sonnet-4-6"


def _get_api_key() -> str:
    """Return the Anthropic API key from Streamlit secrets or env."""
    try:
        import streamlit as st  # imported lazily — not available in tests
        return st.secrets["ANTHROPIC_API_KEY"]
    except Exception:
        pass
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    return key


def _make_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=_get_api_key())


def call_llm(
    system_prompt: str,
    messages: list[dict[str, str]],
    default_reply: str,
    *,
    max_tokens: int = 120,
) -> str:
    """Call claude-haiku for a chat reply.

    Falls back to *default_reply* on any API error so the agent never crashes.
    """
    try:
        client = _make_client()
        response = client.messages.create(
            model=_HAIKU_MODEL,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=messages,
        )
        text = response.content[0].text.strip()
        # Enforce the 40-char soft limit at the client boundary.
        return text[:40] if len(text) > 40 else text
    except Exception:
        return default_reply


def call_strategic(
    system_prompt: str,
    messages: list[dict[str, str]],
    default_reply: str,
    *,
    max_tokens: int = 120,
) -> str:
    """Call claude-sonnet for a strategic game decision.

    Falls back to *default_reply* on any API error.
    """
    try:
        client = _make_client()
        response = client.messages.create(
            model=_SONNET_MODEL,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=messages,
        )
        text = response.content[0].text.strip()
        return text[:40] if len(text) > 40 else text
    except Exception:
        return default_reply
