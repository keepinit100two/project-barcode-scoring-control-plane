from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def test_barcode_scan_requires_idempotency_key_when_missing_device_and_session():
    payload = {
        "barcode": "012345678905",
        "metadata": {}
    }
    r = client.post("/ingest/barcode_scan", json=payload)
    assert r.status_code == 400


def test_barcode_scan_derives_idempotency_key_from_device_and_session():
    payload = {
        "barcode": "012345678905",
        "device_id": "device-1",
        "scan_session_id": "session-1",
        "metadata": {}
    }
    r = client.post("/ingest/barcode_scan", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["event"]["source"] == "barcode_scan"
    assert body["event"]["event_type"] == "barcode_scan"


def test_barcode_scan_idempotency_dedupes_duplicate_scans():
    payload = {
        "barcode": "012345678905",
        "device_id": "device-1",
        "scan_session_id": "session-2",
        "metadata": {}
    }

    r1 = client.post("/ingest/barcode_scan", json=payload)
    assert r1.status_code == 200

    r2 = client.post("/ingest/barcode_scan", json=payload)
    assert r2.status_code == 200
    # On duplicate, event_id should be identical because persistent idempotency store returns the same event
    assert r1.json()["event"]["event_id"] == r2.json()["event"]["event_id"]
