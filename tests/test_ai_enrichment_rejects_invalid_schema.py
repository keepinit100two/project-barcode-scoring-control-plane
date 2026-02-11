import json
import httpx
from fastapi.testclient import TestClient

from app.main import app
import app.services.openfoodfacts_client as off_client

client = TestClient(app)


def test_auto_process_marks_enrichment_not_accepted_on_schema_invalid(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTIFACT_DIR", str(tmp_path))

    import app.services.actuator as actuator
    from app.core.artifacts import LocalArtifactStore
    monkeypatch.setattr(actuator, "artifact_store", LocalArtifactStore(tmp_path))

    # OFF returns unknown ingredient
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

    # Patch AI call to return malformed schema
    def fake_bad_ai_call(*, event_id, barcode, unknown_ingredients, timeout_s=10.0):
        return {"oops": "malformed", "enrichments": "not-a-list"}

    monkeypatch.setattr(actuator, "call_mock_llm_enrichment", fake_bad_ai_call)

    payload = {
        "barcode": "012345678905",
        "device_id": "device-1",
        "scan_session_id": "session-ai-bad-1",
        "metadata": {}
    }

    r = client.post("/ingest/barcode_scan", json=payload)
    assert r.status_code == 200
    event_id = r.json()["event"]["event_id"]

    enr_path = tmp_path / f"{event_id}.ingredient_enrichment_ai.json"
    assert enr_path.exists()

    enr = json.loads(enr_path.read_text(encoding="utf-8"))
    assert enr["status"] in ("rejected", "failed")
    assert enr.get("error")
