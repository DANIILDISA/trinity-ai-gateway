"""Extensible tool registry (architecture §2).

The engine is tool-agnostic: it discovers tool definitions and executors here.
File tools register at import (Sub-project 1); web / code plugins register later.
Each `executor` is `async (args: dict, ctx: ToolContext) -> ToolResultPayload`
and receives ONLY its declared args + a minimal context (no secrets/env).
"""
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.schemas.tools import ToolResultPayload

ToolExecutor = Callable[[dict, Any], Awaitable[ToolResultPayload]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    # OpenAI/OpenRouter function-calling definition sent to the model.
    definition: dict
    executor: ToolExecutor
    # Declared isolation class enforced by the engine (e.g. "data_only").
    safety_profile: str


_registry: dict[str, ToolSpec] = {}


def register(spec: ToolSpec) -> None:
    _registry[spec.name] = spec


def get(name: str) -> ToolSpec | None:
    return _registry.get(name)


def definitions() -> list[dict]:
    """OpenAI-format tool definitions for every registered tool, in registration order."""
    return [spec.definition for spec in _registry.values()]


def clear_registry() -> None:
    """Test-only: reset registrations between cases."""
    _registry.clear()
