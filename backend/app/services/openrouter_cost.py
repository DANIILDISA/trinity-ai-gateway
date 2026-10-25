"""Query OpenRouter for the authoritative cost of a finished generation.

Used by the reconcile cron to fill in openrouter_cost_micro_usd for partial
(Stop-aborted) requests, whose cost never arrived in a streaming usage chunk
(tr-sgz). Reads only — never mutates billing.
"""
from __future__ import annotations

import logging

import httpx

from app.core.constants import usd_float_to_micro_usd

logger = logging.getLogger(__name__)

_GENERATION_URL = "https://openrouter.ai/api/v1/generation"
_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)


async def fetch_generation_cost(
    api_key: str,
    generation_id: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> int | None:
    """Return the generation's cost in micro-USD, or None if unavailable.

    None when OpenRouter has no finalized record yet (404), the request fails, or
    the response carries no `total_cost`. The caller leaves the row for a later run.
    `client` is injectable for tests; in production a short-lived client is created.
    """
    headers = {"Authorization": f"Bearer {api_key}"}
    params = {"id": generation_id}

    async def _do(c: httpx.AsyncClient) -> int | None:
        try:
            resp = await c.get(_GENERATION_URL, params=params, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("generation cost fetch failed for %s: %s", generation_id, exc)
            return None
        if resp.status_code != 200:
            logger.debug("generation %s not ready (HTTP %s)", generation_id, resp.status_code)
            return None
        data = (resp.json() or {}).get("data") or {}
        total_cost = data.get("total_cost")
        if total_cost is None:
            return None
        return usd_float_to_micro_usd(float(total_cost))

    if client is not None:
        return await _do(client)
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        return await _do(c)
