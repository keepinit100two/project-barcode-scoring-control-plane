from datetime import datetime
import uuid
import json
import os
import sqlite3
from pathlib import Path
from typing import Optional, Tuple

from fastapi import FastAPI, Header, HTTPException, Depends

from app.core.auth import require_ops_api_key
from app.core.idempotency_store import SQLiteIdempotencyStore, DB_PATH  # DB_PATH used for barcode search
from app.core.logging import get_logger, log_event
from app.domain.schemas import (
    IngestRequest,
    IngestResponse,
    Event,
    BarcodeScanIngestRequest,
    OpsEventActionRequest,
    Decision,
    ScoreApiResponse,
    ScoreResult,
    ProductNormalizationResult,
    AIEnrichmentResult,
)
from app.services.router import route_event
from app.services.actuator import execute_decision

app = FastAPI(title="AI Control Plane")
logger = get_logger()

idem_store = SQLiteIdempotencyStore()


def _artifact_dir() -> Path:
    """
    Where artifacts are stored in local dev.
    Tests can override via env var ARTIFACT_DIR.
    """
    return Path(os.environ.get("ARTIFACT_DIR", "artifacts/drafts")).resolve()


def _read_artifact_json(filename: str) -> Optional[dict]:
    path = _artifact_dir() / filename
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _process_ingest(ingest_req: IngestRequest, idempotency_key: Optional[str]) -> IngestResponse:
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

    idem_store.set(idempotency_key, event)

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
    existing_event = idem_store.get(req.idempotency_key)
    if not existing_event:
        raise HTTPException(status_code=404, detail="Event not found for idempotency_key. Ingest must happen first.")

    if existing_event.source != "barcode_scan":
        raise HTTPException(status_code=400, detail="AI enrichment supported only for barcode_scan events.")

    decision = Decision(
        decision_id=str(uuid.uuid4()),
        event_id=existing_event.event_id,
        route="ENRICH_UNKNOWN_INGREDIENTS_AI",
        reason="Operator triggered AI enrichment for unknown ingredients",
        risk_level="medium",
        proposed_action={},
    )

    log_event(logger, "ops_ai_enrichment_requested", {"idempotency_key": req.idempotency_key, "event_id": existing_event.event_id})
    action_result = execute_decision(existing_event, decision)
    log_event(logger, "ops_ai_enrichment_completed", {"idempotency_key": req.idempotency_key, "event_id": existing_event.event_id, "status": action_result.status, "artifact_path": action_result.artifact_path})

    return {"event_id": existing_event.event_id, "action_result": action_result.model_dump()}


@app.post("/ops/barcode/score")
def ops_barcode_score(
    req: OpsEventActionRequest,
    _: None = Depends(require_ops_api_key),
):
    existing_event = idem_store.get(req.idempotency_key)
    if not existing_event:
        raise HTTPException(status_code=404, detail="Event not found for idempotency_key. Ingest must happen first.")

    if existing_event.source != "barcode_scan":
        raise HTTPException(status_code=400, detail="Scoring supported only for barcode_scan events.")

    decision = Decision(
        decision_id=str(uuid.uuid4()),
        event_id=existing_event.event_id,
        route="SCORE_PRODUCT",
        reason="Operator triggered deterministic scoring",
        risk_level="low",
        proposed_action={},
    )

    log_event(logger, "ops_score_requested", {"idempotency_key": req.idempotency_key, "event_id": existing_event.event_id})
    action_result = execute_decision(existing_event, decision)
    log_event(logger, "ops_score_completed", {"idempotency_key": req.idempotency_key, "event_id": existing_event.event_id, "status": action_result.status, "artifact_path": action_result.artifact_path})

    return {"event_id": existing_event.event_id, "action_result": action_result.model_dump()}


def _find_latest_event_by_barcode(barcode: str) -> Optional[Tuple[str, Event]]:
    """
    Best-effort convenience: scan idempotency sqlite table to find the latest barcode_scan event for this barcode.
    Returns (idempotency_key, Event) or None.
    """
    if not DB_PATH.exists():
        return None

    best: Optional[Tuple[str, Event]] = None
    best_ts: Optional[str] = None

    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT key, event_json FROM idempotency").fetchall()

    for key, event_json in rows:
        try:
            data = json.loads(event_json)
        except Exception:
            continue
        try:
            ev = Event.model_validate(data)
        except Exception:
            continue

        if ev.source != "barcode_scan":
            continue

        bc = None
        if isinstance(ev.payload, dict):
            bc = ev.payload.get("barcode")

        if str(bc) != str(barcode):
            continue

        ts = ev.timestamp.isoformat()
        if best is None or (best_ts is not None and ts > best_ts) or best_ts is None:
            best = (key, ev)
            best_ts = ts

    return best


def _score_api_response_for_event(idempotency_key: str, ev: Event) -> ScoreApiResponse:
    """
    Determine current state and return a mobile-friendly response without re-running pipeline.
    """
    event_id = ev.event_id

    norm_name = f"{event_id}.barcode_normalization.json"
    enr_name = f"{event_id}.ingredient_enrichment_ai.json"
    score_name = f"{event_id}.score_result.json"

    norm_raw = _read_artifact_json(norm_name)
    enr_raw = _read_artifact_json(enr_name)
    score_raw = _read_artifact_json(score_name)

    artifacts = {
        "normalization": str(_artifact_dir() / norm_name) if norm_raw else None,
        "enrichment": str(_artifact_dir() / enr_name) if enr_raw else None,
        "score": str(_artifact_dir() / score_name) if score_raw else None,
    }

    # If score exists, we're complete
    if score_raw:
        score = ScoreResult.model_validate(score_raw)
        return ScoreApiResponse(
            status="complete",
            idempotency_key=idempotency_key,
            event_id=event_id,
            barcode=(ev.payload.get("barcode") if isinstance(ev.payload, dict) else None),
            score=score,
            next_steps=None,
            artifacts=artifacts,
        )

    # No normalization yet
    if not norm_raw:
        return ScoreApiResponse(
            status="pending_normalization",
            idempotency_key=idempotency_key,
            event_id=event_id,
            barcode=(ev.payload.get("barcode") if isinstance(ev.payload, dict) else None),
            score=None,
            next_steps="Run barcode scan ingest (normalization phase).",
            artifacts=artifacts,
        )

    norm = ProductNormalizationResult.model_validate(norm_raw)
    unknown = [i for i in norm.ingredients if i.ingredient_id is None or i.provenance == "unknown"]

    # Needs enrichment if unknown exists and no accepted enrichment artifact
    if unknown:
        if not enr_raw:
            return ScoreApiResponse(
                status="pending_enrichment",
                idempotency_key=idempotency_key,
                event_id=event_id,
                barcode=norm.barcode,
                score=None,
                next_steps="Trigger AI enrichment via /ops/barcode/enrich_ai.",
                artifacts=artifacts,
            )
        try:
            enr = AIEnrichmentResult.model_validate(enr_raw)
            if enr.status != "accepted":
                return ScoreApiResponse(
                    status="pending_enrichment",
                    idempotency_key=idempotency_key,
                    event_id=event_id,
                    barcode=norm.barcode,
                    score=None,
                    next_steps="AI enrichment was not accepted. Retry /ops/barcode/enrich_ai or proceed with conservative scoring.",
                    artifacts=artifacts,
                )
        except Exception:
            return ScoreApiResponse(
                status="pending_enrichment",
                idempotency_key=idempotency_key,
                event_id=event_id,
                barcode=norm.barcode,
                score=None,
                next_steps="AI enrichment artifact invalid. Retry enrichment.",
                artifacts=artifacts,
            )

    # Normalized (and either no unknowns OR enrichment accepted) but score missing
    return ScoreApiResponse(
        status="pending_score",
        idempotency_key=idempotency_key,
        event_id=event_id,
        barcode=norm.barcode,
        score=None,
        next_steps="Trigger scoring via /ops/barcode/score.",
        artifacts=artifacts,
    )


@app.get("/score/by_idempotency/{idempotency_key}", response_model=ScoreApiResponse)
def get_score_by_idempotency(idempotency_key: str) -> ScoreApiResponse:
    ev = idem_store.get(idempotency_key)
    if not ev:
        raise HTTPException(status_code=404, detail="Event not found for idempotency_key")
    return _score_api_response_for_event(idempotency_key, ev)


@app.get("/score/by_barcode/{barcode}", response_model=ScoreApiResponse)
def get_score_by_barcode(barcode: str) -> ScoreApiResponse:
    found = _find_latest_event_by_barcode(barcode)
    if not found:
        return ScoreApiResponse(
            status="error",
            idempotency_key=None,
            event_id=None,
            barcode=barcode,
            score=None,
            next_steps="No scan event found for this barcode. Scan the product first.",
            artifacts={},
        )
    idem_key, ev = found
    return _score_api_response_for_event(idem_key, ev)
