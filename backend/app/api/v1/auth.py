from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.backpressure import backpressure
from app.core.security import decode_access_token
from app.dependencies import get_current_user, get_db, get_redis, require_active, require_admin, require_auth
from app.models.user import User
from app.schemas.auth import (
    AccountRecoveryRequest,
    AuthResponse,
    LoginRequest,
    PasswordResetConfirmBody,
    PasswordResetRequestBody,
    RegisterRequest,
)
from app.services.auth_service import (
    confirm_password_reset,
    login_user,
    logout_user,
    recover_account,
    refresh_access_token,
    register_user,
    request_password_reset,
)
from app.services.email_service import EmailServiceProtocol, get_email_service

router = APIRouter(
    dependencies=[Depends(backpressure("auth", settings.auth_backpressure_limit))]
)


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _set_refresh_cookie(response: Response, token: str, expires_at: datetime) -> None:
    """Persistent cookie whose lifetime mirrors the server-side expiry exactly."""
    max_age = max(0, int((expires_at - datetime.now(timezone.utc)).total_seconds()))
    response.set_cookie(
        key="refresh_token",
        value=token,
        max_age=max_age,
        httponly=True,
        secure=settings.app_env == "production",
        samesite="strict",
    )


@router.post("/register", response_model=AuthResponse)
async def register(
    body: RegisterRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> AuthResponse:
    auth_response, raw_token, expires_at = await register_user(
        db, body, ip_address=_client_ip(request)
    )
    _set_refresh_cookie(response, raw_token, expires_at)
    return auth_response


@router.post("/login", response_model=AuthResponse)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    email_service: EmailServiceProtocol = Depends(get_email_service),
) -> AuthResponse:
    auth_response, raw_token, expires_at = await login_user(
        db, redis, body, email_service, ip_address=_client_ip(request)
    )
    _set_refresh_cookie(response, raw_token, expires_at)
    return auth_response


@router.post("/refresh", response_model=AuthResponse)
async def refresh(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> AuthResponse:
    refresh_token_str = request.cookies.get("refresh_token")
    if not refresh_token_str:
        raise HTTPException(status_code=401, detail="Токен обновления не найден")

    auth_response, new_raw_token, expires_at = await refresh_access_token(
        db, refresh_token_str, ip_address=_client_ip(request)
    )
    _set_refresh_cookie(response, new_raw_token, expires_at)
    return auth_response


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    auth_header = request.headers.get("Authorization", "")
    _, _, token = auth_header.partition(" ")
    payload = await decode_access_token(token)
    access_token_jti: str = payload["jti"]
    access_token_exp: int = payload["exp"]

    refresh_token_str = request.cookies.get("refresh_token")
    await logout_user(
        db,
        redis,
        access_token_jti,
        access_token_exp,
        refresh_token_str,
        user=current_user,
        ip_address=_client_ip(request),
    )
    response.delete_cookie("refresh_token")
    return {"message": "Выход выполнен успешно"}


@router.post("/reset-password/request")
async def reset_password_request(
    body: PasswordResetRequestBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
    email_service: EmailServiceProtocol = Depends(get_email_service),
) -> dict[str, str]:
    await request_password_reset(db, body.email, email_service, ip_address=_client_ip(request))
    return {"message": "Если email зарегистрирован, вам отправлено письмо"}


@router.post("/reset-password/confirm")
async def reset_password_confirm(
    body: PasswordResetConfirmBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    await confirm_password_reset(db, body.token, body.new_password, ip_address=_client_ip(request))
    return {"message": "Пароль успешно изменён"}


@router.post("/recover", response_model=AuthResponse)
async def recover(
    body: AccountRecoveryRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> AuthResponse:
    auth_response, raw_token, expires_at = await recover_account(
        db, body.email, body.password, ip_address=_client_ip(request)
    )
    _set_refresh_cookie(response, raw_token, expires_at)
    return auth_response


# TEST ENDPOINTS

@router.get("/test/auth-only")
async def test_auth_only(user: User = Depends(require_auth)) -> dict:
    return {"ok": True, "user_id": str(user.id)}


@router.get("/test/active-only")
async def test_active_only(user: User = Depends(require_active)) -> dict:
    return {"ok": True, "user_id": str(user.id)}


@router.get("/test/admin-only")
async def test_admin_only(user: User = Depends(require_admin)) -> dict:
    return {"ok": True, "user_id": str(user.id)}
