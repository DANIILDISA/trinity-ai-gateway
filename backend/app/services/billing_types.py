"""Shared dataclasses for the billing pipeline (BILLING_LOGIC.md §2.2)."""
from dataclasses import dataclass, field
from time import perf_counter


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int
    # Reasoning portion of output_tokens (extended thinking); 0 when not used or
    # when estimated from collected text (estimation can't separate reasoning).
    reasoning_tokens: int = 0
    cost_usd: float | None = None
    openrouter_request_id: str | None = None


@dataclass(frozen=True)
class Finalization:
    actual_cost_micro_rub: int
    balance_after_micro_rub: int
    refunded_micro_rub: int
    overdraft_absorbed_micro_rub: int = 0


@dataclass
class StreamResult:
    content: str
    actual_usage: TokenUsage | None
    estimated_output_tokens: int


@dataclass(frozen=True)
class SSEEvent:
    event: str
    data: dict


@dataclass
class RequestTiming:
    """Tracks step latencies for usage_logs. All durations in milliseconds.

    `start(name)` and `stop(name)` bracket a step. `total` is bracketed
    automatically by __post_init__. `mark_first_byte_if_needed()` records
    the OpenRouter TTFB on the first content chunk only.
    """

    _starts: dict[str, float] = field(default_factory=dict)
    _durations_ms: dict[str, int] = field(default_factory=dict)
    _request_started_at: float = field(default_factory=perf_counter)
    _first_byte_recorded: bool = False

    def start(self, name: str) -> None:
        self._starts[name] = perf_counter()

    def stop(self, name: str) -> None:
        started = self._starts.pop(name, None)
        if started is None:
            return
        self._durations_ms[name] = int((perf_counter() - started) * 1000)

    def mark_first_byte_if_needed(self) -> None:
        if self._first_byte_recorded:
            return
        self._first_byte_recorded = True
        self._durations_ms["openrouter_ttfb"] = int(
            (perf_counter() - self._request_started_at) * 1000
        )

    def get(self, name: str) -> int | None:
        if name == "total":
            return int((perf_counter() - self._request_started_at) * 1000)
        if name in self._durations_ms:
            return self._durations_ms[name]
        # Timer still running — return elapsed-so-far so a caller reading
        # `finalization_ms` from inside finalize_reservation gets a value
        # without needing the orchestrator to pre-stop the clock.
        started = self._starts.get(name)
        if started is None:
            return None
        return int((perf_counter() - started) * 1000)
