from datetime import datetime
import uuid
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Depends

from app.core.auth import require_ops_api_key
from app.core.idempotency_store import SQLiteIdempotencyStore
from app.core.logging import get_logger, log_event
from app.domain.schemas import (
    IngestRequest,
    IngestResponse,
    Event,
    BarcodeScanIngestRequest,
    OpsEventActionRequest,  # ✅ generic ops request
    Decision,
)
from app.services.router import route_event
from app.services.actuator import execute_decision

app = FastAPI(title="AI Control Plane")
logger = get_logger()

# Persistent idempotency store (survives restarts)
idem_store = SQLiteIdempotencyStore()


def _process_ingest(ingest_req: IngestRequest, idempotency_key: Optional[str]) -> IngestResponse:
    # Gate 1: Idempotency-Key is required
    if not idempotency_key:
        log_event(
            logger,
            event_name="ingest_rejected",
            fields={
                "reason": "missing_idempotency_key",
                "event_type": ingest_req.event_type,
                "source": ingest_req.source,
            },
        )
        raise HTTPException(status_code=400, detail="Missing Idempotency-Key header")

    # Gate 2: Reuse existing Event if this key was already processed (persistent)
    existing_event = idem_store.get(idempotency_key)
    if existing_event:
        log_event(
            logger,
            event_name="ingest_duplicate",
            fields={
                "idempotency_key": idempotency_key,
                "event_id": existing_event.event_id,
                "event_type": existing_event.event_type,
                "source": existing_event.source,
            },
        )

        decision = route_event(existing_event)
        log_event(
            logger,
            event_name="decision_created",
            fields={
                "decision_id": decision.decision_id,
                "event_id": decision.event_id,
                "route": decision.route,
                "risk_level": decision.risk_level,
                "reason": decision.reason,
            },
        )

        try:
            action_result = execute_decision(existing_event, decision)
            log_event(
                logger,
                event_name="action_executed" if action_result.status == "executed" else "action_noop",
                fields={
                    "action_id": action_result.action_id,
                    "event_id": action_result.event_id,
                    "decision_id": action_result.decision_id,
                    "action_type": action_result.action_type,
                    "status": action_result.status,
                    "artifact_path": action_result.artifact_path,
                    "reason": action_result.reason,
                },
            )
        except Exception as e:
            log_event(
                logger,
                event_name="action_failed",
                fields={
                    "event_id": existing_event.event_id,
                    "decision_id": decision.decision_id,
                    "route": decision.route,
                    "error": str(e),
                },
            )

        return IngestResponse(event=existing_event, decision=decision)

    # New Event
    event = Event(
        event_id=str(uuid.uuid4()),
        event_type=ingest_req.event_type,
        source=ingest_req.source,
        timestamp=datetime.utcnow(),
        actor=ingest_req.actor,
        payload=ingest_req.payload,
        metadata=ingest_req.metadata,
    )

    log_event(
        logger,
        event_name="ingest_created",
        fields={
            "idempotency_key": idempotency_key,
            "event_id": event.event_id,
            "event_type": event.event_type,
            "source": event.source,
        },
    )

    # Persist Event for idempotency
    idem_store.set(idempotency_key, event)

    # Decide
    decision = route_event(event)
    log_event(
        logger,
        event_name="decision_created",
        fields={
            "decision_id": decision.decision_id,
            "event_id": decision.event_id,
            "route": decision.route,
            "risk_level": decision.risk_level,
            "reason": decision.reason,
        },
    )

    # Act
    try:
        action_result = execute_decision(event, decision)
        log_event(
            logger,
            event_name="action_executed" if action_result.status == "executed" else "action_noop",
            fields={
                "action_id": action_result.action_id,
                "event_id": action_result.event_id,
                "decision_id": action_result.decision_id,
                "action_type": action_result.action_type,
                "status": action_result.status,
                "artifact_path": action_result.artifact_path,
                "reason": action_result.reason,
            },
        )
    except Exception as e:
        log_event(
            logger,
            event_name="action_failed",
            fields={
                "event_id": event.event_id,
                "decision_id": decision.decision_id,
                "route": decision.route,
                "error": str(e),
            },
        )

    return IngestResponse(event=event, decision=decision)


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.get("/ops/ping")
def ops_ping(_: None = Depends(require_ops_api_key)):
    return {"status": "ok"}


@app.post("/ingest/api", response_model=IngestResponse)
def ingest_api(
    req: IngestRequest,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> IngestResponse:
    return _process_ingest(req, idempotency_key)


@app.post("/ingest/barcode_scan", response_model=IngestResponse)
def ingest_barcode_scan(
    req: BarcodeScanIngestRequest,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> IngestResponse:
    """
    Mobile barcode scan ingest.

    Idempotency (frontend-aware):
    - Prefer Idempotency-Key header if client provides it
    - Else derive stable key from (device_id + scan_session_id + barcode)
    - Else reject
    """
    effective_key = idempotency_key
    if not effective_key:
        if req.device_id and req.scan_session_id:
            effective_key = f"scan:{req.device_id}:{req.scan_session_id}:{req.barcode}"
        else:
            raise HTTPException(
                status_code=400,
                detail="Missing Idempotency-Key (provide header or device_id+scan_session_id)",
            )

    ingest_req = IngestRequest(
        source="barcode_scan",
        event_type="barcode_scan",
        actor=req.device_id,
        payload={
            "barcode": req.barcode,
            "symbology": req.symbology,
            "device_id": req.device_id,
            "scan_session_id": req.scan_session_id,
            "app_version": req.app_version,
            "locale": req.locale,
        },
        metadata=req.metadata,
    )

    return _process_ingest(ingest_req, effective_key)


@app.post("/ops/barcode/enrich_ai")
def ops_barcode_enrich_ai(
    req: OpsEventActionRequest,
    _: None = Depends(require_ops_api_key),
):
    """
    Operator-triggered Tier-2 enrichment phase.

    Uses stored Event (via idempotency_key) and executes:
      route = ENRICH_UNKNOWN_INGREDIENTS_AI
    """
    existing_event = idem_store.get(req.idempotency_key)
    if not existing_event:
        raise HTTPException(
            status_code=404,
            detail="Event not found for idempotency_key. Ingest must happen first.",
        )

    if existing_event.source != "barcode_scan":
        raise HTTPException(
            status_code=400,
            detail="AI enrichment supported only for barcode_scan events.",
        )

    decision = Decision(
        decision_id=str(uuid.uuid4()),
        event_id=existing_event.event_id,
        route="ENRICH_UNKNOWN_INGREDIENTS_AI",
        reason="Operator triggered AI enrichment for unknown ingredients",
        risk_level="medium",
        proposed_action={},
    )

    log_event(
        logger,
        event_name="ops_ai_enrichment_requested",
        fields={
            "idempotency_key": req.idempotency_key,
            "event_id": existing_event.event_id,
            "mode": req.mode,
        },
    )

    action_result = execute_decision(existing_event, decision)

    log_event(
        logger,
        event_name="ops_ai_enrichment_completed",
        fields={
            "idempotency_key": req.idempotency_key,
            "event_id": existing_event.event_id,
            "status": action_result.status,
            "artifact_path": action_result.artifact_path,
        },
    )

    return {"event_id": existing_event.event_id, "action_result": action_result.model_dump()}
