"""Input guardrail: a fast, deterministic safety check on the user's request.

This is intentionally a non-LLM keyword guard — cheap, predictable, and run
before the model. It trips only on a small set of genuinely dangerous or
out-of-scope intents (mass deletion, privilege escalation, exfiltrating
credentials). The tools themselves are already sandboxed and never delete; this
is a second, earlier line of defence.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from agents import (
    Agent,
    GuardrailFunctionOutput,
    RunContextWrapper,
    TResponseInputItem,
    input_guardrail,
)

from ..logging_setup import get_logger

logger = get_logger(__name__)

# (compiled pattern, human-readable reason) — kept tight to avoid false positives.
_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\brm\s+-rf\s+/(?:\s|$)"), "destructive recursive deletion of the filesystem root"),
    (
        re.compile(r"\b(format|wipe|erase)\s+(the\s+)?(disk|drive|hard\s*drive|ssd|computer)\b"),
        "formatting or wiping a disk",
    ),
    (
        re.compile(r"\b(delete|erase|wipe)\s+(all|every|everything|my\s+entire)\b.*\b(file|files|disk|drive|data)\b"),
        "mass deletion of files",
    ),
    (re.compile(r"\bsudo\b"), "running privileged shell commands"),
    (
        re.compile(
            r"(read|show|print|cat|send|upload|email|exfiltrat\w*)\b.{0,40}"
            r"(\.ssh|id_rsa|private\s*key|aws\s*credentials|/etc/passwd|keychain)"
        ),
        "accessing or exfiltrating credentials",
    ),
]


def _as_text(user_input: str | list[TResponseInputItem]) -> str:
    if isinstance(user_input, str):
        return user_input
    parts: list[str] = []
    for item in _iter(user_input):
        content = item.get("content") if isinstance(item, dict) else None
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for chunk in content:
                if isinstance(chunk, dict) and isinstance(chunk.get("text"), str):
                    parts.append(chunk["text"])
    return "\n".join(parts)


def _iter(items: object) -> Iterable[dict]:
    if isinstance(items, list):
        for it in items:
            if isinstance(it, dict):
                yield it


@input_guardrail
async def safety_guardrail(
    ctx: RunContextWrapper[None],
    agent: Agent,
    user_input: str | list[TResponseInputItem],
) -> GuardrailFunctionOutput:
    """Trip the wire on clearly dangerous or out-of-scope requests."""
    text = _as_text(user_input).lower()
    for pattern, reason in _RULES:
        if pattern.search(text):
            logger.warning("Guardrail tripped: %s", reason)
            return GuardrailFunctionOutput(
                output_info={"reason": reason},
                tripwire_triggered=True,
            )
    return GuardrailFunctionOutput(output_info={"reason": None}, tripwire_triggered=False)


__all__ = ["safety_guardrail"]
