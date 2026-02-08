from app.services.barcode_normalizer import parse_ingredients_raw, normalize_ingredients

def test_parse_ingredients_strips_prefix_and_splits():
    raw = "Ingredients: Water; Sugar, Natural Flavors"
    tokens = parse_ingredients_raw(raw)
    assert tokens == ["Water", "Sugar", "Natural Flavors"]

def test_normalize_handles_parenthetical_alias():
    # Example: "Vitamin C (Ascorbic Acid)" should match Ascorbic Acid if in alias map
    raw = "Vitamin C (Ascorbic Acid), Water"
    out = normalize_ingredients(raw)
    assert len(out) == 2
    assert out[0].provenance in ("alias_match", "db_match")
    # We won't assume exact IDs unless your alias map includes ascorbic acid yet
