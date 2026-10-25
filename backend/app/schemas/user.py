from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class UserProfile(BaseModel):
    id: UUID
    email: str
    display_name: str
    full_name: str | None = None
    role: str
    status: str
    balance: str
    # tr-tl3q: held funds (active reservations) + spendable balance, so the UI can
    # explain why "available" dips below the headline balance during in-flight
    # requests instead of the gap only surfacing in an insufficient-funds error.
    reserved: str
    available: str
    has_password: bool = False
    scheduled_delete_at: datetime | None = None


class UpdateProfileRequest(BaseModel):
    # Both fields are optional so the same endpoint can update either
    # independently. display_name keeps its existing 1..100 length cap;
    # full_name is a free-form polite name, capped at 150.
    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    full_name: str | None = Field(default=None, max_length=150)


class SessionItem(BaseModel):
    id: UUID
    ip_address: str | None
    user_agent: str | None
    created_at: datetime
    expires_at: datetime
    is_current: bool


class SessionListResponse(BaseModel):
    items: list[SessionItem]


class DeleteAccountRequest(BaseModel):
    # Plain-text confirmation of the account email the user just typed. The
    # service compares it to the authenticated user's email; mismatch returns
    # 400 so a casual click can't trigger the soft-delete.
    email_confirmation: str


class ModelUsageBreakdown(BaseModel):
    model_id: str
    messages: int
    input_tokens: int
    output_tokens: int
    spent: str  # api_string format


class AnalyticsResponse(BaseModel):
    total_messages: int
    total_input_tokens: int
    total_output_tokens: int
    total_spent: str
    by_model: list[ModelUsageBreakdown]


class TransactionItem(BaseModel):
    id: UUID
    type: str
    amount: str
    description: str
    balance_after: str
    created_at: datetime


class TransactionHistoryResponse(BaseModel):
    items: list[TransactionItem]
    total: int
    page: int
    per_page: int
    pages: int


class NotificationItem(BaseModel):
    id: UUID
    type: str
    title: str
    message: str
    channel: str
    is_read: bool
    created_at: datetime


class NotificationListResponse(BaseModel):
    items: list[NotificationItem]
    total: int
    page: int
    per_page: int
    pages: int
