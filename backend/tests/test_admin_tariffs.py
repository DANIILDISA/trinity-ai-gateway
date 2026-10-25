"""Tests for the admin tariff management endpoints (Task 8.4)."""
from __future__ import annotations

import json
import uuid as _uuid
from datetime import datetime, timedelta, timezone

import pytest_asyncio
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy import delete as sa_delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import micro_to_api_string
from app.models.audit_log import AdminAuditLog
from app.models.model_pricing import ModelPricing
from app.models.notification import Notification
from app.models.system_setting import SystemSetting
from app.models.transaction import Transaction
from app.models.user import User
from app.services.tariff_service import (
    PRICING_CACHE_KEY,
    PRICING_CURRENT_CACHE_KEY,
    calculate_rub_price,
)

REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"
TARIFFS_URL = "/api/v1/admin/tariffs"
PREVIEW_URL = "/api/v1/admin/tariffs/preview"
APPLY_URL = "/api/v1/admin/tariffs/apply"
CBR_URL = "/api/v1/admin/tariffs/cbr-rate"


def _override_url(model_id: str) -> str:
    return f"/api/v1/admin/tariffs/models/{model_id}"


_TEST_EMAILS = [
    "t84_admin@example.com",
]

# A seeded model from 002_seed_model_pricing.py we'll use for assertions.
_SEEDED_MODEL = "anthropic/claude-sonnet-4-6"


@pytest_asyncio.fixture(autouse=True)
async def reset_tariff_state(
    db_session: AsyncSession, redis_client: Redis
) -> None:
    """Hard-reset model_pricing + tariff settings so each test starts clean.

    The seed migration leaves four current models with is_price_override=False
    and an exchange_rate_updated_at of 2026-04-29. Other tests may insert
    archived rows or flip overrides; the apply endpoint also bumps the
    "updated_at" timestamp, which would block the next test's apply via
    cooldown. We restore the baseline before every test.
    """
    # Clean leftover users from this file's runs.
    for email in _TEST_EMAILS:
        existing = (
            await db_session.execute(
                select(User).where(User.email_lower == email.lower())
            )
        ).scalar_one_or_none()
        if existing is not None:
            await db_session.execute(
                sa_delete(Notification).where(Notification.user_id == existing.id)
            )
            await db_session.execute(
                sa_delete(Transaction).where(
                    Transaction.admin_user_id == existing.id
                )
            )
            await db_session.execute(
                sa_delete(AdminAuditLog).where(
                    AdminAuditLog.actor_id == existing.id
                )
            )
            # Tariff applies set system_settings.updated_by — null it out so
            # we can drop the admin user without an FK violation.
            await db_session.execute(
                text(
                    "UPDATE system_settings SET updated_by = NULL "
                    "WHERE updated_by = :uid"
                ),
                {"uid": existing.id},
            )
            await db_session.delete(existing)

    # The apply-tariff endpoint fans tariff_change notifications out to EVERY
    # active user, leaving rows that reference users owned by other test files
    # and FK-blocking their cleanup. Purge globally before each run.
    await db_session.execute(
        sa_delete(Notification).where(Notification.type == "tariff_change")
    )

    # Wipe any non-current rows or rows we may have added.
    await db_session.execute(
        text("DELETE FROM model_pricing WHERE is_current = false")
    )
    # Reset all current rows: clear override flag, ensure available, clear effective_until.
    await db_session.execute(
        text(
            "UPDATE model_pricing "
            "SET is_price_override = false, status = 'available', effective_until = NULL "
            "WHERE is_current = true"
        )
    )
    # Reset tariff system_settings to a known baseline.
    await db_session.execute(
        text(
            "UPDATE system_settings SET value = :v WHERE key = 'exchange_rate_micro_rub'"
        ),
        {"v": json.dumps(82_500_000)},
    )
    await db_session.execute(
        text(
            "UPDATE system_settings SET value = :v WHERE key = 'exchange_rate_updated_at'"
        ),
        # Comfortably outside the 7-day cooldown vs. 2026-05-17.
        {"v": json.dumps("2026-04-01T00:00:00Z")},
    )
    await db_session.commit()

    # Clear pricing caches so the public /models endpoint sees fresh rows.
    await redis_client.delete(PRICING_CACHE_KEY)
    await redis_client.delete(PRICING_CURRENT_CACHE_KEY)
    # M4: also clear the pricing-SETTINGS cache so a stale rate/markup cached by a
    # prior test doesn't mask the DB values this fixture just reset (the reset
    # writes via raw SQL, which doesn't go through _set_setting's invalidation).
    # Clear via BOTH the fixture client and the code's own client (get_redis_client
    # — the http_client fixture inits it), since which one holds the stale key
    # depends on fixture-setup order.
    _settings_keys = ("settings:exchange_rate_micro_rub", "settings:markup_multiplier")
    await redis_client.delete(*_settings_keys)
    try:
        from app.core.redis import get_redis_client as _grc

        await _grc().delete(*_settings_keys)
    except Exception:
        pass


async def _register_admin(
    http: AsyncClient, db: AsyncSession, email: str
) -> tuple[str, str]:
    r = await http.post(
        REGISTER_URL,
        json={"email": email, "password": "SecurePass1", "display_name": "Имя"},
    )
    assert r.status_code == 200, r.text
    uid = r.json()["user"]["id"]

    user = (
        await db.execute(select(User).where(User.id == _uuid.UUID(uid)))
    ).scalar_one()
    user.role = "admin"
    await db.commit()
    login = await http.post(
        LOGIN_URL, json={"email": email, "password": "SecurePass1"}
    )
    assert login.status_code == 200
    return login.json()["access_token"], uid


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ── Tests ────────────────────────────────────────────────────────────────────


def test_calculate_rub_price() -> None:
    """BILLING_LOGIC.md §8.1 formula — pure-Python sanity check."""
    # 3 USD/M @ 82.5 RUB/USD × 1.25 markup
    # base = ceil(3_000_000 × 82_500_000 / 10^6) = 247_500_000
    # final = ceil(247_500_000 × 125 / 100) = 309_375_000
    assert calculate_rub_price(3_000_000, 82_500_000, 1.25) == 309_375_000


async def test_get_tariffs(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin_token, _ = await _register_admin(
        http_client, db_session, "t84_admin@example.com"
    )
    resp = await http_client.get(TARIFFS_URL, headers=_auth(admin_token))
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["exchange_rate_micro_rub"] == 82_500_000
    assert data["exchange_rate_display"] == "82.5000"
    assert abs(data["markup_multiplier"] - 1.25) < 1e-9
    assert data["last_updated"] is not None
    assert data["next_update_allowed_at"] is not None
    assert len(data["models"]) >= 1
    seed = next(m for m in data["models"] if m["model_id"] == _SEEDED_MODEL)
    assert seed["is_price_override"] is False
    assert seed["input_price_micro_rub"] > 0


async def test_preview_tariff(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin_token, _ = await _register_admin(
        http_client, db_session, "t84_admin@example.com"
    )
    new_rate = 85_000_000  # 85 RUB/USD

    resp = await http_client.post(
        PREVIEW_URL,
        headers=_auth(admin_token),
        json={"usd_rub_rate": new_rate},
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert len(rows) >= 1

    # Cross-check against calculate_rub_price for the seeded model.
    pricing_row = (
        await db_session.execute(
            select(ModelPricing).where(
                ModelPricing.model_id == _SEEDED_MODEL,
                ModelPricing.is_current.is_(True),
            )
        )
    ).scalar_one()
    expected_in = calculate_rub_price(
        pricing_row.openrouter_input_price_micro_usd, new_rate, 1.25
    )
    expected_out = calculate_rub_price(
        pricing_row.openrouter_output_price_micro_usd, new_rate, 1.25
    )
    preview = next(r for r in rows if r["model_id"] == _SEEDED_MODEL)
    # Compare as micro: parse "X.XXXX" → micro.
    def _to_micro(s: str) -> int:
        whole, frac = s.split(".")
        return int(whole) * 1_000_000 + int(frac.ljust(6, "0")[:6])
    assert _to_micro(preview["proposed_input_price"]) == expected_in
    assert _to_micro(preview["proposed_output_price"]) == expected_out


async def test_apply_tariff_updates_models(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin_token, _ = await _register_admin(
        http_client, db_session, "t84_admin@example.com"
    )
    new_rate = 90_000_000

    resp = await http_client.post(
        APPLY_URL,
        headers=_auth(admin_token),
        json={"usd_rub_rate": new_rate},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is True
    assert body["new_rate_micro_rub"] == new_rate
    assert body["models_updated"] >= 1

    # The new current row should reflect the new rate's recomputed price.
    pricing_row = (
        await db_session.execute(
            select(ModelPricing).where(
                ModelPricing.model_id == _SEEDED_MODEL,
                ModelPricing.is_current.is_(True),
            )
        )
    ).scalar_one()
    expected_in = calculate_rub_price(
        pricing_row.openrouter_input_price_micro_usd, new_rate, 1.25
    )
    assert pricing_row.input_price_micro_rub == expected_in

    # Old archive row should still exist with is_current=False.
    archived = (
        await db_session.execute(
            select(ModelPricing).where(
                ModelPricing.model_id == _SEEDED_MODEL,
                ModelPricing.is_current.is_(False),
            )
        )
    ).scalars().all()
    assert len(archived) >= 1
    for r in archived:
        assert r.effective_until is not None


async def test_apply_tariff_skips_overrides(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin_token, _ = await _register_admin(
        http_client, db_session, "t84_admin@example.com"
    )

    # Pin the seeded model first.
    pin = await http_client.patch(
        _override_url(_SEEDED_MODEL),
        headers=_auth(admin_token),
        json={
            "input_price_micro_rub": 999_000_000,
            "output_price_micro_rub": 1_999_000_000,
        },
    )
    assert pin.status_code == 200, pin.text

    # Apply a new rate — pinned model should NOT change.
    resp = await http_client.post(
        APPLY_URL,
        headers=_auth(admin_token),
        json={"usd_rub_rate": 95_000_000},
    )
    assert resp.status_code == 200, resp.text

    row = (
        await db_session.execute(
            select(ModelPricing).where(
                ModelPricing.model_id == _SEEDED_MODEL,
                ModelPricing.is_current.is_(True),
            )
        )
    ).scalar_one()
    assert row.is_price_override is True
    assert row.input_price_micro_rub == 999_000_000
    assert row.output_price_micro_rub == 1_999_000_000


async def test_apply_tariff_no_cooldown(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    # Manual rate updates are always allowed now — the cooldown restriction was
    # removed so an admin can correct the rate immediately (the automated CBR
    # updater self-throttles instead). Two consecutive applies both succeed.
    admin_token, _ = await _register_admin(
        http_client, db_session, "t84_admin@example.com"
    )

    first = await http_client.post(
        APPLY_URL,
        headers=_auth(admin_token),
        json={"usd_rub_rate": 85_000_000},
    )
    assert first.status_code == 200, first.text

    second = await http_client.post(
        APPLY_URL,
        headers=_auth(admin_token),
        json={"usd_rub_rate": 86_000_000},
    )
    assert second.status_code == 200, second.text
    assert second.json()["new_rate_micro_rub"] == 86_000_000


async def test_override_model_price(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin_token, _ = await _register_admin(
        http_client, db_session, "t84_admin@example.com"
    )

    resp = await http_client.patch(
        _override_url(_SEEDED_MODEL),
        headers=_auth(admin_token),
        json={
            "input_price_micro_rub": 500_000_000,
            "output_price_micro_rub": 1_500_000_000,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["is_price_override"] is True
    assert body["input_price_micro_rub"] == 500_000_000

    # New current row pinned, old archived.
    rows = (
        await db_session.execute(
            select(ModelPricing).where(
                ModelPricing.model_id == _SEEDED_MODEL,
            ).order_by(ModelPricing.effective_from.desc())
        )
    ).scalars().all()
    assert len(rows) >= 2
    current = next(r for r in rows if r.is_current)
    assert current.is_price_override is True
    assert current.input_price_micro_rub == 500_000_000


# ── Capability-preservation regression (tr-giy) ───────────────────────────────
#
# Every tariff/override flow ARCHIVES the current model_pricing row and INSERTS a
# fresh one. The re-insert must carry over the descriptive columns — capability
# flags + per-model stream timeout — because they're set from the OpenRouter sync,
# not recomputed here. Dropping them resets each to its server_default (flags →
# false, timeout → 30), which silently disabled web search (gated on
# supports_web_search) and extended thinking (gated on supports_reasoning) every
# time the automated exchange-rate updater fired (dev: once a minute).

_CAPABILITY_COLUMNS = (
    "supports_image_input",
    "supports_file_input",
    "supports_tools",
    "supports_reasoning",
    "supports_web_search",
)
_CUSTOM_STREAM_TIMEOUT = 300


async def _seed_capabilities(
    db: AsyncSession, model_id: str, *, is_override: bool = False
) -> None:
    """Force all capability flags True + a non-default stream timeout on the
    model's current row, so a re-insert that drops any of them is detectable."""
    sets = ", ".join(f"{c} = true" for c in _CAPABILITY_COLUMNS)
    await db.execute(
        text(
            f"UPDATE model_pricing SET {sets}, "
            "stream_timeout_seconds = :t, is_price_override = :ov "
            "WHERE model_id = :mid AND is_current = true"
        ),
        {"t": _CUSTOM_STREAM_TIMEOUT, "ov": is_override, "mid": model_id},
    )
    await db.commit()


async def _current_row(db: AsyncSession, model_id: str) -> ModelPricing:
    return (
        await db.execute(
            select(ModelPricing).where(
                ModelPricing.model_id == model_id,
                ModelPricing.is_current.is_(True),
            )
        )
    ).scalar_one()


def _assert_capabilities_preserved(row: ModelPricing) -> None:
    for col in _CAPABILITY_COLUMNS:
        assert getattr(row, col) is True, f"{col} dropped by tariff re-insert"
    assert row.stream_timeout_seconds == _CUSTOM_STREAM_TIMEOUT


async def test_apply_tariff_preserves_capabilities(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin_token, _ = await _register_admin(
        http_client, db_session, "t84_admin@example.com"
    )
    await _seed_capabilities(db_session, _SEEDED_MODEL)

    resp = await http_client.post(
        APPLY_URL, headers=_auth(admin_token), json={"usd_rub_rate": 90_000_000}
    )
    assert resp.status_code == 200, resp.text

    _assert_capabilities_preserved(await _current_row(db_session, _SEEDED_MODEL))


async def test_override_preserves_capabilities(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin_token, _ = await _register_admin(
        http_client, db_session, "t84_admin@example.com"
    )
    await _seed_capabilities(db_session, _SEEDED_MODEL)

    resp = await http_client.patch(
        _override_url(_SEEDED_MODEL),
        headers=_auth(admin_token),
        json={
            "input_price_micro_rub": 500_000_000,
            "output_price_micro_rub": 1_500_000_000,
        },
    )
    assert resp.status_code == 200, resp.text

    row = await _current_row(db_session, _SEEDED_MODEL)
    assert row.is_price_override is True
    _assert_capabilities_preserved(row)


async def test_clear_override_preserves_capabilities(
    http_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin_token, _ = await _register_admin(
        http_client, db_session, "t84_admin@example.com"
    )
    # Start from a pinned row that carries capabilities, then unpin it.
    await _seed_capabilities(db_session, _SEEDED_MODEL, is_override=True)

    resp = await http_client.delete(
        f"{_override_url(_SEEDED_MODEL)}/override",
        headers=_auth(admin_token),
    )
    assert resp.status_code == 200, resp.text

    row = await _current_row(db_session, _SEEDED_MODEL)
    assert row.is_price_override is False
    _assert_capabilities_preserved(row)


# ── M4: pricing-settings Redis cache + invalidation ──────────────────────────


async def test_pricing_settings_returns_cached_values(
    db_session: AsyncSession, redis_client: Redis, monkeypatch
) -> None:
    # M4: a warm cache is served WITHOUT a DB read. Seed sentinel values the DB
    # does not hold, so a cache MISS would return different numbers. Point the
    # service's get_redis_client at the test client so both share one connection.
    import app.services.tariff_service as ts

    monkeypatch.setattr(ts, "get_redis_client", lambda: redis_client)
    await redis_client.set(ts._RATE_CACHE_KEY, "123456789")
    await redis_client.set(ts._MARKUP_CACHE_KEY, "1.99")
    try:
        rate, markup = await ts._get_pricing_settings(db_session)
        assert rate == 123456789
        assert markup == 1.99
    finally:
        await redis_client.delete(ts._RATE_CACHE_KEY, ts._MARKUP_CACHE_KEY)


async def test_set_setting_busts_pricing_cache(
    db_session: AsyncSession, redis_client: Redis, monkeypatch
) -> None:
    # M4: writing exchange_rate/markup invalidates the cache key immediately;
    # writing an unrelated setting leaves it untouched.
    import app.services.tariff_service as ts

    monkeypatch.setattr(ts, "get_redis_client", lambda: redis_client)
    try:
        await redis_client.set(ts._RATE_CACHE_KEY, "999")
        await ts._set_setting(db_session, "exchange_rate_micro_rub", 100_000_000)
        assert await redis_client.get(ts._RATE_CACHE_KEY) is None

        await redis_client.set(ts._RATE_CACHE_KEY, "888")
        await ts._set_setting(db_session, "some_unrelated_setting", "x")
        assert await redis_client.get(ts._RATE_CACHE_KEY) == "888"
    finally:
        await redis_client.delete(ts._RATE_CACHE_KEY)


# ── tr-bztb: public pricing must display the BILLED price (display == charge) ──


def test_row_to_public_shows_billed_stored_price() -> None:
    """The public catalog must DISPLAY the stored RUB price billing actually charges,
    never the live formula — otherwise an override (or a markup drift) yields
    display != charge (ZZPP exposure / revenue leak)."""
    from app.services.tariff_service import _row_to_public

    row = ModelPricing(
        model_id="test/override-model",
        model_name="Override Model",
        provider="test",
        openrouter_input_price_micro_usd=1_000_000,
        openrouter_output_price_micro_usd=2_000_000,
        input_price_micro_rub=12_345_678,   # admin-pinned; the billed value
        output_price_micro_rub=23_456_789,
        is_price_override=True,
        max_context_tokens=8000,
        max_output_tokens=4000,
        stream_timeout_seconds=30,
        supports_image_input=False,
        supports_file_input=False,
        supports_tools=False,
        supports_reasoning=False,
        supports_web_search=False,
        status="available",
        is_current=True,
        effective_from=datetime(2026, 7, 2, tzinfo=timezone.utc),
    )
    public = _row_to_public(row)
    assert public["input_price_per_m"] == micro_to_api_string(12_345_678)
    assert public["output_price_per_m"] == micro_to_api_string(23_456_789)
    # Sanity: a formula-based display would have produced something different.
    assert calculate_rub_price(1_000_000, 95_000_000, 1.5) != 12_345_678


async def test_public_pricing_reflects_override_after_set(
    http_client: AsyncClient, db_session: AsyncSession, redis_client: Redis
) -> None:
    """End-to-end: after an admin pins an override, the Redis-cached public pricing
    surface must return the billed override price, not the formula."""
    from app.services.tariff_service import get_all_current_pricing, override_model_price

    _, admin_uid = await _register_admin(http_client, db_session, "t84_admin@example.com")

    await override_model_price(
        db_session, redis_client, _SEEDED_MODEL,
        input_price_micro_rub=7_777_777, output_price_micro_rub=8_888_888,
        admin_user_id=_uuid.UUID(admin_uid),
    )
    pricing = await get_all_current_pricing(db_session, redis_client)
    row = next(p for p in pricing if p["id"] == _SEEDED_MODEL)
    assert row["input_price_per_m"] == micro_to_api_string(7_777_777)
    assert row["output_price_per_m"] == micro_to_api_string(8_888_888)
