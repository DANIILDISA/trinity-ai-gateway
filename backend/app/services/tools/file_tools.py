"""Data-only file-tool executors (tr-cy0.2.3) — the "sandbox" for Sub-project 1.

`create_file` / `str_replace` operate ONLY on the per-conversation `artifacts`
table via the ORM: no filesystem, no process, no network (architecture §6,
safety_profile = data_only). Each edit writes a new version row in the same
group. Quotas are enforced per-file (schema), per-conversation, and per-user
(architecture §7.4). Executors NEVER raise on bad model input — they return a
`ToolResultPayload(status="error", …)` that is fed back so the model can adapt.
"""
import uuid
from dataclasses import dataclass, field

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.artifact import Artifact
from app.schemas.tools import (
    MAX_ARTIFACT_BYTES,
    CreateFileArgs,
    StrReplaceArgs,
    ToolDiff,
    ToolResultPayload,
    ViewFileArgs,
)
from app.services.tools.diff import line_diff
from app.services.tools.registry import ToolSpec, register
from app.schemas.tools import (
    CREATE_FILE_DEFINITION,
    STR_REPLACE_DEFINITION,
    VIEW_FILE_DEFINITION,
)

# Quota caps (architecture §7.4). Tunable; kept conservative.
MAX_ARTIFACTS_PER_CONVERSATION = 50  # counts logical files (distinct group_id)
MAX_ARTIFACT_BYTES_PER_USER = 20_000_000  # 20 MB summed over ALL version rows
# Cap versions retained per file so the per-user quota (which counts all versions)
# stays bounded and the model can edit freely (older versions are pruned).
MAX_VERSIONS_PER_GROUP = 10


@dataclass
class ToolContext:
    """Minimal execution context handed to a tool executor — no secrets/env."""

    db: AsyncSession
    user_id: uuid.UUID
    conversation_id: uuid.UUID | None
    # ids of artifacts created this request, so the engine can link message_id
    # after the assistant message is saved.
    created_artifact_ids: list[uuid.UUID] = field(default_factory=list)


def _media_type(path: str) -> str:
    lower = path.lower()
    if lower.endswith(".md"):
        return "text/markdown"
    if lower.endswith((".py", ".js", ".ts", ".json", ".html", ".css", ".sh")):
        return "text/plain"
    return "text/plain"


def _first_error(exc: ValidationError) -> str:
    try:
        return exc.errors()[0].get("msg", "Некорректные аргументы инструмента")
    except Exception:  # noqa: BLE001
        return "Некорректные аргументы инструмента"


async def _latest(
    db: AsyncSession,
    conversation_id: uuid.UUID | None,
    path: str,
    *,
    for_update: bool = False,
) -> Artifact | None:
    stmt = (
        select(Artifact)
        .where(
            Artifact.conversation_id == conversation_id,
            Artifact.filename == path,
        )
        .order_by(Artifact.version.desc())
        .limit(1)
    )
    if for_update:
        # Serialize concurrent edits to the same file (version race, §3/§4).
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


async def _user_total_bytes(db: AsyncSession, user_id: uuid.UUID) -> int:
    """Per-user stored artifact bytes, read from the denormalized
    users.artifact_bytes_used column (tr-9wq, audit §H4). Maintained by the
    artifacts_user_bytes DB trigger (migration 018), so the quota check no longer
    runs SUM(size_bytes) over every artifact on each file op."""
    from app.models.user import User

    return int(
        (await db.execute(select(User.artifact_bytes_used).where(User.id == user_id))).scalar_one()
    )


async def _group_versions(
    db: AsyncSession, conversation_id: uuid.UUID | None, group_id: uuid.UUID
) -> list[Artifact]:
    """All version rows for a group, oldest version first."""
    return list(
        (
            await db.execute(
                select(Artifact)
                .where(
                    Artifact.conversation_id == conversation_id,
                    Artifact.group_id == group_id,
                )
                .order_by(Artifact.version.asc())
            )
        ).scalars()
    )


async def _prune_old_versions(
    db: AsyncSession, conversation_id: uuid.UUID | None, group_id: uuid.UUID
) -> None:
    """Keep only the newest MAX_VERSIONS_PER_GROUP versions of a group."""
    versions = await _group_versions(db, conversation_id, group_id)
    excess = len(versions) - MAX_VERSIONS_PER_GROUP
    for row in versions[:excess] if excess > 0 else []:
        await db.delete(row)


async def _conversation_group_count(
    db: AsyncSession, conversation_id: uuid.UUID | None
) -> int:
    rows = (
        await db.execute(
            select(Artifact.group_id).where(
                Artifact.conversation_id == conversation_id
            )
        )
    ).all()
    return len({g for (g,) in rows})


async def create_file(args: dict, ctx: ToolContext) -> ToolResultPayload:
    try:
        parsed = CreateFileArgs(**args)
    except ValidationError as exc:
        return ToolResultPayload(status="error", output=_first_error(exc))

    # create_file never overwrites (D1b): an existing path is an error so the
    # model uses str_replace instead of silently clobbering the file.
    if await _latest(ctx.db, ctx.conversation_id, parsed.path) is not None:
        return ToolResultPayload(
            status="error",
            filename=parsed.path,
            output=f"Файл {parsed.path} уже существует — используйте str_replace для правки.",
        )

    new_bytes = len(parsed.file_text.encode("utf-8"))
    if await _conversation_group_count(ctx.db, ctx.conversation_id) >= MAX_ARTIFACTS_PER_CONVERSATION:
        return ToolResultPayload(
            status="error",
            filename=parsed.path,
            output=f"Достигнут лимит файлов в диалоге ({MAX_ARTIFACTS_PER_CONVERSATION}).",
        )
    current = await _user_total_bytes(ctx.db, ctx.user_id)
    if current + new_bytes > MAX_ARTIFACT_BYTES_PER_USER:
        return ToolResultPayload(
            status="error",
            filename=parsed.path,
            output="Достигнут лимит хранилища артефактов.",
        )

    diff = line_diff("", parsed.file_text)
    art = Artifact(
        user_id=ctx.user_id,
        conversation_id=ctx.conversation_id,
        group_id=uuid.uuid4(),
        version=1,
        filename=parsed.path,
        media_type=_media_type(parsed.path),
        content=parsed.file_text,
        size_bytes=new_bytes,
    )
    ctx.db.add(art)
    await ctx.db.flush()
    ctx.created_artifact_ids.append(art.id)
    return ToolResultPayload(
        status="ok",
        filename=parsed.path,
        diff=ToolDiff(**diff),
        presented=False,
        output=f"Создан файл {parsed.path} ({diff['added']} строк).",
    )


async def str_replace(args: dict, ctx: ToolContext) -> ToolResultPayload:
    try:
        parsed = StrReplaceArgs(**args)
    except ValidationError as exc:
        return ToolResultPayload(status="error", output=_first_error(exc))

    # Lock the group's latest version to serialize concurrent edits (version race).
    existing = await _latest(ctx.db, ctx.conversation_id, parsed.path, for_update=True)
    if existing is None:
        return ToolResultPayload(
            status="error",
            filename=parsed.path,
            output=f"Файл {parsed.path} не найден.",
        )
    occurrences = existing.content.count(parsed.old_str)
    if occurrences == 0:
        # tr-7ml: the model is editing without knowing the current content (common
        # across turns). Nudge it to view_file instead of guessing again — without
        # the nudge it loops str_replace to max_rounds, burning real money.
        return ToolResultPayload(
            status="error",
            filename=parsed.path,
            output=(
                f"Текст для замены не найден в файле {parsed.path}. Вызовите "
                f"view_file({parsed.path}), чтобы увидеть текущее содержимое, и "
                "повторите str_replace с точным фрагментом."
            ),
        )
    if occurrences > 1:
        return ToolResultPayload(
            status="error",
            filename=parsed.path,
            output=f"Текст для замены неоднозначен (встречается {occurrences} раз).",
        )
    new_content = existing.content.replace(parsed.old_str, parsed.new_str, 1)
    new_bytes = len(new_content.encode("utf-8"))
    if new_bytes > MAX_ARTIFACT_BYTES:
        return ToolResultPayload(
            status="error",
            filename=parsed.path,
            output=f"Файл превышает лимит {MAX_ARTIFACT_BYTES} байт.",
        )
    # Per-user quota counts ALL versions; project the bytes this write will prune
    # (oldest versions beyond the cap) so editing doesn't falsely hit the limit.
    versions = await _group_versions(ctx.db, ctx.conversation_id, existing.group_id)
    prune_count = max(0, len(versions) + 1 - MAX_VERSIONS_PER_GROUP)
    prune_bytes = sum(v.size_bytes for v in versions[:prune_count])
    current = await _user_total_bytes(ctx.db, ctx.user_id)
    if current + new_bytes - prune_bytes > MAX_ARTIFACT_BYTES_PER_USER:
        return ToolResultPayload(
            status="error",
            filename=parsed.path,
            output="Достигнут лимит хранилища артефактов.",
        )
    diff = line_diff(existing.content, new_content)
    art = Artifact(
        user_id=ctx.user_id,
        conversation_id=ctx.conversation_id,
        group_id=existing.group_id,
        version=existing.version + 1,
        filename=parsed.path,
        media_type=existing.media_type,
        content=new_content,
        size_bytes=new_bytes,
    )
    ctx.db.add(art)
    await ctx.db.flush()
    ctx.created_artifact_ids.append(art.id)
    await _prune_old_versions(ctx.db, ctx.conversation_id, existing.group_id)
    return ToolResultPayload(
        status="ok",
        filename=parsed.path,
        diff=ToolDiff(**diff),
        presented=False,
        output=f"Изменён файл {parsed.path} (+{diff['added']}/-{diff['removed']}).",
    )


async def view_file(args: dict, ctx: ToolContext) -> ToolResultPayload:
    """Read-only: return the latest content of an existing file (tr-7ml).

    Lets the model SEE a file created in an earlier turn before editing it, so a
    cross-turn str_replace doesn't loop on "text not found". Writes nothing — no
    new version, no artifact id, no card."""
    try:
        parsed = ViewFileArgs(**args)
    except ValidationError as exc:
        return ToolResultPayload(status="error", output=_first_error(exc))

    existing = await _latest(ctx.db, ctx.conversation_id, parsed.path)
    if existing is None:
        return ToolResultPayload(
            status="error",
            filename=parsed.path,
            output=f"Файл {parsed.path} не найден.",
        )
    return ToolResultPayload(
        status="ok",
        filename=parsed.path,
        presented=False,
        output=existing.content,
    )


def register_builtin_tools() -> None:
    """Idempotent (by name) registration of the file tools."""
    register(
        ToolSpec(
            name="create_file",
            definition=CREATE_FILE_DEFINITION,
            executor=create_file,
            safety_profile="data_only",
        )
    )
    register(
        ToolSpec(
            name="str_replace",
            definition=STR_REPLACE_DEFINITION,
            executor=str_replace,
            safety_profile="data_only",
        )
    )
    register(
        ToolSpec(
            name="view_file",
            definition=VIEW_FILE_DEFINITION,
            executor=view_file,
            safety_profile="data_only",
        )
    )


register_builtin_tools()
