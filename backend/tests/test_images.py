"""Gallery + conversation image-lifecycle tests.

Covers the two behaviours added with conversation archive/delete handling:
  1. Permanent (hard) delete of an archived conversation also deletes its
     `chat_images` rows — from the DB and therefore the gallery.
  2. The gallery flags images whose conversation is archived (soft-deleted)
     with `archived=true` so the frontend can dim them, while leaving the
     image itself in place.
"""
import base64
import uuid as _uuid

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat_image import ChatImage

REGISTER_URL = "/api/v1/auth/register"
CONV_URL = "/api/v1/chat/conversations"
GALLERY_URL = "/api/v1/images/gallery"
MODEL_ID = "openai/gpt-4.1-mini"

# A minimal valid base64 payload (1x1 PNG-ish bytes — content is irrelevant here).
_PAYLOAD = base64.b64encode(b"trinity-test-image-bytes").decode()


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
        CONV_URL, json={"model_id": MODEL_ID, "title": "Картинки"}, headers=_auth(token)
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _add_image(
    db: AsyncSession, user_id: _uuid.UUID, conversation_id: _uuid.UUID | None
) -> _uuid.UUID:
    img = ChatImage(
        user_id=user_id,
        conversation_id=conversation_id,
        message_id=None,
        thumbnail_base64=_PAYLOAD,
        full_base64=_PAYLOAD,
        media_type="image/png",
        file_size_bytes=len(base64.b64decode(_PAYLOAD)),
    )
    db.add(img)
    await db.commit()
    await db.refresh(img)
    return img.id


async def _me_id(http_client: AsyncClient, token: str) -> _uuid.UUID:
    resp = await http_client.get("/api/v1/users/me", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    return _uuid.UUID(resp.json()["id"])


# ── Permanent delete cleans up images ──────────────────────────────────────


async def test_permanent_delete_removes_conversation_images(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _register(http_client, "img_test_a@example.com")
    user_id = await _me_id(http_client, token)
    conv_id = await _create_conversation(http_client, token)

    await _add_image(db_session, user_id, _uuid.UUID(conv_id))
    await _add_image(db_session, user_id, _uuid.UUID(conv_id))

    # Archive, then permanently delete.
    assert (await http_client.delete(
        f"{CONV_URL}/{conv_id}", headers=_auth(token)
    )).status_code == 204
    assert (await http_client.delete(
        f"{CONV_URL}/{conv_id}/permanent", headers=_auth(token)
    )).status_code == 204

    # No chat_images rows survive for this user.
    db_session.expire_all()
    remaining = (
        await db_session.execute(
            select(func.count()).select_from(ChatImage).where(
                ChatImage.user_id == user_id
            )
        )
    ).scalar()
    assert remaining == 0

    # And the gallery is empty.
    gallery = await http_client.get(GALLERY_URL, headers=_auth(token))
    assert gallery.status_code == 200
    assert gallery.json()["total"] == 0
    assert gallery.json()["items"] == []


async def test_permanent_delete_only_removes_its_own_images(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _register(http_client, "img_test_a@example.com")
    user_id = await _me_id(http_client, token)
    keep_conv = await _create_conversation(http_client, token)
    drop_conv = await _create_conversation(http_client, token)

    keep_img = await _add_image(db_session, user_id, _uuid.UUID(keep_conv))
    await _add_image(db_session, user_id, _uuid.UUID(drop_conv))

    await http_client.delete(f"{CONV_URL}/{drop_conv}", headers=_auth(token))
    await http_client.delete(f"{CONV_URL}/{drop_conv}/permanent", headers=_auth(token))

    db_session.expire_all()
    surviving = (
        await db_session.execute(select(ChatImage.id).where(ChatImage.user_id == user_id))
    ).scalars().all()
    assert surviving == [keep_img]


# ── Gallery archived flag ──────────────────────────────────────────────────


async def test_gallery_marks_archived_conversation_images(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _register(http_client, "img_test_a@example.com")
    user_id = await _me_id(http_client, token)
    conv_id = await _create_conversation(http_client, token)
    img_id = await _add_image(db_session, user_id, _uuid.UUID(conv_id))

    # Active conversation → not archived.
    gallery = await http_client.get(GALLERY_URL, headers=_auth(token))
    item = next(i for i in gallery.json()["items"] if i["id"] == str(img_id))
    assert item["archived"] is False

    # Archive the conversation → same image, now flagged archived but still listed.
    assert (await http_client.delete(
        f"{CONV_URL}/{conv_id}", headers=_auth(token)
    )).status_code == 204

    gallery = await http_client.get(GALLERY_URL, headers=_auth(token))
    assert gallery.json()["total"] == 1
    item = next(i for i in gallery.json()["items"] if i["id"] == str(img_id))
    assert item["archived"] is True
    assert item["conversation_id"] == conv_id


async def test_gallery_orphan_image_is_not_archived(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _register(http_client, "img_test_a@example.com")
    user_id = await _me_id(http_client, token)
    # Image with no conversation (conversation_id IS NULL) must report archived=False.
    img_id = await _add_image(db_session, user_id, None)

    gallery = await http_client.get(GALLERY_URL, headers=_auth(token))
    item = next(i for i in gallery.json()["items"] if i["id"] == str(img_id))
    assert item["archived"] is False
    assert item["conversation_id"] is None
