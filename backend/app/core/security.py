import base64
import hashlib
import logging
import secrets
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

import anyio
import bcrypt
import jwt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError

from app.config import settings

logger = logging.getLogger(__name__)

# tr-34t3 FU-3: in-process observability for the blocklist fail-open (below). Redis is
# the thing that's down during a skip, so the counter CANNOT be Redis-backed. Per-worker
# rolling window; an elevated skip rate escalates WARNING → a rate-limited ERROR that log
# monitoring can alert on. Per-worker is correct: each worker independently signals a broad
# outage within seconds at load. The blind spot is LOW traffic (a degraded worker may see
# <threshold authed requests in the window), not high load. `maxlen` caps memory under a
# sustained high-RPS outage; it's >> the threshold so auto-eviction never hides an escalation.
_SKIP_WINDOW_SECONDS = 60
_SKIP_ALERT_THRESHOLD = 5
_blocklist_skips: deque[float] = deque(maxlen=2000)
_last_skip_escalation: float = 0.0


def _record_blocklist_skip(jti: str) -> None:
    global _last_skip_escalation
    now = time.monotonic()
    _blocklist_skips.append(now)
    while _blocklist_skips and _blocklist_skips[0] < now - _SKIP_WINDOW_SECONDS:
        _blocklist_skips.popleft()
    logger.warning("blocklist check skipped — Redis unavailable (fail-open) jti=%s", jti)
    if len(_blocklist_skips) > _SKIP_ALERT_THRESHOLD and now - _last_skip_escalation >= _SKIP_WINDOW_SECONDS:
        _last_skip_escalation = now
        logger.error(
            "ELEVATED blocklist-skip rate: %d in %ds — token revocation is degraded "
            "(Redis outage); investigate.",
            len(_blocklist_skips), _SKIP_WINDOW_SECONDS,
        )


async def blacklist_access_token(redis: Redis, jti: str, exp: int) -> None:
    """Best-effort: blacklist an access token's jti until its natural expiry (tr-34t3 FU-2).

    Shared by every logout/delete path (auth_service.logout_user, users.terminate_all_sessions,
    users.delete_account), each of which has ALREADY committed its primary action (refresh tokens
    deleted / account scheduled for deletion) before calling this. The blocklist write is therefore
    best-effort: NO Redis failure may 500 the request (that would mislead the client into retrying a
    completed sign-out — e.g. a duplicate audit row). But it is never SILENT either — a
    Connection/Timeout blip logs WARNING; any other RedisError (ResponseError from OOM/eviction/ACL —
    a persistent fault) logs ERROR so monitoring surfaces the degraded revocation. Either way the
    token still expires in jwt_access_token_expire_minutes, bounding the exposure (tr-34t3 FU-2,
    audit#2: write-path best-effort ≠ the read-path SB-1 rule that a blocklist CHECK must not
    fail-open on ResponseError — see decode_access_token).
    """
    ttl = exp - int(time.time())
    if ttl <= 0:
        return
    try:
        await redis.setex(f"blacklist:{jti}", ttl, "1")
    except (RedisConnectionError, RedisTimeoutError) as exc:
        logger.warning(
            "blocklist write skipped for jti=%s — Redis unavailable (token expires in ~%dm): %s",
            jti, settings.jwt_access_token_expire_minutes, exc,
        )
    except RedisError as exc:
        logger.error(
            "blocklist write FAILED for jti=%s — Redis ResponseError; revocation degraded "
            "(token expires in ~%dm): %s",
            jti, settings.jwt_access_token_expire_minutes, exc,
        )


_AES_NONCE_BYTES = 12  # GCM standard


def _key_bytes() -> bytes:
    return hashlib.sha256(settings.secret_key.encode()).digest()


def encrypt_key(plaintext: str) -> str:
    """AES-256-GCM-encrypt and return base64(nonce || ciphertext_with_tag)."""
    aes = AESGCM(_key_bytes())
    nonce = secrets.token_bytes(_AES_NONCE_BYTES)
    ct = aes.encrypt(nonce, plaintext.encode("utf-8"), associated_data=None)
    return base64.b64encode(nonce + ct).decode("ascii")


def decrypt_key(encrypted: str) -> str:
    """Reverse of encrypt_key. Raises ValueError on tamper / wrong key."""
    try:
        blob = base64.b64decode(encrypted.encode("ascii"))
        nonce, ct = blob[:_AES_NONCE_BYTES], blob[_AES_NONCE_BYTES:]
        aes = AESGCM(_key_bytes())
        return aes.decrypt(nonce, ct, associated_data=None).decode("utf-8")
    except Exception as exc:
        raise ValueError("Failed to decrypt key (tampered or wrong secret)") from exc


def _hash_password_sync(password: str) -> str:
    salt = bcrypt.gensalt(rounds=settings.bcrypt_rounds)
    return bcrypt.hashpw(password.encode(), salt).decode()


def _verify_password_sync(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


async def hash_password(password: str) -> str:
    """bcrypt hash, offloaded to a worker thread.

    bcrypt is CPU-bound (~250-400ms at rounds=12); running it on the event loop
    freezes every other request for that duration. anyio.to_thread.run_sync moves
    it off the loop so concurrent requests keep flowing (load-test bottleneck B1).
    """
    return await anyio.to_thread.run_sync(_hash_password_sync, password)


async def verify_password(plain: str, hashed: str) -> bool:
    """bcrypt verify, offloaded to a worker thread (see hash_password)."""
    return await anyio.to_thread.run_sync(_verify_password_sync, plain, hashed)


def create_access_token(subject: str) -> str:
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=settings.jwt_access_token_expire_minutes)
    payload: dict[str, Any] = {
        "sub": subject,
        "iat": now,
        "exp": expire,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


async def decode_access_token(token: str, redis: Redis | None = None) -> dict[str, Any]:
    payload = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
    if redis is not None:
        jti = payload.get("jti")
        if jti:
            try:
                revoked = await redis.exists(f"blacklist:{jti}")
            except (RedisConnectionError, RedisTimeoutError):
                # tr-8dy3: Redis unavailable (pool exhausted / timeout / outage) → fail-OPEN.
                # A Redis blip must not 500 every authed request; treat the token as
                # not-revoked. Bounded exposure (short access-token TTL); logged for
                # visibility. Scoped to connection/timeout ONLY (audit SB-1) — a ResponseError
                # (ACL misconfig / OOM / data bug) must NOT silently disable revocation; it
                # propagates → 500 → alert → fix, rather than fail-open forever.
                _record_blocklist_skip(jti)
                revoked = False
            if revoked:
                raise jwt.InvalidTokenError("Token has been revoked")
    return payload


def create_refresh_token() -> str:
    """Generate a cryptographically random 32-byte hex token (64 chars)."""
    return secrets.token_hex(32)
