"""Agentic loop engine (architecture §2/§3, tr-cy0.2.5).

Drives the model↔tool rounds over the proxy: per round it streams reasoning /
content / tool_call deltas, and when the model emits tool calls it executes them
server-side (sequentially, emission order), feeds the results back, and loops —
until the model answers with no tool calls or a hard bound trips.

This module is intentionally decoupled from billing/persistence: it yields a
unified `EngineEvent` stream (the caller maps events → SSE and sums usage) and
mutates `tool_ctx.created_artifact_ids` as a side effect. The caller enforces the
COST ceiling (architecture §3/§5) by stopping iteration; this module enforces the
count/structure bounds (`max_rounds`, degenerate-loop guard). Invoked ONLY in
tool mode (tools_enabled AND supports_tools) — the non-tool path is unchanged.
"""
import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass

import anyio

from app.services.tools import registry
from app.services.tools.file_tools import ToolContext

MAX_ROUNDS_DEFAULT = 8
DEGENERATE_K_DEFAULT = 3
# Vision round-trip (Phase 4 tr-5ktl): cap images fed back to a vision model per round
# so a plot-heavy cell can't blow up the next round's input tokens.
_MAX_VISION_IMAGES_PER_ROUND = 4
_VISION_NOTE = "Изображения, сгенерированные кодом выше (результат выполнения):"


@dataclass(frozen=True)
class EngineEvent:
    kind: str  # content|reasoning|tool_call_start|tool_args_delta|tool_result|usage|halted|done
    # content / reasoning
    delta: str | None = None
    segment_break: bool = False
    segment_index: int | None = None
    segment_summary: str | None = None
    # tool steps
    step_id: str | None = None
    tool: str | None = None
    title: str | None = None
    status: str | None = None
    filename: str | None = None
    diff: dict | None = None
    presented: bool | None = None
    output: str | None = None
    args_delta: str | None = None
    # raw tool args persisted for reload (run_python only — file-tool args can be
    # large, e.g. create_file's file_text, so they are NOT carried here). tr-jk4j.
    input: str | None = None
    # code-execution metadata (run_python, epic tr-s3y1 Phase 2)
    stderr: str | None = None
    duration_ms: int | None = None
    truncated: bool = False
    cost_micro_rub: int = 0
    # rich outputs (run_python, Phase 4 tr-5ktl): tagged results (image/png, text/html,
    # text/plain) for the run-card. None for file tools / empty results.
    results: list | None = None
    # usage (per round) / halt reason
    usage: dict | None = None
    reason: str | None = None


def _title(tool: str, path: str | None) -> str:
    # At tool_call_start the path hasn't streamed yet → no placeholder; the chip
    # shows the filename. At tool_result the path is known and gets appended.
    if tool == "create_file":
        return f"Создание файла {path}" if path else "Создание файла"
    if tool == "str_replace":
        return f"Правка файла {path}" if path else "Правка файла"
    if tool == "view_file":
        return f"Просмотр файла {path}" if path else "Просмотр файла"
    if tool == "run_python":
        return "Выполнение Python"
    return tool


def _canonical(args: dict) -> str:
    # Canonical JSON so key reordering can't evade the degenerate-loop guard.
    try:
        return json.dumps(args, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(args)


def _is_degenerate(round_sigs: list[tuple], k: int) -> bool:
    if len(round_sigs) < k:
        return False
    last = round_sigs[-1]
    return all(sig == last for sig in round_sigs[-k:]) and last != ()


async def run_agentic_loop(
    *,
    proxy,
    model_id: str,
    context_messages: list[dict],
    tool_ctx: ToolContext,
    reasoning_budget: int | None = None,
    tool_definitions: list[dict] | None = None,
    plugins: list[dict] | None = None,
    max_rounds: int = MAX_ROUNDS_DEFAULT,
    degenerate_k: int = DEGENERATE_K_DEFAULT,
    budget_guard=None,
    vision_capable: bool = False,
) -> AsyncGenerator[EngineEvent, None]:
    """`budget_guard()` (optional) is checked before each round; returning False
    stops the loop with a `halted` event (the caller enforces the COST ceiling
    here — architecture §3/§5)."""
    definitions = tool_definitions if tool_definitions is not None else registry.definitions()
    context = list(context_messages)
    round_sigs: list[tuple] = []

    for round_idx in range(max_rounds):
        if budget_guard is not None and not budget_guard():
            yield EngineEvent(kind="halted", reason="budget")
            yield EngineEvent(kind="done")
            return
        calls: dict[int, dict] = {}
        order: list[int] = []

        async for chunk in proxy.proxy_to_openrouter_with_retry(
            context, model_id, plugins=plugins,
            reasoning_budget=reasoning_budget, tools=definitions
        ):
            if chunk.type == "content" and chunk.delta:
                yield EngineEvent(kind="content", delta=chunk.delta)
            elif chunk.type == "reasoning":
                yield EngineEvent(
                    kind="reasoning",
                    delta=chunk.delta,
                    segment_break=chunk.segment_break,
                    segment_index=chunk.segment_index,
                    segment_summary=chunk.segment_summary,
                )
            elif chunk.type == "tool_call":
                idx = chunk.tool_call_index or 0
                step_id = f"tc-{round_idx}-{idx}"
                if idx not in calls:
                    calls[idx] = {"id": None, "name": None, "args": ""}
                    order.append(idx)
                call = calls[idx]
                if chunk.tool_call_id:
                    call["id"] = chunk.tool_call_id
                if chunk.name:
                    call["name"] = chunk.name
                    yield EngineEvent(
                        kind="tool_call_start",
                        step_id=step_id,
                        tool=chunk.name,
                        title=_title(chunk.name, None),
                    )
                if chunk.arguments_delta:
                    call["args"] += chunk.arguments_delta
                    yield EngineEvent(
                        kind="tool_args_delta",
                        step_id=step_id,
                        args_delta=chunk.arguments_delta,
                    )
            elif chunk.type == "usage":
                yield EngineEvent(
                    kind="usage",
                    usage={
                        "input_tokens": chunk.input_tokens or 0,
                        "output_tokens": chunk.output_tokens or 0,
                        "reasoning_tokens": chunk.reasoning_tokens or 0,
                        "cost_usd": chunk.cost_usd,
                        # tr-xzy: surface OpenRouter's generation id so the caller can
                        # persist it for cost reconciliation (parity with single-shot).
                        "openrouter_request_id": chunk.openrouter_request_id,
                    },
                )
            elif chunk.type == "done":
                break

        if not calls:
            yield EngineEvent(kind="done")
            return

        # Append the assistant turn that carries the tool calls, then execute each
        # call sequentially (deterministic for side effects) and feed results back.
        context.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": calls[i]["id"] or f"tc-{round_idx}-{i}",
                        "type": "function",
                        "function": {
                            "name": calls[i]["name"],
                            "arguments": calls[i]["args"],
                        },
                    }
                    for i in order
                ],
            }
        )

        this_round_sig = []
        round_images: list[str] = []  # base64 PNGs to feed back to a vision model
        for i in order:
            call = calls[i]
            step_id = f"tc-{round_idx}-{i}"
            name = call["name"]
            try:
                args = json.loads(call["args"] or "{}")
                if not isinstance(args, dict):
                    args = {}
            except (TypeError, ValueError):
                args = {}

            spec = registry.get(name) if name else None
            if spec is None:
                from app.schemas.tools import ToolResultPayload

                result = ToolResultPayload(
                    status="error", output=f"Неизвестный инструмент: {name}"
                )
                # No DB write, but end any txn this round opened so the request
                # connection is released before the next round's create() TTFB (tr-ans).
                await tool_ctx.db.commit()
            else:
                # Window #2 (tr-ans): commit the tool's flush the instant it lands so
                # the request connection is released before the next round's create()
                # TTFB (the dominant pool-hold for agentic loops). The executor flushes
                # the artifact AND appends its id to created_artifact_ids; both + the
                # commit run inside ONE shielded scope. Otherwise a CancelledError
                # (ASGI client disconnect) delivered mid-executor could leave the
                # artifact flushed-but-uncommitted while its id is already captured ->
                # the abort rollback discards the row and link-by-id finds nothing ->
                # the streamed, already-charged answer's artifact is LOST. The shield
                # defers cancellation until AFTER the commit, so created_artifact_ids
                # only ever holds COMMITTED ids -> link-by-id is total. anyio shield,
                # not asyncio.shield (broken under Starlette/anyio cancel scopes —
                # tr-04a). commit() keeps the caller's pricing/reservation loaded
                # (expire_on_commit=False).
                with anyio.CancelScope(shield=True):
                    result = await spec.executor(args, tool_ctx)
                    await tool_ctx.db.commit()

            path = result.filename or args.get("path")
            yield EngineEvent(
                kind="tool_result",
                step_id=step_id,
                tool=name,
                title=_title(name, path),
                status="done" if result.status == "ok" else "error",
                filename=result.filename,
                diff=result.diff.model_dump() if result.diff else None,
                presented=bool(result.presented),
                output=result.output,
                # Persist the submitted code for run_python so the run-card shows it
                # on reload; skip large file-tool args (tr-jk4j).
                input=call["args"] if name == "run_python" else None,
                stderr=result.stderr,
                duration_ms=result.duration_ms,
                truncated=result.truncated,
                cost_micro_rub=result.cost_micro_rub,
                results=getattr(result, "results", None) or None,
            )
            context.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"] or step_id,
                    "content": result.output,
                }
            )
            # Vision round-trip (Phase 4): collect any PNGs this tool produced so a
            # vision-capable model can see its own output next round.
            if vision_capable:
                for r in getattr(result, "results", None) or ():
                    if isinstance(r, dict) and r.get("type") == "image/png" and r.get("data"):
                        round_images.append(r["data"])
            this_round_sig.append((name, _canonical(args)))

        # Feed the round's images back as ONE user message AFTER all tool results (tool
        # messages must directly follow the assistant tool_calls; a user message with
        # image_url parts then lets the model reason about the rendered output).
        if vision_capable and round_images:
            capped = round_images[:_MAX_VISION_IMAGES_PER_ROUND]
            context.append({
                "role": "user",
                "content": [
                    {"type": "text", "text": _VISION_NOTE},
                    *(
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{d}"}}
                        for d in capped
                    ),
                ],
            })

        round_sigs.append(tuple(this_round_sig))
        if _is_degenerate(round_sigs, degenerate_k):
            yield EngineEvent(kind="halted", reason="degenerate_loop")
            yield EngineEvent(kind="done")
            return

    # Exhausted max_rounds with the model still wanting tools.
    yield EngineEvent(kind="halted", reason="max_rounds")
    yield EngineEvent(kind="done")
