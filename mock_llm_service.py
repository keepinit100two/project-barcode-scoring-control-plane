import hashlib
from typing import List, Optional, Dict, Any

from fastapi import FastAPI
from pydantic import BaseModel, Field

app = FastAPI(title="Mock LLM Service (Ingredient Enrichment)")


class EnrichRequest(BaseModel):
    event_id: str = Field(..., description="Event ID (used to make failure deterministic)")
    barcode: str = Field(..., description="Barcode")
    unknown_ingredients: List[str] = Field(default_factory=list)


@app.get("/health")
def health():
    return {"status": "ok"}


def _should_fail(event_id: str, unknown_ingredients: List[str]) -> bool:
    """
    Deterministic ~10% failure:
    hash(event_id + joined ingredients) % 10 == 0 => fail
    """
    key = (event_id + "|" + "|".join(unknown_ingredients)).encode("utf-8")
    h = hashlib.sha256(key).hexdigest()
    return int(h, 16) % 10 == 0


@app.post("/enrich/ingredients")
def enrich(req: EnrichRequest) -> Dict[str, Any]:
    # 10% deterministic failure: return malformed JSON shape
    if _should_fail(req.event_id, req.unknown_ingredients):
        # Malformed output (missing required fields, wrong types)
        return {
            "oops": "malformed",
            "enrichments": "not-a-list",
        }

    # Otherwise return valid, structured enrichment
    enrichments = []
    for ing in req.unknown_ingredients:
        k = ing.strip().lower()

        # Deterministic fake classifications
        if "fragrance" in k or "parfum" in k:
            enrichments.append({
                "ingredient_name": ing,
                "suggested_canonical_name": "Fragrance",
                "category": "fragrance",
                "hormone_impact": "medium",
                "safety_flags": ["endocrine_disruptor_suspected"],
                "confidence": 0.72,
                "notes": "Fragrance mix may contain endocrine-active compounds; treat as medium impact."
            })
        elif "bht" in k:
            enrichments.append({
                "ingredient_name": ing,
                "suggested_canonical_name": "Butylated Hydroxytoluene",
                "category": "preservative",
                "hormone_impact": "low",
                "safety_flags": ["controversial_additive"],
                "confidence": 0.66,
                "notes": "Common preservative; low hormone concern but controversial."
            })
        else:
            enrichments.append({
                "ingredient_name": ing,
                "suggested_canonical_name": ing,
                "category": "unknown",
                "hormone_impact": "none",
                "safety_flags": [],
                "confidence": 0.55,
                "notes": "No strong signals; classified as none."
            })

    return {
        "schema_version": "ai_enrichment_v0",
        "event_id": req.event_id,
        "barcode": req.barcode,
        "unknown_ingredients": req.unknown_ingredients,
        "enrichments": enrichments,
        "model": "mock-llm-v0",
        "status": "accepted",
        "error": None,
    }
