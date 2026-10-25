"""B1 (tr-pkv3): the server must refuse to boot outside development when
SECRET_KEY is empty/weak — and must do so WITHOUT leaking any other config
secret into the raised exception (audit#1: a pydantic ValidationError embeds the
full input dict). Init kwargs override .env (InitSettingsSource has highest
priority), so these never touch the real env."""
import pytest
from pydantic import ValidationError

from app.config import Settings, _enforce_production_secret_key

_STRONG = "a3f5c9d2e1b8074f6a2c5e9d1b3f7a0c"  # 32 chars, diverse — a realistic key


def test_unknown_app_env_is_hard_construction_failure() -> None:
    # A typo like "Prod" must not silently route to the permissive branch.
    with pytest.raises(ValidationError):
        Settings(app_env="Prod", secret_key=_STRONG)


def test_production_empty_secret_key_refuses_boot() -> None:
    s = Settings(app_env="production", secret_key="")
    with pytest.raises(RuntimeError):
        _enforce_production_secret_key(s)


def test_production_short_secret_key_refuses_boot() -> None:
    s = Settings(app_env="production", secret_key="a1b2c3d4e5")  # <32
    with pytest.raises(RuntimeError):
        _enforce_production_secret_key(s)


def test_production_whitespace_secret_key_refuses_boot() -> None:
    s = Settings(app_env="production", secret_key=" " * 40)  # empty after strip
    with pytest.raises(RuntimeError):
        _enforce_production_secret_key(s)


def test_production_repeated_char_secret_key_refuses_boot() -> None:
    s = Settings(app_env="production", secret_key="x" * 40)  # len ok, no entropy
    with pytest.raises(RuntimeError):
        _enforce_production_secret_key(s)


def test_production_placeholder_secret_key_refuses_boot() -> None:
    # >=32 chars and diverse, but an obvious placeholder.
    s = Settings(app_env="production", secret_key="changeme-changeme-changeme-change")
    with pytest.raises(RuntimeError):
        _enforce_production_secret_key(s)


def test_error_message_leaks_no_other_secret() -> None:
    # The guard must not embed DATABASE_URL / other secrets in its message.
    s = Settings(
        app_env="production",
        secret_key="",
        database_url="postgresql+asyncpg://u:SUPERSECRETPW@h:5432/db",
    )
    with pytest.raises(RuntimeError) as ei:
        _enforce_production_secret_key(s)
    assert "SUPERSECRETPW" not in str(ei.value)
    assert s.database_url not in str(ei.value)


def test_production_strong_secret_key_boots() -> None:
    s = Settings(app_env="production", secret_key=_STRONG)
    _enforce_production_secret_key(s)  # must not raise


def test_development_empty_secret_key_allowed() -> None:
    s = Settings(app_env="development", secret_key="")
    _enforce_production_secret_key(s)  # dev is exempt — must not raise
