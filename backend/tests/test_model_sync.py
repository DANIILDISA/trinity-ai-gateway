"""ModelSyncService tests — httpx is mocked, never hits real OpenRouter.

Each test runs against a clean slate: the autouse `sync_env` fixture snapshots
and restores both `model_pricing` and `api_keys` so a sync (which deactivates
every current model absent from the response) can't corrupt seeded data.
"""
import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import delete as sa_delete
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.model_sync_service as sync_module
from app.core.security import encrypt_key
from app.models.api_key import ApiKey
from app.models.model_pricing import ModelPricing
from app.services.model_sync_service import (
    ModelSyncService,
    _extract_capabilities,
    _to_micro_usd_per_million,
)
from app.services.openrouter_proxy import get_active_key


# ── httpx mock ────────────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> "_FakeResponse":
        return self

    def json(self) -> dict:
        return self._payload


def _install_httpx_mock(monkeypatch, *, payload=None, error: Exception | None = None):
    """Patch httpx.AsyncClient so .get() returns `payload` or raises `error`."""

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self) -> "_FakeAsyncClient":
            return self

        async def __aexit__(self, *exc) -> bool:
            return False

        async def get(self, url, headers=None):
            if error is not None:
                raise error
            return _FakeResponse(payload)

    monkeypatch.setattr(sync_module.httpx, "AsyncClient", _FakeAsyncClient)


def _models_payload(*models: dict) -> dict:
    return {"data": list(models)}


def _model(model_id: str, *, prompt="0.000001", completion="0.000002", ctx=8192, name=None):
    return {
        "id": model_id,
        "name": name or model_id,
        "context_length": ctx,
        "pricing": {"prompt": prompt, "completion": completion},
    }


# ── Snapshot/restore isolation ───────────────────────────────────────────────


def _snapshot(rows, model):
    cols = [c.key for c in inspect(model).columns]
    return [{c: getattr(r, c) for c in cols} for r in rows]


@pytest_asyncio.fixture
async def sync_env(db_session: AsyncSession):
    """Clean slate: no models, exactly one primary+active key. Restored after."""
    mp_rows = (await db_session.execute(select(ModelPricing))).scalars().all()
    key_rows = (await db_session.execute(select(ApiKey))).scalars().all()
    mp_snapshot = _snapshot(mp_rows, ModelPricing)
    key_snapshot = _snapshot(key_rows, ApiKey)

    await db_session.execute(sa_delete(ModelPricing))
    await db_session.execute(sa_delete(ApiKey))
    db_session.add(ApiKey(
        label="mock",
        encrypted_key=encrypt_key("sk-or-mock-test12345"),
        key_suffix="test1234",
        status="active",
        is_primary=True,
    ))
    await db_session.commit()

    yield db_session

    await db_session.execute(sa_delete(ModelPricing))
    await db_session.execute(sa_delete(ApiKey))
    for d in mp_snapshot:
        db_session.add(ModelPricing(**d))
    for d in key_snapshot:
        db_session.add(ApiKey(**d))
    await db_session.commit()


async def _insert_model(db: AsyncSession, model_id: str, **overrides) -> None:
    defaults = dict(
        model_id=model_id,
        model_name=model_id,
        provider="test",
        openrouter_input_price_micro_usd=1_000_000,
        openrouter_output_price_micro_usd=2_000_000,
        input_price_micro_rub=1_000_000,
        output_price_micro_rub=2_000_000,
        is_price_override=False,
        max_context_tokens=8192,
        status="available",
        is_current=True,
    )
    defaults.update(overrides)
    db.add(ModelPricing(**defaults))
    await db.commit()


async def _current(db: AsyncSession, model_id: str) -> ModelPricing | None:
    db.expire_all()
    return (
        await db.execute(
            select(ModelPricing).where(
                ModelPricing.model_id == model_id,
                ModelPricing.is_current.is_(True),
            )
        )
    ).scalar_one_or_none()


# ── Tests ─────────────────────────────────────────────────────────────────────


async def test_sync_filters_free_models(
    sync_env: AsyncSession, redis_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_httpx_mock(
        monkeypatch,
        payload=_models_payload(
            _model("vendor/free-one:free"),
            _model("vendor/paid-one"),
        ),
    )

    result = await ModelSyncService().sync_models(sync_env, redis_client)

    assert result.added == 1
    assert await _current(sync_env, "vendor/paid-one") is not None
    assert await _current(sync_env, "vendor/free-one:free") is None


async def test_sync_upserts_new_model(
    sync_env: AsyncSession, redis_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_httpx_mock(
        monkeypatch,
        payload=_models_payload(_model("anthropic/new-model", ctx=200_000)),
    )

    result = await ModelSyncService().sync_models(sync_env, redis_client)

    assert result.added == 1
    row = await _current(sync_env, "anthropic/new-model")
    assert row is not None
    assert row.status == "available"
    assert row.is_current is True
    assert row.max_context_tokens == 200_000
    assert row.provider == "Anthropic"


async def test_sync_updates_existing_prices(
    sync_env: AsyncSession, redis_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _insert_model(sync_env, "openai/existing")
    _install_httpx_mock(
        monkeypatch,
        payload=_models_payload(
            _model("openai/existing", prompt="0.000005", completion="0.000010")
        ),
    )

    result = await ModelSyncService().sync_models(sync_env, redis_client)

    assert result.updated == 1
    assert result.added == 0
    row = await _current(sync_env, "openai/existing")
    assert row.openrouter_input_price_micro_usd == 5_000_000
    assert row.openrouter_output_price_micro_usd == 10_000_000
    # RUB recomputed (default rate 82.5, markup 1.25) — must differ from seed.
    assert row.input_price_micro_rub != 1_000_000


async def test_sync_deactivates_missing_models(
    sync_env: AsyncSession, redis_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _insert_model(sync_env, "vendor/gone")
    _install_httpx_mock(
        monkeypatch,
        payload=_models_payload(_model("vendor/still-here")),
    )

    result = await ModelSyncService().sync_models(sync_env, redis_client)

    assert result.deactivated == 1
    row = await _current(sync_env, "vendor/gone")
    assert row.status == "unavailable"


async def test_sync_handles_network_error(
    sync_env: AsyncSession, redis_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_httpx_mock(monkeypatch, error=httpx.ConnectError("boom"))

    result = await ModelSyncService().sync_models(sync_env, redis_client)

    assert result.errors  # non-empty
    assert result.added == 0 and result.updated == 0 and result.deactivated == 0


async def test_no_primary_key_raises_503(
    sync_env: AsyncSession,
) -> None:
    await sync_env.execute(sa_delete(ApiKey))
    await sync_env.commit()

    with pytest.raises(HTTPException) as exc_info:
        await get_active_key(sync_env)
    assert exc_info.value.status_code == 503


async def test_sync_sets_timeout_for_reasoning_models(
    sync_env: AsyncSession, redis_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_httpx_mock(
        monkeypatch,
        payload=_models_payload(
            _model("deepseek/deepseek-r1"),
            _model("vendor/extended-thinking"),
            _model("vendor/deepresearch-pro"),
        ),
    )

    await ModelSyncService().sync_models(sync_env, redis_client)

    for model_id in (
        "deepseek/deepseek-r1",
        "vendor/extended-thinking",
        "vendor/deepresearch-pro",
    ):
        row = await _current(sync_env, model_id)
        assert row is not None
        assert row.stream_timeout_seconds == 300


async def test_sync_sets_default_timeout_for_regular_models(
    sync_env: AsyncSession, redis_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_httpx_mock(
        monkeypatch,
        payload=_models_payload(_model("anthropic/claude-sonnet")),
    )

    await ModelSyncService().sync_models(sync_env, redis_client)

    row = await _current(sync_env, "anthropic/claude-sonnet")
    assert row is not None
    assert row.stream_timeout_seconds == 30


async def test_sync_skips_negative_price_models(
    sync_env: AsyncSession, redis_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_httpx_mock(
        monkeypatch,
        payload=_models_payload(
            _model("vendor/auto-router", prompt="-1"),
            _model("vendor/neg-router", prompt="-0.000001"),
            _model("vendor/null-router", prompt=None),
            _model("vendor/good"),
        ),
    )

    result = await ModelSyncService().sync_models(sync_env, redis_client)

    assert result.added == 1
    assert await _current(sync_env, "vendor/good") is not None
    assert await _current(sync_env, "vendor/auto-router") is None
    assert await _current(sync_env, "vendor/neg-router") is None
    assert await _current(sync_env, "vendor/null-router") is None


async def test_sync_skips_zero_price_models(
    sync_env: AsyncSession, redis_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_httpx_mock(
        monkeypatch,
        payload=_models_payload(
            _model("vendor/zero-both", prompt="0", completion="0"),
            _model("vendor/zero-completion", prompt="0.000001", completion="0"),
            _model("vendor/good"),
        ),
    )

    result = await ModelSyncService().sync_models(sync_env, redis_client)

    assert result.added == 1
    assert await _current(sync_env, "vendor/good") is not None
    assert await _current(sync_env, "vendor/zero-both") is None
    assert await _current(sync_env, "vendor/zero-completion") is None


async def test_sync_sets_capabilities_from_modalities(
    sync_env: AsyncSession, redis_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _model("vendor/multimodal")
    model["architecture"] = {"input_modalities": ["text", "image", "file"]}
    model["supported_parameters"] = ["tools", "reasoning"]
    model["pricing"]["web_search"] = "0.001"
    _install_httpx_mock(monkeypatch, payload=_models_payload(model))

    await ModelSyncService().sync_models(sync_env, redis_client)

    row = await _current(sync_env, "vendor/multimodal")
    assert row is not None
    assert row.supports_image_input is True
    assert row.supports_file_input is True
    assert row.supports_tools is True
    assert row.supports_reasoning is True
    assert row.supports_web_search is True


async def test_sync_no_capabilities_for_text_only_model(
    sync_env: AsyncSession, redis_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _model("vendor/text-only")
    model["architecture"] = {"input_modalities": ["text"]}
    model["supported_parameters"] = []
    model["pricing"]["web_search"] = "0"
    _install_httpx_mock(monkeypatch, payload=_models_payload(model))

    await ModelSyncService().sync_models(sync_env, redis_client)

    row = await _current(sync_env, "vendor/text-only")
    assert row is not None
    assert row.supports_image_input is False
    assert row.supports_file_input is False
    assert row.supports_tools is False
    assert row.supports_reasoning is False
    assert row.supports_web_search is False


# ── Tool-calling denylist (tr-1kb) ────────────────────────────────────────────


def test_extract_capabilities_denies_tools_for_ai21_jamba() -> None:
    # OpenRouter advertises `tools` for AI21 Jamba, but the model does NOT emit
    # tool_calls — it hallucinates "file created" in prose. Force the flag off so
    # the «Инструменты» toggle is hidden and no phantom file is promised (tr-1kb).
    raw = {
        "supported_parameters": ["tools", "temperature"],
        "architecture": {"input_modalities": ["text"]},
    }
    caps = _extract_capabilities(raw, "ai21/jamba-large-1.7")
    assert caps["supports_tools"] is False


def test_extract_capabilities_keeps_tools_for_supported_model() -> None:
    # Guard against over-broad denial: a genuinely tool-capable model is untouched.
    raw = {
        "supported_parameters": ["tools"],
        "architecture": {"input_modalities": ["text"]},
    }
    caps = _extract_capabilities(raw, "openai/gpt-4.1-mini")
    assert caps["supports_tools"] is True


async def test_sync_denies_tools_for_ai21_even_when_advertised(
    sync_env: AsyncSession, redis_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _model("ai21/jamba-large-1.7")
    model["architecture"] = {"input_modalities": ["text"]}
    model["supported_parameters"] = ["tools"]
    _install_httpx_mock(monkeypatch, payload=_models_payload(model))

    await ModelSyncService().sync_models(sync_env, redis_client)

    row = await _current(sync_env, "ai21/jamba-large-1.7")
    assert row is not None
    assert row.supports_tools is False


# ── Conversion unit check ─────────────────────────────────────────────────────


def test_usd_per_token_to_micro_usd_per_million() -> None:
    # $3 / 1M tokens == 0.000003 USD/token → 3_000_000 μUSD/M.
    assert _to_micro_usd_per_million("0.000003") == 3_000_000
    assert _to_micro_usd_per_million("0") == 0
