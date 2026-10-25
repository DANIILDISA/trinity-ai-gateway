"""Prepare attached-file content parts for the OpenRouter chat payload.

Trinity accepts two families of attached files (see frontend lib/fileTypes.ts):

  • Text / code (md, txt, json, csv, source code, …) — sent by the frontend as a
    `file` part whose `file_data` is the UTF-8 text encoded as base64. The model
    has no "file" concept for these, so we DECODE them here and inline the text
    as an ordinary `text` part. This works on every model and never needs the
    file-parser plugin.

  • PDF / DOCX — binary documents OpenRouter can parse natively. We keep these as
    `file` parts and signal that the `file-parser` plugin must be enabled
    (BILLING_LOGIC unaffected: the per-page OCR cost, when any, comes back in the
    usage chunk like web-search).

Files are NOT persisted (storage is a later phase); this transform only shapes
the outgoing request. A plain-string message passes through untouched.
"""
from __future__ import annotations

import base64
import binascii
import logging

logger = logging.getLogger(__name__)

# MIME types we treat as plain text — decoded and inlined rather than forwarded
# as a file. Mirrors the `kind: "text"` entries in frontend lib/fileTypes.ts.
_TEXT_MIME_TYPES: frozenset[str] = frozenset(
    {
        "text/plain",
        "text/markdown",
        "text/csv",
        "text/html",
        "text/css",
        "application/json",
        "application/xml",
        "text/xml",
        "application/x-yaml",
        "text/yaml",
        "application/toml",
        "text/x-python",
        "application/javascript",
        "text/javascript",
        "application/typescript",
        "text/x-typescript",
        "text/x-go",
        "text/x-java",
        "text/x-rust",
        "application/x-sh",
        "application/sql",
    }
)

# Inlined file text is bounded so a pathological upload can't blow up the
# context (the schema already caps file_data length; this caps the decoded text).
_MAX_INLINE_TEXT_CHARS = 200_000


def _media_type_of(file_data: str) -> str:
    """Best-effort media type from a `data:{mime};base64,...` URI ("" if absent)."""
    if not file_data.startswith("data:"):
        return ""
    header = file_data[len("data:") :].split(",", 1)[0]
    return header.split(";", 1)[0].strip().lower()


def _decode_data_uri_text(file_data: str) -> str | None:
    """Decode the base64 payload of a text data URI to a UTF-8 string.

    Returns None when the URI is malformed or the payload isn't valid base64 —
    the caller then leaves the part as-is so the request still goes through.
    """
    _, _, payload = file_data.partition(",")
    if not payload:
        return None
    try:
        raw = base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError):
        return None
    text = raw.decode("utf-8", errors="replace")
    if len(text) > _MAX_INLINE_TEXT_CHARS:
        text = text[:_MAX_INLINE_TEXT_CHARS] + "\n…[файл обрезан]"
    return text


def prepare_model_content(
    content: "str | list[dict]",
) -> "tuple[str | list[dict], bool]":
    """Return (model_content, needs_file_parser) for an outgoing message.

    Text/code file parts are decoded and inlined as text parts; PDF/DOCX file
    parts are kept verbatim and flip `needs_file_parser` to True. Image and text
    parts pass through unchanged. A plain string is returned as-is.
    """
    if not isinstance(content, list):
        return content, False

    out: list[dict] = []
    needs_file_parser = False
    for part in content:
        if not isinstance(part, dict) or part.get("type") != "file":
            out.append(part)
            continue

        file_obj = part.get("file") or {}
        file_data = file_obj.get("file_data") or ""
        filename = file_obj.get("filename") or "file"
        media_type = _media_type_of(file_data)

        if media_type in _TEXT_MIME_TYPES:
            text = _decode_data_uri_text(file_data)
            if text is not None:
                out.append(
                    {
                        "type": "text",
                        "text": f"Содержимое файла «{filename}»:\n```\n{text}\n```",
                    }
                )
                continue
            # Undecodable text file — fall through and forward as a file part so
            # the request isn't silently dropped.
            logger.warning("Could not decode text file %s; forwarding as-is", filename)

        # PDF / DOCX / anything non-text → native file part, enable the parser.
        out.append(part)
        needs_file_parser = True

    return out, needs_file_parser


def prepare_context_content(
    messages: "list[dict]",
) -> "tuple[list[dict], bool]":
    """Apply prepare_model_content to every message in an assembled context.

    History messages may now carry re-injected `file` parts (chat_file_service),
    so the text-inlining + file-parser detection that prepare_model_content does
    for the current message must run uniformly across the whole context. Returns
    (messages, needs_file_parser). Messages whose content is a plain string (or
    has no file parts) pass through unchanged, so the text/image-only path stays
    byte-identical.
    """
    needs_file_parser = False
    out: list[dict] = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            prepared, needs = prepare_model_content(content)
            needs_file_parser = needs_file_parser or needs
            out.append({**msg, "content": prepared})
        else:
            out.append(msg)
    return out, needs_file_parser
