"""What supermode knows about a model: quality tier, context window, capabilities.

A built-in table matched on the model id, first pattern wins, so the more
specific patterns come first. The id is matched without any routing prefix
(``openai/gpt-4o`` and ``gpt-4o`` are the same model) and without an
OpenRouter variant suffix (``:free``, ``:beta``).

Quality is a 0-100 tier for ranking only, not a benchmark score. A model the
table does not know gets a low tier and an unknown context window, so it is
picked only when no known model can serve. Operators correct or extend the
scores with ``SUPERMODE_QUALITY``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelProfile:
    quality: int
    # Tokens of prompt plus reply; None when unknown (never filtered on).
    context: int | None
    vision: bool
    # None when unknown: kept for a request with tools.
    tools: bool | None


UNKNOWN = ModelProfile(quality=40, context=None, vision=False, tools=None)

_K = 1000
_M = 1_000_000

# (pattern, quality, context, vision, tools)
_TABLE: tuple[tuple[str, int, int | None, bool, bool | None], ...] = (
    # Anthropic
    (r"claude-(opus-4|4-opus|4\.\d-opus|opus)", 98, 200 * _K, True, True),
    (r"claude-(sonnet-4|4-sonnet|3-7-sonnet|3\.7-sonnet|sonnet)", 94, 200 * _K, True, True),
    (r"claude-3-5-sonnet|claude-3\.5-sonnet", 88, 200 * _K, True, True),
    (r"claude-(haiku-4|4-haiku|3-5-haiku|3\.5-haiku|haiku)", 80, 200 * _K, True, True),
    (r"claude", 75, 200 * _K, True, True),
    # OpenAI
    (r"gpt-5(\.\d)?-pro", 97, 400 * _K, True, True),
    (r"gpt-5(\.\d)?-nano", 72, 400 * _K, True, True),
    (r"gpt-5(\.\d)?-mini", 85, 400 * _K, True, True),
    (r"gpt-5", 96, 400 * _K, True, True),
    (r"^o3-pro", 94, 200 * _K, True, True),
    (r"^o3-mini", 84, 200 * _K, False, True),
    (r"^o3", 92, 200 * _K, True, True),
    (r"^o4-mini", 87, 200 * _K, True, True),
    (r"gpt-4\.1-nano", 68, _M, True, True),
    (r"gpt-4\.1-mini", 80, _M, True, True),
    (r"gpt-4\.1", 88, _M, True, True),
    (r"gpt-4o-mini", 72, 128 * _K, True, True),
    (r"gpt-4o", 85, 128 * _K, True, True),
    (r"gpt-oss-120b", 82, 128 * _K, False, True),
    (r"gpt-oss-20b", 70, 128 * _K, False, True),
    (r"gpt-4-turbo", 78, 128 * _K, True, True),
    (r"gpt-3\.5", 50, 16 * _K, False, True),
    # Google
    (r"gemini-3(\.\d)?-pro", 97, _M, True, True),
    (r"gemini-3(\.\d)?-flash", 90, _M, True, True),
    (r"gemini-2\.5-pro", 94, _M, True, True),
    (r"gemini-2\.5-flash-lite", 74, _M, True, True),
    (r"gemini-2\.5-flash", 85, _M, True, True),
    (r"gemini-2\.0-flash-lite", 66, _M, True, True),
    (r"gemini-2\.0-flash", 76, _M, True, True),
    (r"gemini-1\.5-pro", 78, 2 * _M, True, True),
    (r"gemini-1\.5-flash", 66, _M, True, True),
    (r"gemma-3", 60, 128 * _K, True, False),
    (r"gemma", 50, 8 * _K, False, False),
    # xAI
    (r"grok-4", 93, 256 * _K, True, True),
    (r"grok-3-mini", 78, 128 * _K, False, True),
    (r"grok-3", 86, 128 * _K, False, True),
    # DeepSeek
    (r"deepseek-(r1|reasoner)", 88, 128 * _K, False, False),
    (r"deepseek-(v3|chat)", 86, 128 * _K, False, True),
    (r"deepseek", 75, 64 * _K, False, None),
    # Moonshot, Qwen, Z.ai
    (r"kimi-k2", 88, 128 * _K, False, True),
    (r"qwen3-(max|235b)", 87, 128 * _K, False, True),
    (r"qwen3-coder", 86, 256 * _K, False, True),
    (r"qwen3", 78, 128 * _K, False, True),
    (r"qwen-?2\.5-vl", 70, 32 * _K, True, False),
    (r"qwen-?2\.5", 72, 128 * _K, False, True),
    (r"glm-4\.\dv|glm-4v", 76, 64 * _K, True, False),
    (r"glm-4\.[5-9]-(air|flash)", 80, 128 * _K, False, True),
    (r"glm-4\.[5-9]|glm-5", 88, 128 * _K, False, True),
    (r"glm", 65, 128 * _K, False, True),
    # Meta
    (r"llama-4-maverick", 80, _M, True, True),
    (r"llama-4-scout", 74, 128 * _K, True, True),
    (r"llama-3\.[13]-(405|70)b", 74, 128 * _K, False, True),
    (r"llama-3\.[123]-(8|3|1)b", 55, 128 * _K, False, True),
    (r"llama", 55, 8 * _K, False, None),
    # Mistral
    (r"mistral-large", 82, 128 * _K, False, True),
    (r"mistral-medium", 81, 128 * _K, True, True),
    (r"magistral-medium", 82, 40 * _K, False, True),
    (r"magistral-small", 72, 40 * _K, False, True),
    (r"pixtral-large", 80, 128 * _K, True, True),
    (r"pixtral", 66, 128 * _K, True, True),
    (r"codestral|devstral", 74, 128 * _K, False, True),
    (r"mistral-small", 70, 32 * _K, True, True),
    (r"ministral|open-mistral|mistral-tiny", 58, 32 * _K, False, True),
    (r"mixtral", 60, 32 * _K, False, True),
    # Cohere, Microsoft
    (r"command-a", 80, 256 * _K, False, True),
    (r"command-r-plus", 72, 128 * _K, False, True),
    (r"command-r", 64, 128 * _K, False, True),
    (r"phi-4", 64, 16 * _K, False, False),
)

_COMPILED = tuple(
    (re.compile(pattern), ModelProfile(quality, context, vision, tools))
    for pattern, quality, context, vision, tools in _TABLE
)


def _normalize(model: str) -> str:
    name = model.strip().lower().rsplit("/", 1)[-1]
    return name.split(":", 1)[0]


def profile_for(model: str) -> ModelProfile:
    """The profile of ``model``, or ``UNKNOWN`` when no pattern matches."""
    name = _normalize(model)
    for pattern, profile in _COMPILED:
        if pattern.search(name):
            return profile
    return UNKNOWN
