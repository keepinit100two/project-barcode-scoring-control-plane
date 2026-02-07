import json
import httpx
from fastapi.testclient import TestClient

from app.main import app
import app.services.openfoodfacts_client as off_client

client = TestClient(app)


def test_barcode_scan_normalize_writes_artifact(tmp_path, monkeypatch):
    # Patch artifact store output dir
    import app.services.actuator as actuator
    from app.core.artifacts import LocalArtifactStore
    monkeypatch.setattr(actuator, "artifact_store", LocalArtifactStore(tmp_path))

    # Mock OFF HTTP response by patching httpx.Client used in off_client
    class PatchedClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

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

    monkeypatch.setattr(off_client.httpx, "Client", PatchedClient)

    payload = {
        "barcode": "012345678905",
        "device_id": "device-1",
        "scan_session_id": "session-100",
        "metadata": {}
    }

    r = client.post("/ingest/barcode_scan", json=payload)
    assert r.status_code == 200

    body = r.json()
    event_id = body["event"]["event_id"]

    artifact_path = tmp_path / f"{event_id}.barcode_normalization.json"
    assert artifact_path.exists()

    data = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert data["barcode"] == "012345678905"
    assert data["product_name"] == "Mock Product"
    assert data["brand"] == "MockBrand"
    assert "ingredients" in data
    assert len(data["ingredients"]) == 3
