import re
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, field_validator
from pydantic_core import PydanticCustomError


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    display_name: str = Field(max_length=100)

    @field_validator("password")
    @classmethod
    def password_must_contain_digit_and_letter(cls, v: str) -> str:
        if not re.search(r"\d", v):
            raise PydanticCustomError(
                "password_no_digit",
                "Пароль должен содержать хотя бы одну цифру",
            )
        if not re.search(r"[a-zA-Zа-яёА-ЯЁ]", v):
            raise PydanticCustomError(
                "password_no_letter",
                "Пароль должен содержать хотя бы одну букву",
            )
        return v


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


class UserInAuth(BaseModel):
    id: UUID
    email: str
    display_name: str
    full_name: str | None = None
    role: str
    status: str
    balance: str
    # tr-tl3q: held funds + spendable balance (mirrors UserProfile) so the login
    # payload already carries the held-funds breakdown.
    reserved: str
    available: str
    has_password: bool = False


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    refresh_token: str | None = None
    user: UserInAuth


class PasswordResetRequestBody(BaseModel):
    email: EmailStr


class PasswordResetConfirmBody(BaseModel):
    token: str
    new_password: str = Field(min_length=8)


class AccountRecoveryRequest(BaseModel):
    email: EmailStr
    password: str
