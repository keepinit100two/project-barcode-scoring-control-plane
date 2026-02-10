import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.domain.schemas import (
    ProductNormalizationResult,
    AIEnrichmentResult,
    ScoreResult,
    IngredientScoreContribution,
)

_SCORING_PATH = Path("configs/scoring.json")


def _load_scoring_config() -> Dict[str, Any]:
    return json.loads(_SCORING_PATH.read_text(encoding="utf-8"))


def score_product(
    *,
    event_id: str,
    normalization: ProductNormalizationResult,
    enrichment: Optional[AIEnrichmentResult] = None,
) -> ScoreResult:
    cfg = _load_scoring_config()

    safety = int(cfg["scales"]["safety_score"]["start"])
    hormone = int(cfg["scales"]["hormone_score"]["start"])

    known_rules = cfg.get("known_ingredient_rules", {})
    flag_penalty = cfg.get("flags_penalty", {})
    hormone_points = cfg.get("hormone_impact_points", {})
    unknown_policy = cfg.get("unknown_ingredient_policy", {})

    enrich_by_name: Dict[str, Dict[str, Any]] = {}
    if enrichment and enrichment.status == "accepted":
        for e in enrichment.enrichments:
            enrich_by_name[e.ingredient_name] = e.model_dump()

    contributions: List[IngredientScoreContribution] = []

    for ing in normalization.ingredients:
        name = ing.name
        ing_id = ing.ingredient_id
        prov = ing.provenance

        safety_delta = 0
        hormone_delta = 0
        rule_ids: List[str] = []
        notes: Optional[str] = None

        if ing_id and ing_id in known_rules:
            rule = known_rules[ing_id]
            sp = int(rule.get("safety_penalty", 0))
            hp = int(rule.get("hormone_points", 0))
            safety_delta -= sp
            hormone_delta += hp
            rule_ids.append(f"known:{ing_id}")
            notes = rule.get("notes")
        else:
            # unknown ingredient path
            ai_row = enrich_by_name.get(name)
            if ai_row and unknown_policy.get("if_ai_accepted_use_ai", True):
                prov = "ai_enriched"
                # use AI bins deterministically
                hi = (ai_row.get("hormone_impact") or "none").lower()
                hormone_delta += int(hormone_points.get(hi, 0))
                rule_ids.append(f"ai:hormone_impact:{hi}")

                # apply deterministic penalties for flags
                flags = ai_row.get("safety_flags") or []
                for f in flags:
                    fp = flag_penalty.get(f)
                    if fp:
                        safety_delta -= int(fp.get("safety", 0))
                        hormone_delta += int(fp.get("hormone", 0))
                        rule_ids.append(f"ai:flag:{f}")

                notes = ai_row.get("notes")
            else:
                # AI missing or rejected -> conservative fallback
                fallback = unknown_policy.get("if_ai_rejected_or_missing", {})
                safety_delta -= int(fallback.get("safety_penalty", 0))
                hormone_delta += int(fallback.get("hormone_points", 0))
                rule_ids.append("unknown:fallback")
                notes = fallback.get("notes")

        safety += safety_delta
        hormone += hormone_delta

        contributions.append(
            IngredientScoreContribution(
                ingredient_name=name,
                ingredient_id=ing_id,
                provenance=prov,
                safety_delta=safety_delta,
                hormone_delta=hormone_delta,
                rule_ids=rule_ids,
                notes=notes,
            )
        )

    # clamp to ranges
    safety = max(cfg["scales"]["safety_score"]["min"], min(cfg["scales"]["safety_score"]["max"], safety))
    hormone = max(cfg["scales"]["hormone_score"]["min"], min(cfg["scales"]["hormone_score"]["max"], hormone))

    return ScoreResult(
        event_id=event_id,
        barcode=normalization.barcode,
        product_name=normalization.product_name,
        brand=normalization.brand,
        safety_score=int(safety),
        hormone_score=int(hormone),
        contributions=contributions,
        notes="Deterministic scoring from canonical ingredients + accepted AI enrichment only",
    )
