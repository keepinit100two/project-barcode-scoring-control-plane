import json
import httpx
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_ai_enrichment_rejects_invalid_schema_and_writes_artifact(tmp_path, monkeypatch):
    """
    If the AI enrichment returns malformed JSON (schema-invalid),
    the system must:
      - write an enrichment artifact anyway (audit trail)
      - mark status as 'rejected'
      - include an error message
    """
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

    # IMPORTANT: patch the symbol used inside actuator to return malformed schema
    def fake_bad_call(*, event_id, barcode, unknown_ingredients, timeout_s=10.0):
        # Malformed: missing required fields, wrong types
        return {
            "oops": "malformed",
            "enrichments": "not-a-list",
        }

    monkeypatch.setattr(actuator, "call_mock_llm_enrichment", fake_bad_call)

    # ingest barcode scan (normalization runs and writes normalization artifact)
    payload = {
        "barcode": "012345678905",
        "device_id": "device-1",
        "scan_session_id": "session-ai-bad-1",
        "metadata": {}
    }
    r1 = client.post("/ingest/barcode_scan", json=payload)
    assert r1.status_code == 200
    event_id = r1.json()["event"]["event_id"]

    norm_path = tmp_path / f"{event_id}.barcode_normalization.json"
    assert norm_path.exists()

    # trigger AI enrichment via ops endpoint
    idem_key = f"scan:device-1:session-ai-bad-1:012345678905"
    r2 = client.post(
        "/ops/barcode/enrich_ai",
        json={"idempotency_key": idem_key, "mode": "draft"},
        headers={"X-API-Key": "test-key"},
    )
    assert r2.status_code == 200

    enr_path = tmp_path / f"{event_id}.ingredient_enrichment_ai.json"
    assert enr_path.exists()

    enr = json.loads(enr_path.read_text(encoding="utf-8"))

    # The actuator writes 'rejected' artifact when schema validation fails
    assert enr["status"] == "rejected"
    assert "error" in enr and enr["error"]
    # raw should be captured for ops/debug
    assert "raw" in enr
