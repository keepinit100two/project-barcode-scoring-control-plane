import json
import httpx
from fastapi.testclient import TestClient

from app.main import app
import app.services.openfoodfacts_client as off_client

client = TestClient(app)


def test_auto_process_writes_score_artifact_and_breakdown(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTIFACT_DIR", str(tmp_path))

    import app.services.actuator as actuator
    from app.core.artifacts import LocalArtifactStore
    monkeypatch.setattr(actuator, "artifact_store", LocalArtifactStore(tmp_path))

    # OFF returns: Water (known), Fragrance (known), MysteryCompound (unknown)
    class PatchedOFFClient:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, exc_type, exc, tb): return False
        def get(self, url):
            req = httpx.Request("GET", url)
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

    # Patch AI enrichment to deterministic accepted enrichment
    def fake_ai_call(*, event_id, barcode, unknown_ingredients, timeout_s=10.0):
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
                    "notes": "Mock enrichment"
                }
            ],
            "model": "mock-llm-v0",
            "status": "accepted",
            "error": None,
        }

    monkeypatch.setattr(actuator, "call_mock_llm_enrichment", fake_ai_call)

    payload = {
        "barcode": "012345678905",
        "device_id": "device-1",
        "scan_session_id": "session-score-1",
        "metadata": {},
    }

    r = client.post("/ingest/barcode_scan", json=payload)
    assert r.status_code == 200
    event_id = r.json()["event"]["event_id"]

    score_path = tmp_path / f"{event_id}.score_result.json"
    assert score_path.exists()

    score = json.loads(score_path.read_text(encoding="utf-8"))
    assert score["safety_score"] == 75
    assert score["hormone_score"] == 80
    assert len(score["contributions"]) == 3
