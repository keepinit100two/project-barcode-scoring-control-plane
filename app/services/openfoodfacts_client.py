from typing import Any, Dict, Optional

import httpx


class OpenFoodFactsError(RuntimeError):
    pass


def fetch_product_by_barcode(
    barcode: str,
    *,
    timeout_s: float = 10.0,
) -> Dict[str, Any]:
    """
    Fetch product data from Open Food Facts by barcode.

    Returns the parsed JSON response from OFF.
    Raises OpenFoodFactsError on network/HTTP errors.
    """
    url = f"https://world.openfoodfacts.org/api/v2/product/{barcode}.json"

    try:
        with httpx.Client(timeout=timeout_s) as client:
            r = client.get(url)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        raise OpenFoodFactsError(f"OFF lookup failed for barcode={barcode}: {e}") from e


def extract_ingredients_text(off_json: Dict[str, Any]) -> Optional[str]:
    """
    Extract the best available ingredients text from OFF payload.
    """
    product = off_json.get("product") or {}
    # OFF commonly uses 'ingredients_text' (sometimes language-specific)
    return product.get("ingredients_text") or product.get("ingredients_text_en")
