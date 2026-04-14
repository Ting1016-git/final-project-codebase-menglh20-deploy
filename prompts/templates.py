"""Decision-prompt builder for AI agents.

Each AI action (reply / proactive chat / game decision) is resolved via a
single LLM call.  This module assembles the user-turn message that is
appended to the persona's system prompt.
"""

from __future__ import annotations

from typing import Any


def build_reply_prompt(
    persona_name: str,
    sender: str,
    incoming_text: str,
    score: int,
    send_budget: int | None,
) -> str:
    """Prompt for replying to an incoming ChatMessage."""
    budget_str = str(send_budget) if send_budget is not None else "无限制"
    lines = [
        "当前得分：" + str(score) + "  剩余发言次数：" + budget_str,
        sender + " 对你说：「" + incoming_text + "」",
        "请用符合你人格的语气回复一句话（中文，不超过40字）。",
    ]
    return "\n".join(lines)


def build_proactive_prompt(
    persona_name: str,
    target: str,
    score: int,
    send_budget: int | None,
    recent_observations: list[str] | None = None,
) -> str:
    """Prompt for proactively initiating a message."""
    budget_str = str(send_budget) if send_budget is not None else "无限制"
    lines = [
        "当前得分：" + str(score) + "  剩余发言次数：" + budget_str,
    ]
    if recent_observations:
        lines.append("近期观察：")
        lines.extend("- " + o for o in recent_observations)
    lines.append("你决定主动联系 " + target + "。")
    lines.append("请说一句话（中文，不超过40字），内容可以是闲聊、试探或传递（真假）信息。")
    return "\n".join(lines)


def build_game_decision_prompt(
    persona_name: str,
    game_type: str,
    situation: dict[str, Any],
    score: int,
    send_budget: int | None,
    recent_messages: list[dict[str, str]] | None = None,
) -> str:
    """Prompt for a strategic in-game decision (uses Sonnet)."""
    budget_str = str(send_budget) if send_budget is not None else "无限制"
    situation_str = "\n".join("  " + k + ": " + str(v) for k, v in situation.items())
    lines = [
        "当前游戏：" + game_type,
        "当前得分：" + str(score) + "  剩余发言次数：" + budget_str,
        "局面信息：",
        situation_str,
    ]
    if recent_messages:
        lines.append("最近收到的消息：")
        for m in recent_messages[-6:]:
            lines.append(
                "  [" + m["sender"] + "->" + m["recipient"] + "] " + m["text"]
            )
    lines.append("请做出你的决策，并简短说明理由（中文，不超过40字）。")
    return "\n".join(lines)
