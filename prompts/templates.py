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
    sender_name: str | None = None,
) -> str:
    """Prompt for replying to an incoming ChatMessage."""
    budget_str = str(send_budget) if send_budget is not None else "无限制"
    sender_label = sender_name or sender
    lines = [
        "当前得分：" + str(score) + "  剩余发言次数：" + budget_str,
        sender_label + " 对你说：「" + incoming_text + "」",
        "请用符合你人格的语气回复一句话（中文，不超过40字）。",
        "不要使用内部ID（如ai_0/human），请使用角色名。",
    ]
    return "\n".join(lines)


def build_proactive_prompt(
    persona_name: str,
    target: str,
    score: int,
    send_budget: int | None,
    recent_observations: list[str] | None = None,
    target_name: str | None = None,
    current_round: int | None = None,
    game_type: str | None = None,
) -> str:
    """Prompt for proactively initiating a message."""
    budget_str = str(send_budget) if send_budget is not None else "无限制"
    target_label = target_name or target
    lines = [
        "当前得分：" + str(score) + "  剩余发言次数：" + budget_str,
    ]
    if current_round is not None:
        lines.append("当前回合：" + str(current_round))
    if game_type:
        lines.append("当前游戏类型：" + game_type)
    if recent_observations:
        lines.append("近期观察：")
        lines.extend("- " + o for o in recent_observations)
    lines.append("你决定主动联系 " + target_label + "。")
    lines.append(
        "请说一句话（中文，不超过40字），内容可以是试探、误导或套取信息。"
        "不要只寒暄，你的每句话都应该有战略意图。"
    )
    lines.append("不要使用内部ID（如ai_0/human），请使用角色名。")
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
