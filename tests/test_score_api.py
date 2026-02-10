import json
import httpx
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_score_api_pending_then_complete(tmp_path, monkeypatch):
    # ops auth
    monkeypatch.setenv("OPS_API_KEY", "test-key")

    # artifact dir for API reads
    monkeypatch.setenv("ARTIFACT_DIR", str(tmp_path))

    # patch actuator artifact store
    import app.services.actuator as actuator
    from app.core.artifacts import LocalArtifactStore
    monkeypatch.setattr(actuator, "artifact_store", LocalArtifactStore(tmp_path))

    # mock OFF
    import app.services.openfoodfacts_client as off_client

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

    # patch AI enrichment inside actuator
    def fake_ai_call(*, event_id, barcode, unknown_ingredients, timeout_s=10.0):
        return {
            "schema_version": "ai_enrichment_v0",
            "event_id": event_id,
            "barcode": barcode,
            "unknown_ingredients": unknown_ingredients,
            "enrichments": [
                {
                    "ingredient_name": unknown_ingredients[0],
                    "suggested_canonical_name": unknown_ingredients[0],
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

    # ingest scan
    payload = {
        "barcode": "012345678905",
        "device_id": "device-1",
        "scan_session_id": "session-scoreapi-1",
        "metadata": {},
    }
    r1 = client.post("/ingest/barcode_scan", json=payload)
    assert r1.status_code == 200
    event_id = r1.json()["event"]["event_id"]
    idem_key = "scan:device-1:session-scoreapi-1:012345678905"

    # score API should be pending enrichment initially (no enrichment artifact yet)
    r_pending = client.get(f"/score/by_idempotency/{idem_key}")
    assert r_pending.status_code == 200
    assert r_pending.json()["status"] in ("pending_enrichment", "pending_score", "pending_normalization")

    # enrich + score
    r2 = client.post("/ops/barcode/enrich_ai", json={"idempotency_key": idem_key}, headers={"X-API-Key": "test-key"})
    assert r2.status_code == 200

    r3 = client.post("/ops/barcode/score", json={"idempotency_key": idem_key}, headers={"X-API-Key": "test-key"})
    assert r3.status_code == 200

    # now score endpoint should be complete
    r_done = client.get(f"/score/by_idempotency/{idem_key}")
    assert r_done.status_code == 200
    body = r_done.json()
    assert body["status"] == "complete"
    assert body["score"]["event_id"] == event_id
    assert body["score"]["safety_score"] == 75
    assert body["score"]["hormone_score"] == 80

    # convenience barcode lookup should also return complete
    r_bar = client.get("/score/by_barcode/012345678905")
    assert r_bar.status_code == 200
    assert r_bar.json()["status"] == "complete"
