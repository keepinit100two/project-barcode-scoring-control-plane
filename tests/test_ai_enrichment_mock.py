import json
import httpx
from fastapi.testclient import TestClient

from app.main import app
import app.services.openfoodfacts_client as off_client

client = TestClient(app)


def test_auto_process_accepts_ai_enrichment_when_schema_valid(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTIFACT_DIR", str(tmp_path))

    import app.services.actuator as actuator
    from app.core.artifacts import LocalArtifactStore
    monkeypatch.setattr(actuator, "artifact_store", LocalArtifactStore(tmp_path))

    # OFF returns one unknown ingredient so AI will be invoked
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

    # Patch the symbol used inside actuator
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

    monkeypatch.setattr(actuator, "call_mock_llm_enrichment", fake_ai_call)

    payload = {
        "barcode": "012345678905",
        "device_id": "device-1",
        "scan_session_id": "session-ai-ok-1",
        "metadata": {}
    }

    r = client.post("/ingest/barcode_scan", json=payload)
    assert r.status_code == 200
    event_id = r.json()["event"]["event_id"]

    enr_path = tmp_path / f"{event_id}.ingredient_enrichment_ai.json"
    assert enr_path.exists()

    enr = json.loads(enr_path.read_text(encoding="utf-8"))
    assert enr["status"] == "accepted"
    assert len(enr["unknown_ingredients"]) == 1
    assert len(enr["enrichments"]) == 1
