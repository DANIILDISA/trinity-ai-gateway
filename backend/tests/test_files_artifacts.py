"""AI-artifacts gallery endpoint (tr-4eg).

The «Файлы» gallery has an opt-in "Созданные ИИ" mode listing the model-authored
artifacts the user created via the file tools. This endpoint returns the LATEST
version per logical file (distinct group_id), ownership-scoped, newest-first, with
enough to display + download. It must NOT include the user's uploaded chat_files.
"""
import uuid as _uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.artifact import Artifact
from app.models.chat_file import ChatFile

REGISTER_URL = "/api/v1/auth/register"
CONV_URL = "/api/v1/chat/conversations"
ARTIFACTS_URL = "/api/v1/files/artifacts"
MODEL_ID = "openai/gpt-4.1-mini"


async def _register(http_client: AsyncClient, email: str) -> str:
    resp = await http_client.post(
        REGISTER_URL,
        json={"email": email, "password": "SecurePass1", "display_name": "Тест"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _create_conversation(http_client: AsyncClient, token: str) -> str:
    resp = await http_client.post(
        CONV_URL, json={"model_id": MODEL_ID, "title": "Артефакты"}, headers=_auth(token)
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _me_id(http_client: AsyncClient, token: str) -> _uuid.UUID:
    resp = await http_client.get("/api/v1/users/me", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    return _uuid.UUID(resp.json()["id"])


async def _add_artifact(
    db: AsyncSession,
    user_id: _uuid.UUID,
    conversation_id: _uuid.UUID,
    group_id: _uuid.UUID,
    version: int,
    filename: str = "roadmap.md",
) -> None:
    content = f"# Roadmap v{version}\n"
    db.add(
        Artifact(
            user_id=user_id,
            conversation_id=conversation_id,
            message_id=None,
            group_id=group_id,
            version=version,
            filename=filename,
            media_type="text/markdown",
            content=content,
            size_bytes=len(content.encode("utf-8")),
        )
    )
    await db.commit()


async def _add_upload(
    db: AsyncSession, user_id: _uuid.UUID, conversation_id: _uuid.UUID
) -> None:
    db.add(
        ChatFile(
            user_id=user_id,
            conversation_id=conversation_id,
            message_id=None,
            filename="uploaded.txt",
            media_type="text/plain",
            kind="text",
            base64_data="dXBsb2Fk",
            file_size_bytes=6,
            token_estimate=2,
        )
    )
    await db.commit()


async def test_artifacts_gallery_latest_per_group_excludes_uploads(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _register(http_client, "artifacts_a@example.com")
    user_id = await _me_id(http_client, token)
    conv_id = _uuid.UUID(await _create_conversation(http_client, token))

    # One uploaded file (must NOT appear) + two versions of one AI artifact.
    await _add_upload(db_session, user_id, conv_id)
    group = _uuid.uuid4()
    await _add_artifact(db_session, user_id, conv_id, group, version=1)
    await _add_artifact(db_session, user_id, conv_id, group, version=2)

    resp = await http_client.get(ARTIFACTS_URL, headers=_auth(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["filename"] == "roadmap.md"
    assert item["media_type"] == "text/markdown"
    assert item["version"] == 2  # latest version
    assert item["version_count"] == 2
    assert item["conversation_id"] == str(conv_id)
    assert item["size_bytes"] > 0
    # The uploaded chat_file must not leak into the artifacts mode.
    assert all(i["filename"] != "uploaded.txt" for i in body["items"])


async def test_artifacts_gallery_is_ownership_scoped(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    owner = await _register(http_client, "artifacts_owner@example.com")
    owner_id = await _me_id(http_client, owner)
    owner_conv = _uuid.UUID(await _create_conversation(http_client, owner))
    await _add_artifact(db_session, owner_id, owner_conv, _uuid.uuid4(), version=1)

    other = await _register(http_client, "artifacts_other@example.com")
    resp = await http_client.get(ARTIFACTS_URL, headers=_auth(other))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 0
    assert body["items"] == []


async def test_artifacts_gallery_multiple_groups_newest_first(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _register(http_client, "artifacts_multi@example.com")
    user_id = await _me_id(http_client, token)
    conv_id = _uuid.UUID(await _create_conversation(http_client, token))

    g1 = _uuid.uuid4()
    await _add_artifact(db_session, user_id, conv_id, g1, version=1, filename="old.md")
    g2 = _uuid.uuid4()
    await _add_artifact(db_session, user_id, conv_id, g2, version=1, filename="new.md")

    resp = await http_client.get(ARTIFACTS_URL, headers=_auth(token))
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [i["filename"] for i in items] == ["new.md", "old.md"]
    assert all(i["version_count"] == 1 for i in items)
