"""Central Bank of Russia (ЦБ РФ) daily FX rate fetch.

Single public helper: fetch_cbr_usd_rate() → Decimal RUB-per-USD, or None on
any failure (network, parse, missing USD). Never raises — the caller (the
automated rate updater) must keep running on a flaky upstream.

CBR daily XML: https://www.cbr.ru/scripts/XML_daily.asp
  <Valute ID="R01235">
    <NumCode>840</NumCode><CharCode>USD</CharCode>
    <Nominal>1</Nominal><Value>70,9509</Value><VunitRate>70,9509</VunitRate>
  </Valute>
Values use a comma decimal separator; the document is windows-1251 encoded
(ElementTree honors the XML declaration when given raw bytes). The effective
rate is Value / Nominal (Nominal is 1 for USD but we divide defensively).
"""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree as ET

import httpx

logger = logging.getLogger(__name__)

CBR_XML_URL = "https://www.cbr.ru/scripts/XML_daily.asp"
CBR_TIMEOUT_SECONDS = 15.0


async def fetch_cbr_usd_rate() -> Decimal | None:
    """Return the current CBR USD/RUB rate, or None on any error."""
    try:
        async with httpx.AsyncClient(timeout=CBR_TIMEOUT_SECONDS) as client:
            resp = await client.get(CBR_XML_URL)
            resp.raise_for_status()
        # Pass bytes so ElementTree reads the windows-1251 declaration itself.
        root = ET.fromstring(resp.content)
    except Exception as exc:  # noqa: BLE001 — fetch/parse must never propagate
        logger.error("CBR rate fetch/parse failed: %s", exc)
        return None

    for valute in root.findall("Valute"):
        if (valute.findtext("CharCode") or "").strip() == "USD":
            raw_value = (valute.findtext("Value") or "").strip().replace(",", ".")
            raw_nominal = (
                (valute.findtext("Nominal") or "1").strip().replace(",", ".")
            )
            try:
                value = Decimal(raw_value)
                nominal = Decimal(raw_nominal or "1")
            except (InvalidOperation, ValueError) as exc:
                logger.error("CBR USD value unparseable (%r): %s", raw_value, exc)
                return None
            if nominal == 0:
                logger.error("CBR USD nominal is zero")
                return None
            return value / nominal

    logger.warning("CBR XML contained no USD entry")
    return None
