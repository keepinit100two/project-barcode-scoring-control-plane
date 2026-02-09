import json
import httpx
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_ai_enrichment_writes_artifact_and_accepts_valid_schema(tmp_path, monkeypatch):
    # ops auth
    monkeypatch.setenv("OPS_API_KEY", "test-key")

    # patch artifact store output dir
    import app.services.actuator as actuator
    from app.core.artifacts import LocalArtifactStore
    monkeypatch.setattr(actuator, "artifact_store", LocalArtifactStore(tmp_path))

    # mock OFF to include an unknown ingredient
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
                        "product_name": "Mock",
                        "brands": "MockBrand",
                        "ingredients_text": "Water, MysteryCompound"
                    }
                },
            )

    monkeypatch.setattr(off_client.httpx, "Client", PatchedOFFClient)

    # IMPORTANT: patch the symbol used inside actuator (not the client module)
    def fake_call(*, event_id, barcode, unknown_ingredients, timeout_s=10.0):
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
                    "hormone_impact": "none",
                    "safety_flags": [],
                    "confidence": 0.6,
                    "notes": "Mock classification"
                }
            ],
            "model": "mock-llm-v0",
            "status": "accepted",
            "error": None
        }

    monkeypatch.setattr(actuator, "call_mock_llm_enrichment", fake_call)

    # ingest barcode scan (normalization runs and writes normalization artifact)
    payload = {
        "barcode": "012345678905",
        "device_id": "device-1",
        "scan_session_id": "session-ai-1",
        "metadata": {}
    }
    r1 = client.post("/ingest/barcode_scan", json=payload)
    assert r1.status_code == 200
    event_id = r1.json()["event"]["event_id"]

    norm_path = tmp_path / f"{event_id}.barcode_normalization.json"
    assert norm_path.exists()

    # trigger AI enrichment via ops endpoint
    idem_key = f"scan:device-1:session-ai-1:012345678905"
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
    assert len(enr["enrichments"]) == 1
