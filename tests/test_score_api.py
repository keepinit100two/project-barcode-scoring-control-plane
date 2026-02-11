import httpx
from fastapi.testclient import TestClient

from app.main import app
import app.services.openfoodfacts_client as off_client

client = TestClient(app)


def test_score_api_complete_after_auto_process(tmp_path, monkeypatch):
    monkeypatch.setenv("OPS_API_KEY", "test-key")
    monkeypatch.setenv("ARTIFACT_DIR", str(tmp_path))

    import app.services.actuator as actuator
    from app.core.artifacts import LocalArtifactStore
    monkeypatch.setattr(actuator, "artifact_store", LocalArtifactStore(tmp_path))

    # Mock OFF to keep it deterministic
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
                        "ingredients_text": "Water, Baking Soda, Fragrance"
                    }
                },
            )

    monkeypatch.setattr(off_client.httpx, "Client", PatchedOFFClient)

    # Patch AI call to not be used (no unknowns), but safe if called
    def fake_ai_call(*, event_id, barcode, unknown_ingredients, timeout_s=10.0):
        return {
            "schema_version": "ai_enrichment_v0",
            "event_id": event_id,
            "barcode": barcode,
            "unknown_ingredients": unknown_ingredients,
            "enrichments": [],
            "model": "mock-llm-v0",
            "status": "accepted",
            "error": None,
        }

    monkeypatch.setattr(actuator, "call_mock_llm_enrichment", fake_ai_call)

    payload = {
        "barcode": "012345678905",
        "device_id": "device-1",
        "scan_session_id": "session-scoreapi-1",
        "metadata": {},
    }

    r = client.post("/ingest/barcode_scan", json=payload)
    assert r.status_code == 200
    idem_key = "scan:device-1:session-scoreapi-1:012345678905"

    r_done = client.get(f"/score/by_idempotency/{idem_key}")
    assert r_done.status_code == 200
    assert r_done.json()["status"] == "complete"

    r_bar = client.get("/score/by_barcode/012345678905")
    assert r_bar.status_code == 200
    assert r_bar.json()["status"] == "complete"
