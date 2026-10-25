"""run_python tool executor — runs untrusted Python in the sandbox (epic tr-s3y1 Phase 2).

Mirrors the file-tool executor contract (registry.ToolExecutor): it NEVER raises on
bad model input or a sandbox failure — it returns a ToolResultPayload(status="error", …)
that is fed back so the model can self-correct. It maps the sandbox ExecutionResult to
the payload and prices the run in rubles (config-driven, spec §7) so the agentic loop's
budget guard can meter and charge execution time.

NOTE: registration is intentionally NOT done at import (unlike file_tools). The tool is
wired into the live registry only alongside the execution-time billing (tr-nyil), so the
chat never offers an unbilled code-execution tool.
"""
import base64
import logging
import math
import mimetypes
import uuid

import httpx
from pydantic import ValidationError

from app.config import settings
from app.core.redis import get_redis_client
from app.models.artifact import Artifact
from app.schemas.tools import RUN_PYTHON_DEFINITION, RunPythonArgs, ToolResultPayload
from app.services.chat_file_service import (
    included_files_for_sandbox,
    included_files_for_sandbox_with_hash,
    included_files_seed_hash,
)
from app.services.sandbox.client import ExecutionResult, SandboxClient
from app.services.tools.file_tools import (
    MAX_ARTIFACTS_PER_CONVERSATION,
    MAX_ARTIFACT_BYTES_PER_USER,
    _conversation_group_count,
    _user_total_bytes,
)
from app.services.tools.registry import ToolSpec, register

# Agent-facing error messages (spec 2026-06-26 §2.4) — the model relays these to the
# user, so each failure mode is distinct and actionable.
_MSG_BAD_ARGS = "Некорректные аргументы run_python: требуется непустой код (поле code)."
_MSG_PER_USER = (
    "Слишком много одновременных запусков кода — дождитесь завершения предыдущих и повторите."
)
_MSG_OVERLOADED = (
    "Сервер сейчас перегружен, выполнить код не получается — попробуйте через несколько секунд."
)
_MSG_UNREACHABLE = "Песочница недоступна, выполнить код сейчас нельзя."

_log = logging.getLogger(__name__)


async def _acquire_user_slot(user_id) -> tuple[bool, object | None, str | None]:
    """Per-user concurrency slot via an atomic Redis counter (spec §2.2).

    Returns (ok, redis, key). ok=False → reject immediately (the INCR was already
    rolled back). Fails OPEN if Redis is unavailable — the global sandbox cap still
    protects resources, so a Redis hiccup must not block code execution."""
    if user_id is None:
        return True, None, None
    try:
        redis = get_redis_client()
        key = f"sandbox:conc:{user_id}"
        count = await redis.incr(key)
    except Exception:  # noqa: BLE001 - Redis unavailable: fail open (global cap protects)
        return True, None, None
    # Enforce the cap on the KNOWN count first — before any best-effort op can fail
    # and trip the fail-open path (which would bypass the cap AND leak the INCR).
    if count > settings.sandbox_max_concurrent_per_user:
        await _release_user_slot(redis, key)  # roll back our INCR
        return False, None, None
    # TTL refresh is best-effort: it guards against a crashed worker leaking a slot,
    # but a failure here must NOT bypass the cap or leak — the caller still gets
    # (redis, key) so the finally-path DECRs.
    try:
        await redis.expire(key, settings.sandbox_conc_slot_ttl_seconds)
    except Exception:  # noqa: BLE001
        pass
    return True, redis, key


async def _release_user_slot(redis, key) -> None:
    if redis is None or key is None:
        return
    try:
        await redis.decr(key)
    except Exception:  # noqa: BLE001
        pass


def _run_cost_micro_rub(duration_ms: int) -> int:
    """Flat per-run fee + per-wall-second rate, in micro-rubles (spec §7)."""
    return settings.sandbox_run_base_micro_rub + math.ceil(
        duration_ms * settings.sandbox_sec_micro_rub / 1000
    )


# Non-executable media-type allowlist (audit B1): a generated *.html/*.svg/*.js must never
# be served with its executable content type (it would run when opened from the OS file
# manager). Mirrors sandbox.kernels._SAFE_MEDIA_TYPES. Anything else → octet-stream.
_SAFE_MEDIA_TYPES = frozenset(
    {
        "text/plain", "text/csv", "application/json", "application/pdf",
        "image/png", "image/jpeg", "image/gif", "image/webp",
    }
)


def media_type_for(filename: str) -> str:
    """MIME type for a generated/seeded file by extension (Phase 5, tr-an0g), constrained to
    a non-executable allowlist (B1). Disallowed/unknown types → application/octet-stream so
    they download as inert bytes (never text/html / javascript / svg+xml)."""
    guessed = mimetypes.guess_type(filename)[0]
    return guessed if guessed in _SAFE_MEDIA_TYPES else "application/octet-stream"


# Mirrors the frontend binaryBlob.isBinaryMediaType: text/* and a few text-ish application
# types are stored RAW; everything else (image/*, office formats, parquet, octet-stream…)
# is stored base64. Keeps generated-file artifacts consistent with create_file so every
# shared download/gallery path reproduces the bytes from the media type alone.
_RAW_TEXT_MEDIA = frozenset(
    {"application/json", "application/xml", "application/javascript", "image/svg+xml"}
)


def _is_binary_media(media_type: str) -> bool:
    m = (media_type or "").split(";")[0].strip().lower()
    if not m or m.startswith("text/"):
        return False
    return m not in _RAW_TEXT_MEDIA


async def _persist_binary_artifacts(ctx, results: list) -> None:
    """Persist each BINARY rich result as a downloadable Artifact (reuse the artifact
    system), so it survives reload + can be downloaded. Handles two shapes:
      - image/png (Phase 4, tr-5ktl): a matplotlib figure → plot-<hex>.png;
      - file       (Phase 5, tr-an0g): a generated workspace file the manager already read
        off the sandbox → media_type by extension, filename the workdir-relative path.
    Mutates each persisted result in place, adding `artifact_id` (+ `filename` for images);
    the heavy base64 stays for the LIVE render and is dropped at persist time
    (billing_service._persistable_results).

    Best-effort + quota-bounded: with no db/conversation, or once the per-user byte quota /
    per-conversation file cap is hit, the remaining items stay inline-only (the live card
    still shows + downloads them). Quota checks mirror create_file's (read-then-flush —
    same TOCTOU profile; the advisory-lock fix is M11, deferred to group-e)."""
    db = getattr(ctx, "db", None)
    conversation_id = getattr(ctx, "conversation_id", None)
    user_id = getattr(ctx, "user_id", None)
    if db is None or user_id is None or conversation_id is None or not results:
        return
    # L5 (tr-dtc8) REVERTED: a single AsyncSession does NOT permit concurrent operations —
    # asyncio.gather over the two reads raises InvalidRequestError ("provisioning a new
    # connection; concurrent operations are not permitted"), which run_python's except would
    # swallow → the artifact silently fails to persist. The two reads stay sequential.
    current = await _user_total_bytes(db, user_id)
    group_count = await _conversation_group_count(db, conversation_id)
    pending: list[tuple[dict, Artifact]] = []
    for r in results:
        if not isinstance(r, dict) or not r.get("data"):
            continue
        rtype = r.get("type")
        if rtype == "image/png":
            filename = f"plot-{uuid.uuid4().hex[:8]}.png"
            media_type = "image/png"
        elif rtype == "file":
            filename = r.get("filename")
            if not isinstance(filename, str) or not filename:
                continue
            media_type = r.get("media_type") or media_type_for(filename)
        else:
            continue
        # Storage convention (matches create_file + binaryBlob.isBinaryMediaType): a text-
        # media file is stored RAW (UTF-8), a binary one (image/png, xlsx, parquet…) as
        # base64 — so every shared download/gallery path reproduces the exact bytes from the
        # media type. The manager always sends base64 on the wire, so transcode here.
        # L7 (tr-dtc8): only the text branch needs the decoded bytes — don't b64decode (and
        # re-encode for the size) a large binary just to discard it. The binary branch no
        # longer validates the base64 upfront; the sole producer (sandbox manager
        # attach_file_bytes) always emits valid base64, and a (theoretically) malformed one
        # would surface at download time, never as a quota miscount.
        if rtype == "file" and not _is_binary_media(media_type):
            try:
                content = base64.b64decode(r["data"], validate=True).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                # Mislabeled text (really binary) or malformed base64 — keep base64 + force a
                # binary media type so the download path base64-decodes instead of corrupting it.
                content, media_type = r["data"], "application/octet-stream"
            size = len(content.encode("utf-8"))  # stored as raw UTF-8 text
        else:
            content = r["data"]  # base64 (image/png + binary files): stored as ASCII base64
            size = len(content)  # L7: ASCII base64 → byte length == str length (no copy)
        if group_count >= MAX_ARTIFACTS_PER_CONVERSATION or current + size > MAX_ARTIFACT_BYTES_PER_USER:
            break  # over quota → leave the rest inline-only
        pending.append((
            r,
            Artifact(
                user_id=user_id,
                conversation_id=conversation_id,
                group_id=uuid.uuid4(),
                version=1,
                filename=filename,
                media_type=media_type,
                content=content,  # raw text or base64 — the GET /artifact endpoint serves it back
                size_bytes=size,
            ),
        ))
        current += size
        group_count += 1
    if not pending:
        return
    # One flush for all artifacts (audit S5): PG RETURNING populates every .id in a single
    # round-trip, instead of a sequential flush per artifact.
    db.add_all([art for _, art in pending])
    await db.flush()
    for r, art in pending:
        ctx.created_artifact_ids.append(art.id)
        r["artifact_id"] = str(art.id)
        r["filename"] = art.filename


def _format_output(result: ExecutionResult) -> str:
    """Model-facing text: the program's stdout + stderr (so a traceback drives
    self-correction), plus the wall-clock detail on a timeout."""
    parts: list[str] = []
    if result.stdout:
        parts.append(result.stdout.rstrip("\n"))
    if result.stderr:
        parts.append(result.stderr.rstrip("\n"))
    if result.status == "timeout" and result.detail:
        parts.append(result.detail)
    return "\n".join(parts) or "(пустой вывод)"


async def run_python(args: dict, ctx) -> ToolResultPayload:
    try:
        parsed = RunPythonArgs(**args)
    except ValidationError:
        return ToolResultPayload(status="error", output=_MSG_BAD_ARGS)

    # Per-user cap first — reject before touching the sandbox under monopolization.
    ok, redis, key = await _acquire_user_slot(getattr(ctx, "user_id", None))
    if not ok:
        return ToolResultPayload(status="error", output=_MSG_PER_USER)

    conversation_id = getattr(ctx, "conversation_id", None)
    conv = str(conversation_id) if conversation_id else None
    # Seed the conversation's included uploads (tr-an0g.4): the manager writes them into
    # the kernel workdir's inputs/ so code can `open('inputs/<name>')`. Best-effort — a
    # fetch failure must never block the run (the cell just won't find its inputs).
    db = getattr(ctx, "db", None)
    seed_hash: str | None = None
    if db is not None and conversation_id is not None:
        try:
            seed_hash = await included_files_seed_hash(db, conversation_id)
        except Exception:  # noqa: BLE001 - a hash hiccup must never block the run
            seed_hash = None
    try:
        client = SandboxClient()

        async def _send(files, h):
            return await client.execute(
                code=parsed.code,
                conversation_id=conv,
                timeout=settings.sandbox_exec_timeout_seconds,
                files=files,
                seed_hash=h,
            )

        if conv is not None and seed_hash is not None:
            # B3 (tr-f63e): probe with no bytes; the manager RUNS the cell in this same call on
            # a hash match, or answers needs_seed (code NOT run) on a miss — so the probe must
            # use the normal timeout, never a short one (audit SB-1: a short probe timeout +
            # a >Ns cell would spuriously retry and execute the cell twice + under-bill).
            result = await _send([], seed_hash)
            if result.needs_seed:  # M5: typed access — fail loud on schema drift
                try:
                    seed_files, retry_hash = await included_files_for_sandbox_with_hash(db, conversation_id)
                except Exception:  # noqa: BLE001 - seeding is best-effort
                    # SB-2: fall back to seed_hash=None (NOT the missed hash) so the retry seeds
                    # an empty inputs/ and actually RUNS the cell — sending the missed hash would
                    # re-enter the probe branch, miss again, and silently never execute.
                    _log.warning("run_python: seed fetch failed for conv %s — running without uploads", conv)
                    seed_files, retry_hash = [], None
                # M2: bytes + hash come from ONE snapshot, so they can never disagree.
                result = await _send(seed_files, retry_hash)
        else:
            # No uploads / stateless run → today's single call (seed_hash=None clears inputs/).
            result = await _send([], None)
    except httpx.HTTPStatusError as exc:
        # 503 = global concurrency cap; tell the agent it's an overload, not a crash.
        overloaded = exc.response is not None and exc.response.status_code == 503
        return ToolResultPayload(
            status="error", output=_MSG_OVERLOADED if overloaded else _MSG_UNREACHABLE
        )
    except Exception:  # noqa: BLE001 - never crash the chat on a sandbox failure (§9)
        # Infrastructure failure: the code never ran, so the user is not charged.
        return ToolResultPayload(status="error", output=_MSG_UNREACHABLE)
    finally:
        await _release_user_slot(redis, key)

    output = _format_output(result)
    if getattr(result, "reset", False):
        # The conversation's persistent kernel was reset (timeout/crash/eviction) —
        # tell the model so it knows prior variables/imports are gone (Phase 3).
        output += "\n\n[Состояние сессии сброшено: переменные и импорты предыдущих запусков очищены.]"
    if result.status == "ok" and result.results:
        # Phase 4/5: persist generated PNGs + workspace files as downloadable artifacts
        # (mutates results to carry artifact_id/filename). Never fail the run on a
        # persistence hiccup.
        try:
            await _persist_binary_artifacts(ctx, result.results)
        except Exception:  # noqa: BLE001 - a persistence error must not lose the run
            pass
    return ToolResultPayload(
        status="ok" if result.status == "ok" else "error",
        output=output,
        stderr=result.stderr or None,
        duration_ms=result.duration_ms,
        truncated=result.truncated,
        cost_micro_rub=_run_cost_micro_rub(result.duration_ms),
        results=result.results,  # Phase 4: rich outputs (matplotlib PNG, DataFrame HTML)
    )


def register_code_exec_tool() -> None:
    """Register run_python in the global tool registry. Called from the live wiring
    only once execution-time billing is in place (tr-nyil)."""
    register(
        ToolSpec(
            name="run_python",
            definition=RUN_PYTHON_DEFINITION,
            executor=run_python,
            safety_profile="code_execution",
        )
    )
