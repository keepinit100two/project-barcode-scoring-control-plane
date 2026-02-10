import json
import httpx
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_ops_score_writes_score_artifact_with_deterministic_breakdown(tmp_path, monkeypatch):
    # --- Ops auth ---
    monkeypatch.setenv("OPS_API_KEY", "test-key")

    # --- Patch artifact store to tmp_path (so no real artifacts are touched) ---
    import app.services.actuator as actuator
    from app.core.artifacts import LocalArtifactStore
    monkeypatch.setattr(actuator, "artifact_store", LocalArtifactStore(tmp_path))

    # --- Mock OpenFoodFacts HTTP call ---
    import app.services.openfoodfacts_client as off_client

    class PatchedOFFClient:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, exc_type, exc, tb): return False
        def get(self, url):
            req = httpx.Request("GET", url)
            # Includes:
            # - Water (known)
            # - Fragrance (known)
            # - MysteryCompound (unknown -> will be AI enriched)
            return httpx.Response(
                200,
                request=req,
                json={
                    "product": {
                        "product_name": "Mock Product",
                        "brands": "MockBrand",
                        "ingredients_text": "Water, Fragrance, MysteryCompound"
                    }
                },
            )

    monkeypatch.setattr(off_client.httpx, "Client", PatchedOFFClient)

    # --- Patch AI enrichment call inside actuator (IMPORTANT: patch actuator symbol) ---
    def fake_ai_call(*, event_id, barcode, unknown_ingredients, timeout_s=10.0):
        # We expect exactly one unknown ingredient: "MysteryCompound"
        assert len(unknown_ingredients) == 1
        assert unknown_ingredients[0] == "MysteryCompound"

        return {
            "schema_version": "ai_enrichment_v0",
            "event_id": event_id,
            "barcode": barcode,
            "unknown_ingredients": unknown_ingredients,
            "enrichments": [
                {
                    "ingredient_name": "MysteryCompound",
                    "suggested_canonical_name": "MysteryCompound",
                    "category": "unknown",
                    "hormone_impact": "medium",
                    "safety_flags": ["endocrine_disruptor_suspected"],
                    "confidence": 0.75,
                    "notes": "Mock enrichment: treat as medium hormone impact with endocrine flag",
                }
            ],
            "model": "mock-llm-v0",
            "status": "accepted",
            "error": None,
        }

    monkeypatch.setattr(actuator, "call_mock_llm_enrichment", fake_ai_call)

    # --- Step 1: Ingest barcode scan (routes to NORMALIZE_BARCODE, writes normalization artifact) ---
    payload = {
        "barcode": "012345678905",
        "device_id": "device-1",
        "scan_session_id": "session-score-1",
        "metadata": {},
    }

    r1 = client.post("/ingest/barcode_scan", json=payload)
    assert r1.status_code == 200
    body1 = r1.json()
    event_id = body1["event"]["event_id"]

    norm_path = tmp_path / f"{event_id}.barcode_normalization.json"
    assert norm_path.exists()

    # --- Step 2: Trigger AI enrichment via ops endpoint ---
    idem_key = f"scan:device-1:session-score-1:012345678905"

    r2 = client.post(
        "/ops/barcode/enrich_ai",
        json={"idempotency_key": idem_key, "mode": "draft"},
        headers={"X-API-Key": "test-key"},
    )
    assert r2.status_code == 200

    enr_path = tmp_path / f"{event_id}.ingredient_enrichment_ai.json"
    assert enr_path.exists()

    enr = json.loads(enr_path.read_text(encoding="utf-8"))
    assert enr["status"] == "accepted"
    assert len(enr["unknown_ingredients"]) == 1

    # --- Step 3: Trigger scoring via ops endpoint ---
    r3 = client.post(
        "/ops/barcode/score",
        json={"idempotency_key": idem_key, "mode": "draft"},
        headers={"X-API-Key": "test-key"},
    )
    assert r3.status_code == 200

    score_path = tmp_path / f"{event_id}.score_result.json"
    assert score_path.exists()

    score = json.loads(score_path.read_text(encoding="utf-8"))

    # --- Deterministic expectations based on configs/scoring.json ---
    #
    # Safety starts at 100.
    # - ING_FRAGRANCE has safety_penalty 10 -> safety -10
    # Unknown ingredient AI accepted:
    #   hormone_impact medium -> +25 hormone
    #   flag endocrine_disruptor_suspected -> safety -15 and hormone +35
    #
    # Hormone starts at 0.
    # - ING_FRAGRANCE has hormone_points 20 -> +20
    #
    # Totals:
    # safety = 100 - 10 - 15 = 75
    # hormone = 0 + 20 + 25 + 35 = 80

    assert score["safety_score"] == 75
    assert score["hormone_score"] == 80

    # Explainability checks
    contribs = score["contributions"]
    assert len(contribs) == 3

    # Find the unknown ingredient contribution
    mystery = next(c for c in contribs if c["ingredient_name"] == "MysteryCompound")
    assert mystery["provenance"] == "ai_enriched"
    assert "ai:hormone_impact:medium" in mystery["rule_ids"]
    assert "ai:flag:endocrine_disruptor_suspected" in mystery["rule_ids"]
    assert mystery["safety_delta"] == -15
    assert mystery["hormone_delta"] == 60  # 25 (medium) + 35 (flag)

    fragrance = next(c for c in contribs if c["ingredient_id"] == "ING_FRAGRANCE")
    assert fragrance["safety_delta"] == -10
    assert fragrance["hormone_delta"] == 20
    assert any(r.startswith("known:ING_FRAGRANCE") for r in fragrance["rule_ids"])
