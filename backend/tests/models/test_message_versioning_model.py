"""Model-level guards for the message-versioning columns + self relationship.

Pure ORM-metadata assertions (no DB session): importing the models barrel must
succeed (mapper configuration is valid) and the new columns/relationship must be
mapped exactly as migration 014 declares them.
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import configure_mappers

from app.models import Conversation, Message


def test_message_has_branching_columns() -> None:
    cols = Message.__table__.columns
    assert "parent_id" in cols
    assert "is_active" in cols
    assert "deleted_at" in cols

    assert cols["parent_id"].nullable is True
    # self-FK to messages.id
    fks = list(cols["parent_id"].foreign_keys)
    assert len(fks) == 1
    assert fks[0].column is Message.__table__.c.id

    assert cols["is_active"].nullable is False
    assert isinstance(cols["is_active"].type, sa.Boolean)
    assert cols["deleted_at"].nullable is True


def test_conversation_has_active_leaf_id() -> None:
    cols = Conversation.__table__.columns
    assert "active_leaf_id" in cols
    assert cols["active_leaf_id"].nullable is True
    fks = list(cols["active_leaf_id"].foreign_keys)
    assert len(fks) == 1
    assert fks[0].column is Message.__table__.c.id


def test_message_parent_self_relationship() -> None:
    # Forces full mapper configuration; raises if remote_side is wrong.
    configure_mappers()
    rels = Message.__mapper__.relationships
    assert "parent" in rels
    parent_rel = rels["parent"]
    # self-referential: both sides are Message
    assert parent_rel.mapper.class_ is Message
    # remote side must be Message.id (the "one" end of the parent->children link)
    remote_cols = {c.name for c in parent_rel.remote_side}
    assert "id" in remote_cols
