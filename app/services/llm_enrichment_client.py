import os
from typing import Any, Dict, List

import httpx


class LLMEnrichmentError(RuntimeError):
    pass


def call_mock_llm_enrichment(
    *,
    event_id: str,
    barcode: str,
    unknown_ingredients: List[str],
    timeout_s: float = 10.0,
) -> Dict[str, Any]:
    """
    Calls the local mock LLM service (9191 by default).
    """
    base_url = os.environ.get("MOCK_LLM_BASE_URL", "http://127.0.0.1:9191")
    url = f"{base_url.rstrip('/')}/enrich/ingredients"

    body = {
        "event_id": event_id,
        "barcode": barcode,
        "unknown_ingredients": unknown_ingredients,
    }

    try:
        with httpx.Client(timeout=timeout_s) as client:
            r = client.post(url, json=body)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        raise LLMEnrichmentError(f"Mock LLM enrichment failed: {e}") from e
