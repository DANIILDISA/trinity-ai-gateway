"""Tests for the segment-title heuristic (tr-cy0.3.5).

Product decision (spike tr-cy0.3.1 + user): segment titles are a FREE,
deterministic heuristic — no LLM call, so the user balance is never charged.
The title is a Russian provider summary (when present) or the segment's cleaned
lead line, normalized to one short line.
"""
import openai
import pytest

from app.services.reasoning_summary_service import (
    _SUMMARY_MAX_LEN,
    normalize_summary,
    summarize_segment,
)


def test_normalize_strips_markdown_lead_and_bold():
    assert normalize_summary("## Анализ вопросов") == "Анализ вопросов"
    assert normalize_summary("**Разбираю три вопроса**") == "Разбираю три вопроса"
    assert normalize_summary("- первый шаг") == "Первый шаг"


def test_normalize_collapses_to_single_line_first_sentence():
    raw = "Разбираю три вопроса пользователя. Затем готовлю подробный ответ."
    assert normalize_summary(raw) == "Разбираю три вопроса пользователя"


def test_normalize_removes_trailing_punctuation():
    assert normalize_summary("Готовлю ответ:") == "Готовлю ответ"
    assert normalize_summary("Думаю…") == "Думаю"


def test_normalize_truncates_to_limit_with_ellipsis():
    raw = "слово " * 40  # far over the limit, many short words
    out = normalize_summary(raw)
    assert len(out) <= _SUMMARY_MAX_LEN
    assert out.endswith("…")


def test_normalize_keeps_underscores_in_identifiers():
    assert normalize_summary("Создаю файл Python_exam_prep") == (
        "Создаю файл Python_exam_prep"
    )


def test_normalize_empty_or_markup_only_returns_empty():
    assert normalize_summary("") == ""
    assert normalize_summary("###") == ""


def test_normalize_skips_task_restating_label_line():
    # tr-c14: live repro — the model opened with a bare "Пользователь просит:"
    # label and the title became that meaningless fragment. The label defers the
    # content to the next line, which is the real title.
    raw = "Пользователь просит:\nСтруктурированный разбор задачи про два поезда."
    assert normalize_summary(raw) == "Структурированный разбор задачи про два поезда"


def test_normalize_strips_leading_filler_interjection():
    # tr-c14: drop opening fillers ("Хорошо,", "Окей,", "Итак,") and keep the
    # substantive thought that follows.
    assert (
        normalize_summary("Хорошо, заменяю последнюю строку-заглушку.")
        == "Заменяю последнюю строку-заглушку"
    )
    assert (
        normalize_summary("Окей, итак, считаю расстояние между поездами.")
        == "Считаю расстояние между поездами"
    )


def test_normalize_substantive_first_line_unchanged():
    # Guard against over-stripping: a substantive opener (the reference style) is
    # returned verbatim.
    assert (
        normalize_summary("Структурированный разбор задачи")
        == "Структурированный разбор задачи"
    )


def test_normalize_label_only_falls_back_not_empty():
    # A label with no substantive follow-up still yields a (trimmed) title rather
    # than empty — mirrors the existing "Готовлю ответ:" contract.
    assert normalize_summary("Пользователь просит:") == "Пользователь просит"


def test_summarize_segment_skips_preamble_label():
    text = "Пользователь просит:\nЗаменяю строку-заглушку реальным выводом."
    assert summarize_segment(text) == "Заменяю строку-заглушку реальным выводом"


def test_summarize_uses_russian_provider_summary():
    title = summarize_segment(
        "english reasoning text here",
        provider_summary="Классифицирую вопросы по сложности",
    )
    assert title == "Классифицирую вопросы по сложности"


def test_summarize_ignores_english_provider_summary_uses_text_lead():
    # An English provider summary is not used (we can't guarantee RU without an
    # LLM); the title falls back to the segment's lead line instead.
    title = summarize_segment(
        "Структурирую разбор трёх вопросов",
        provider_summary="Orchestrated comprehensive analysis",
    )
    assert title == "Структурирую разбор трёх вопросов"


def test_summarize_falls_back_to_lead_line():
    text = "**Заменяю последнюю строку-заглушку**\nДалее идёт длинный разбор..."
    assert summarize_segment(text) == "Заменяю последнюю строку-заглушку"


def test_summarize_output_is_clean_one_line_no_markdown():
    out = summarize_segment("## **Готово:** `результат`\nещё текст")
    assert "\n" not in out
    assert "#" not in out and "*" not in out and "`" not in out
    assert not out.endswith((".", ":", "…", ","))
    assert len(out) <= _SUMMARY_MAX_LEN


def test_summarize_empty_text_returns_empty_without_raising():
    assert summarize_segment("") == ""
    assert summarize_segment("   \n  ") == ""


def test_summarize_makes_no_model_call_and_no_charge(monkeypatch):
    """Billing invariant (acceptance #5): the heuristic never instantiates an
    OpenRouter/OpenAI client, so no request is forwarded and nothing is billed to
    the user. The signature takes only text — there is no db/balance to touch."""

    def _boom(*args, **kwargs):
        raise AssertionError(
            "summarize_segment must not call any model (heuristic, free)"
        )

    monkeypatch.setattr(openai, "AsyncOpenAI", _boom)
    monkeypatch.setattr(openai, "OpenAI", _boom, raising=False)
    title = summarize_segment("Анализирую вопросы пользователя")
    assert title == "Анализирую вопросы пользователя"


def test_summarize_signature_has_no_db_or_client_dependency():
    import inspect

    params = set(inspect.signature(summarize_segment).parameters)
    # No db / balance / client parameter can leak a charge into this path.
    assert not (params & {"db", "session", "client", "user_id", "balance"})
