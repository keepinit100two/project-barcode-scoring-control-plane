from typing import List

from app.domain.schemas import NormalizedIngredient, ProductNormalizationResult
from app.services.openfoodfacts_client import fetch_product_by_barcode, extract_ingredients_text


# Deterministic alias table v0 (expand later; client may provide canonical DB)
_ALIAS = {
    "sodium bicarbonate": ("ING_SODIUM_BICARB", "Sodium Bicarbonate"),
    "baking soda": ("ING_SODIUM_BICARB", "Sodium Bicarbonate"),
    "water": ("ING_WATER", "Water"),
    "fragrance": ("ING_FRAGRANCE", "Fragrance"),
}


def normalize_ingredients(ingredients_raw: str) -> List[NormalizedIngredient]:
    parts = [p.strip().lower() for p in ingredients_raw.split(",") if p.strip()]
    out: List[NormalizedIngredient] = []

    for p in parts:
        if p in _ALIAS:
            ing_id, canonical = _ALIAS[p]
            provenance = "alias_match" if p != canonical.lower() else "db_match"
            out.append(
                NormalizedIngredient(
                    ingredient_id=ing_id,
                    name=p.title(),
                    canonical_name=canonical,
                    provenance=provenance,
                    notes=f"Matched via alias table: '{p}' -> '{canonical}'",
                )
            )
        else:
            out.append(
                NormalizedIngredient(
                    ingredient_id=None,
                    name=p.title(),
                    canonical_name=None,
                    provenance="unknown",
                    notes="No match found in alias table",
                )
            )
    return out


def normalize_barcode(barcode: str) -> ProductNormalizationResult:
    """
    Stage A deterministic normalization:
    - Fetch OFF product
    - Extract ingredients text
    - Normalize ingredients deterministically (alias/db match)
    """
    off_json = fetch_product_by_barcode(barcode)
    ingredients_raw = extract_ingredients_text(off_json)

    product = (off_json.get("product") or {})
    product_name = product.get("product_name")
    brand = product.get("brands")

    if not ingredients_raw:
        # Deterministic failure path; the router can later decide how to handle
        ingredients_raw = ""

    ingredients = normalize_ingredients(ingredients_raw) if ingredients_raw else []

    return ProductNormalizationResult(
        barcode=barcode,
        product_name=product_name,
        brand=brand,
        ingredients_raw=ingredients_raw,
        ingredients=ingredients,
    )
