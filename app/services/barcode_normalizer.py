import re
from typing import List

from app.domain.schemas import NormalizedIngredient, ProductNormalizationResult
from app.services.openfoodfacts_client import fetch_product_by_barcode, extract_ingredients_text


# -----------------------------
# Deterministic alias table (v0)
# -----------------------------
# In a real client build, this becomes:
# - a database table (canonical ingredients)
# - a curated alias list
# - versioned policy updates
_ALIAS = {
    "sodium bicarbonate": ("ING_SODIUM_BICARB", "Sodium Bicarbonate"),
    "baking soda": ("ING_SODIUM_BICARB", "Sodium Bicarbonate"),
    "water": ("ING_WATER", "Water"),
    "fragrance": ("ING_FRAGRANCE", "Fragrance"),
    "ascorbic acid": ("ING_ASCORBIC_ACID", "Ascorbic Acid"),
}


# -----------------------------
# Parsing helpers (deterministic)
# -----------------------------
_PREFIX_RE = re.compile(r"^\s*ingredients\s*:\s*", re.IGNORECASE)
_PUNCT_RE = re.compile(r"[^a-z0-9\s\-\(\)]+")  # keep parens for parenthetical parsing
_PAREN_RE = re.compile(r"^(.*?)\((.*?)\)\s*$")


def parse_ingredients_raw(ingredients_raw: str) -> list[str]:
    """
    Deterministically parse an ingredient string into tokens.

    Rules (v0):
    - Strip leading 'Ingredients:' prefix (case-insensitive)
    - Split on comma or semicolon
    - Strip whitespace
    - Remove empty tokens
    - Keep parentheticals inside token for later matching
    """
    if not ingredients_raw:
        return []

    s = _PREFIX_RE.sub("", ingredients_raw.strip())
    s = s.replace(";", ",")
    parts = [p.strip() for p in s.split(",")]
    return [p for p in parts if p]


def norm_key(s: str) -> str:
    """
    Normalize a token to a stable lookup key for alias matching.
    """
    s = s.strip().lower()
    s = _PUNCT_RE.sub("", s)
    s = re.sub(r"\s+", " ", s)
    return s


def split_paren(token: str) -> tuple[str, str | None]:
    """
    If token is like: 'Vitamin C (Ascorbic Acid)'
    returns ('Vitamin C', 'Ascorbic Acid')
    Otherwise: (token, None)
    """
    m = _PAREN_RE.match(token.strip())
    if not m:
        return token, None
    return m.group(1).strip(), m.group(2).strip()


# -----------------------------
# Normalization (deterministic)
# -----------------------------
def normalize_ingredients(ingredients_raw: str) -> List[NormalizedIngredient]:
    tokens = parse_ingredients_raw(ingredients_raw)
    out: List[NormalizedIngredient] = []

    for raw in tokens:
        primary, inner = split_paren(raw)

        k_primary = norm_key(primary)
        k_inner = norm_key(inner) if inner else None

        # Try inner/parenthetical name first (often the chemical)
        if k_inner and k_inner in _ALIAS:
            ing_id, canonical = _ALIAS[k_inner]
            out.append(
                NormalizedIngredient(
                    ingredient_id=ing_id,
                    name=raw,
                    canonical_name=canonical,
                    provenance="alias_match",
                    notes=f"Matched via parenthetical alias: '{inner}' -> '{canonical}'",
                )
            )
            continue

        # Try primary name
        if k_primary in _ALIAS:
            ing_id, canonical = _ALIAS[k_primary]
            provenance = "alias_match" if k_primary != canonical.lower() else "db_match"
            out.append(
                NormalizedIngredient(
                    ingredient_id=ing_id,
                    name=raw,
                    canonical_name=canonical,
                    provenance=provenance,
                    notes=f"Matched via alias table: '{primary}' -> '{canonical}'",
                )
            )
            continue

        # Unknown ingredient
        out.append(
            NormalizedIngredient(
                ingredient_id=None,
                name=raw,
                canonical_name=None,
                provenance="unknown",
                notes="No match found in alias table",
            )
        )

    return out


def normalize_barcode(barcode: str) -> ProductNormalizationResult:
    """
    Stage A deterministic normalization:
    - Fetch OFF product data by barcode
    - Extract ingredients text
    - Normalize ingredients deterministically
    - Return canonical ProductNormalizationResult
    """
    off_json = fetch_product_by_barcode(barcode)
    ingredients_raw = extract_ingredients_text(off_json)

    product = (off_json.get("product") or {})
    product_name = product.get("product_name")
    brand = product.get("brands")

    if not ingredients_raw:
        ingredients_raw = ""

    ingredients = normalize_ingredients(ingredients_raw) if ingredients_raw else []

    return ProductNormalizationResult(
        barcode=barcode,
        product_name=product_name,
        brand=brand,
        ingredients_raw=ingredients_raw,
        ingredients=ingredients,
    )
