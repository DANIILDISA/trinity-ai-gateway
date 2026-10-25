"""SQLAlchemy ORM models.

Importing this package imports every model so that Base.metadata is fully
populated. Alembic env.py relies on this for autogenerate to work.
"""
from app.core.database import Base
from app.models.alert import Alert
from app.models.api_key import ApiKey
from app.models.artifact import Artifact
from app.models.audit_log import AdminAuditLog
from app.models.chat_file import ChatFile
from app.models.chat_image import ChatImage
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.model_pricing import ModelPricing
from app.models.notification import Notification
from app.models.password_reset_token import PasswordResetToken
from app.models.refresh_token import RefreshToken
from app.models.reservation import Reservation
from app.models.system_setting import SystemSetting
from app.models.transaction import Transaction
from app.models.usage_log import UsageLog
from app.models.user import User

metadata = Base.metadata

__all__ = [
    "Base",
    "metadata",
    "AdminAuditLog",
    "Alert",
    "ApiKey",
    "Artifact",
    "ChatFile",
    "ChatImage",
    "Conversation",
    "Message",
    "ModelPricing",
    "Notification",
    "PasswordResetToken",
    "RefreshToken",
    "Reservation",
    "SystemSetting",
    "Transaction",
    "UsageLog",
    "User",
]
