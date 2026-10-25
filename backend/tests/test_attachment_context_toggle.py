"""tr-kx4f — the per-attachment "в контексте" toggle is persisted and honored.

Two coupled guarantees:
  1. PERSISTENCE — the toggle is stored on chat_images/chat_files, exposed by the
     conversation GET, and mutated by a PATCH endpoint (ownership-checked), so the
     choice survives a reload instead of silently resetting to checked.
  2. COST — the context builder must NOT re-inject an attachment whose
     included_in_context is false (otherwise an "excluded" image is still sent to
     the model and re-billed for vision on every follow-up turn). The edit-resend
     clone must carry the flag too, or excluding then editing silently re-includes.
"""
import base64
import uuid as _uuid

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat_file import ChatFile
from app.models.chat_image import ChatImage
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.user import User
from app.services.chat_file_service import copy_files, files_by_message
from app.services.chat_image_service import (
    build_history_with_images,
    copy_images,
)

MODEL_ID = "openai/gpt-4.1-mini"
_PAYLOAD = base64.b64encode(b"trinity-test-attachment-bytes").decode()

REGISTER_URL = "/api/v1/auth/register"
CONV_URL = "/api/v1/chat/conversations"


# ── helpers ─────────────────────────────────────────────────────────────────


async def _seed_user_conv_msg(
    db: AsyncSession, email: str
) -> tuple[User, Conversation, Message]:
    user = User(
        email=email,
        display_name="Вложение",
        password_hash=None,
        role="user",
        status="active",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    conv = Conversation(user_id=user.id, model_id=MODEL_ID, title="Вложения")
    db.add(conv)
    await db.commit()
    await db.refresh(conv)

    msg = Message(
        conversation_id=conv.id, role="user", content="вопрос", is_active=True
    )
    db.add(msg)
    await db.commit()
    await db.refresh(msg)
    return user, conv, msg


def _make_image(
    user: User, conv: Conversation, msg: Message, *, included: bool
) -> ChatImage:
    return ChatImage(
        user_id=user.id,
        conversation_id=conv.id,
        message_id=msg.id,
        thumbnail_base64=_PAYLOAD,
        full_base64=_PAYLOAD,
        media_type="image/png",
        file_size_bytes=len(base64.b64decode(_PAYLOAD)),
        included_in_context=included,
    )


def _make_file(
    user: User, conv: Conversation, msg: Message, *, included: bool
) -> ChatFile:
    return ChatFile(
        user_id=user.id,
        conversation_id=conv.id,
        message_id=msg.id,
        filename="заметки.txt",
        media_type="text/plain",
        kind="text",
        base64_data=_PAYLOAD,
        file_size_bytes=len(base64.b64decode(_PAYLOAD)),
        token_estimate=3,
        included_in_context=included,
    )


def _register_payload(email: str) -> dict:
    return {"email": email, "password": "SecurePass1", "display_name": "Тест"}


async def _register(http_client: AsyncClient, email: str) -> str:
    resp = await http_client.post(REGISTER_URL, json=_register_payload(email))
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ── 1. context builder honors the persisted flag (the COST guarantee) ────────


async def test_build_history_excludes_image_with_flag_false(
    db_session: AsyncSession,
) -> None:
    user, conv, msg = await _seed_user_conv_msg(db_session, "kx4f_img_off@example.com")
    db_session.add(_make_image(user, conv, msg, included=False))
    await db_session.commit()

    history = await build_history_with_images(db_session, conv.id)

    # The excluded image must NOT turn the message into a multimodal array — it
    # stays plain text, so no image_url part is re-injected (no repeat vision cost).
    assert history == [{"role": "user", "content": "вопрос"}]


async def test_build_history_includes_image_with_flag_true(
    db_session: AsyncSession,
) -> None:
    user, conv, msg = await _seed_user_conv_msg(db_session, "kx4f_img_on@example.com")
    db_session.add(_make_image(user, conv, msg, included=True))
    await db_session.commit()

    history = await build_history_with_images(db_session, conv.id)

    assert len(history) == 1
    parts = history[0]["content"]
    assert isinstance(parts, list)
    assert any(p.get("type") == "image_url" for p in parts)


async def test_files_by_message_excludes_file_with_flag_false(
    db_session: AsyncSession,
) -> None:
    user, conv, msg = await _seed_user_conv_msg(db_session, "kx4f_file_off@example.com")
    db_session.add(_make_file(user, conv, msg, included=False))
    await db_session.commit()

    by_msg = await files_by_message(db_session, conv.id)

    assert msg.id not in by_msg


async def test_files_by_message_includes_file_with_flag_true(
    db_session: AsyncSession,
) -> None:
    user, conv, msg = await _seed_user_conv_msg(db_session, "kx4f_file_on@example.com")
    db_session.add(_make_file(user, conv, msg, included=True))
    await db_session.commit()

    by_msg = await files_by_message(db_session, conv.id)

    assert msg.id in by_msg
    assert by_msg[msg.id][0]["type"] == "file"


# ── 2. edit-resend clone carries the flag (no silent re-inclusion) ───────────


async def test_copy_images_preserves_flag(db_session: AsyncSession) -> None:
    user, conv, msg = await _seed_user_conv_msg(db_session, "kx4f_copy_img@example.com")
    db_session.add(_make_image(user, conv, msg, included=False))
    await db_session.commit()

    dst = Message(conversation_id=conv.id, role="user", content="правка", is_active=True)
    db_session.add(dst)
    await db_session.commit()
    await db_session.refresh(dst)

    await copy_images(db_session, msg.id, dst.id)
    await db_session.commit()

    cloned = (
        await db_session.execute(
            select(ChatImage).where(ChatImage.message_id == dst.id)
        )
    ).scalar_one()
    assert cloned.included_in_context is False


async def test_copy_files_preserves_flag(db_session: AsyncSession) -> None:
    user, conv, msg = await _seed_user_conv_msg(db_session, "kx4f_copy_file@example.com")
    db_session.add(_make_file(user, conv, msg, included=False))
    await db_session.commit()

    dst = Message(conversation_id=conv.id, role="user", content="правка", is_active=True)
    db_session.add(dst)
    await db_session.commit()
    await db_session.refresh(dst)

    await copy_files(db_session, msg.id, dst.id)
    await db_session.commit()

    cloned = (
        await db_session.execute(
            select(ChatFile).where(ChatFile.message_id == dst.id)
        )
    ).scalar_one()
    assert cloned.included_in_context is False


# ── 3. PATCH persists + GET serializes the flag (the PERSISTENCE guarantee) ──


async def test_patch_image_persists_flag_and_get_reflects_it(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _register(http_client, "kx4f_patch_img@example.com")
    user = (
        await db_session.execute(
            select(User).where(User.email_lower == "kx4f_patch_img@example.com")
        )
    ).scalar_one()
    conv = Conversation(user_id=user.id, model_id=MODEL_ID, title="Картинки")
    db_session.add(conv)
    await db_session.commit()
    await db_session.refresh(conv)
    img = ChatImage(
        user_id=user.id,
        conversation_id=conv.id,
        message_id=None,
        thumbnail_base64=_PAYLOAD,
        full_base64=_PAYLOAD,
        media_type="image/png",
        file_size_bytes=len(base64.b64decode(_PAYLOAD)),
    )
    db_session.add(img)
    await db_session.commit()
    await db_session.refresh(img)

    # Default is true on the GET.
    listed = await http_client.get(
        f"/api/v1/images/conversation/{conv.id}", headers=_auth(token)
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()[0]["included_in_context"] is True

    # Toggle off.
    patched = await http_client.patch(
        f"/api/v1/images/{img.id}",
        json={"included_in_context": False},
        headers=_auth(token),
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["included_in_context"] is False

    # Survives a re-fetch (persisted, not just client memory).
    relisted = await http_client.get(
        f"/api/v1/images/conversation/{conv.id}", headers=_auth(token)
    )
    assert relisted.json()[0]["included_in_context"] is False


async def test_patch_image_foreign_owner_returns_404(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    owner_token = await _register(http_client, "kx4f_img_owner@example.com")
    await _register(http_client, "kx4f_img_intruder@example.com")
    intruder_token = (
        await http_client.post(
            "/api/v1/auth/login",
            json={"email": "kx4f_img_intruder@example.com", "password": "SecurePass1"},
        )
    ).json()["access_token"]

    owner = (
        await db_session.execute(
            select(User).where(User.email_lower == "kx4f_img_owner@example.com")
        )
    ).scalar_one()
    img = ChatImage(
        user_id=owner.id,
        conversation_id=None,
        message_id=None,
        thumbnail_base64=_PAYLOAD,
        full_base64=_PAYLOAD,
        media_type="image/png",
        file_size_bytes=1,
    )
    db_session.add(img)
    await db_session.commit()
    await db_session.refresh(img)

    resp = await http_client.patch(
        f"/api/v1/images/{img.id}",
        json={"included_in_context": False},
        headers=_auth(intruder_token),
    )
    assert resp.status_code == 404, resp.text


async def test_patch_file_persists_flag_and_get_reflects_it(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _register(http_client, "kx4f_patch_file@example.com")
    user = (
        await db_session.execute(
            select(User).where(User.email_lower == "kx4f_patch_file@example.com")
        )
    ).scalar_one()
    conv = Conversation(user_id=user.id, model_id=MODEL_ID, title="Файлы")
    db_session.add(conv)
    await db_session.commit()
    await db_session.refresh(conv)
    f = ChatFile(
        user_id=user.id,
        conversation_id=conv.id,
        message_id=None,
        filename="data.txt",
        media_type="text/plain",
        kind="text",
        base64_data=_PAYLOAD,
        file_size_bytes=1,
        token_estimate=2,
    )
    db_session.add(f)
    await db_session.commit()
    await db_session.refresh(f)

    listed = await http_client.get(
        f"/api/v1/files/conversation/{conv.id}", headers=_auth(token)
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()[0]["included_in_context"] is True

    patched = await http_client.patch(
        f"/api/v1/files/{f.id}",
        json={"included_in_context": False},
        headers=_auth(token),
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["included_in_context"] is False

    relisted = await http_client.get(
        f"/api/v1/files/conversation/{conv.id}", headers=_auth(token)
    )
    assert relisted.json()[0]["included_in_context"] is False


async def test_patch_file_foreign_owner_returns_404(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _register(http_client, "kx4f_file_owner@example.com")
    await _register(http_client, "kx4f_file_intruder@example.com")
    intruder_token = (
        await http_client.post(
            "/api/v1/auth/login",
            json={"email": "kx4f_file_intruder@example.com", "password": "SecurePass1"},
        )
    ).json()["access_token"]

    owner = (
        await db_session.execute(
            select(User).where(User.email_lower == "kx4f_file_owner@example.com")
        )
    ).scalar_one()
    f = ChatFile(
        user_id=owner.id,
        conversation_id=None,
        message_id=None,
        filename="data.txt",
        media_type="text/plain",
        kind="text",
        base64_data=_PAYLOAD,
        file_size_bytes=1,
        token_estimate=2,
    )
    db_session.add(f)
    await db_session.commit()
    await db_session.refresh(f)

    resp = await http_client.patch(
        f"/api/v1/files/{f.id}",
        json={"included_in_context": False},
        headers=_auth(intruder_token),
    )
    assert resp.status_code == 404, resp.text
