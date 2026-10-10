"""OpenAI-compatible chat schemas.

Unknown fields (``response_format``, ``user``, ...) are ignored at every depth,
never rejected or echoed.
"""
from __future__ import annotations

import time
from typing import Any

from fastuuid import uuid7
from pydantic import BaseModel, ConfigDict, Field


def _now() -> int:
    """Current Unix timestamp (UTC)."""
    return int(time.time())


class ChatMessage(BaseModel):
    """A single chat message; unknown fields are ignored."""

    model_config = ConfigDict(extra="ignore")

    role: str = Field(min_length=1)
    content: Any | None = None
    name: str | None = None
    # Tool-use turns.
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None


class ChatCompletionRequest(BaseModel):
    """OpenAI chat completion request (unknown fields are ignored)."""

    model_config = ConfigDict(extra="ignore")

    model: str = Field(min_length=1)
    messages: list[ChatMessage] = Field(default_factory=list, min_length=1)
    stream: bool = False
    use_history: bool = True

    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, gt=0)
    max_completion_tokens: int | None = Field(default=None, gt=0)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    frequency_penalty: float | None = Field(default=None, ge=-2.0, le=2.0)
    presence_penalty: float | None = Field(default=None, ge=-2.0, le=2.0)
    stop: str | list[str] | None = None
    seed: int | None = None
    # Only one choice is returned and persisted, so n > 1 is rejected.
    n: int | None = Field(default=None, ge=1, le=1)
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    parallel_tool_calls: bool | None = None


class ChatCompletionChoice(BaseModel):
    """A chat completion choice."""

    index: int
    message: ChatMessage
    finish_reason: str | None = None


class ChatCompletionUsage(BaseModel):
    """Token usage statistics."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionResponse(BaseModel):
    """OpenAI chat completion response (blocking)."""

    id: str
    object: str = "chat.completion"
    created: int = Field(default_factory=_now)
    model: str
    choices: list[ChatCompletionChoice]
    usage: ChatCompletionUsage = Field(default_factory=ChatCompletionUsage)
    logprobs: None = None


def generate_chat_id() -> str:
    """Generate a Gateway-owned chat completion ID (``chatcmpl-<uuid7>``)."""
    return f"chatcmpl-{uuid7()}"
