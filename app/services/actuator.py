import json
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, List

from app.core.artifacts import LocalArtifactStore
from app.domain.schemas import (
    Event,
    Decision,
    ActionResult,
    AIEnrichmentResult,
    ProductNormalizationResult,
)
from app.services.barcode_normalizer import normalize_barcode
from app.services.llm_enrichment_client import call_mock_llm_enrichment
from app.services.scoring_engine import score_product

DEFAULT_DRAFT_DIR = Path(__file__).resolve().parents[2] / "artifacts" / "drafts"
artifact_store = LocalArtifactStore(DEFAULT_DRAFT_DIR)


def _artifact_base_dir() -> Path:
    return getattr(artifact_store, "base_dir", DEFAULT_DRAFT_DIR)


def _read_artifact_json(filename: str) -> Optional[Dict[str, Any]]:
    path = _artifact_base_dir() / filename
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _write_enrichment_artifact(
    *,
    event_id: str,
    barcode: str,
    unknown: List[str],
    raw: Optional[Dict[str, Any]] = None,
    status: str,
    error: Optional[str],
) -> str:
    """
    Always write an enrichment artifact (accepted/rejected/failed) for audit.
    """
    payload: Dict[str, Any] = {
        "schema_version": "ai_enrichment_v0",
        "event_id": event_id,
        "barcode": barcode,
        "unknown_ingredients": unknown,
        "enrichments": [],
        "model": "mock-llm-v0",
        "status": status,
        "error": error,
    }
    if status == "rejected" and raw is not None:
        payload["raw"] = raw

    rel = f"{event_id}.ingredient_enrichment_ai.json"
    return artifact_store.write_json(rel, payload)


def _attempt_ai_enrichment(event_id: str, barcode: str, unknown: List[str]) -> AIEnrichmentResult:
    """
    Tier-2 enrichment call with strict schema gate.
    Returns an AIEnrichmentResult object on accepted.
    Raises exception for 'failed' network errors; schema invalid returns ValueError.
    """
    raw = call_mock_llm_enrichment(
        event_id=event_id,
        barcode=barcode,
        unknown_ingredients=unknown,
    )
    # strict schema validate
    return AIEnrichmentResult.model_validate(raw)


def _write_normalization(event: Event) -> ProductNormalizationResult:
    """
    Deterministic Stage A: OFF lookup + ingredient parsing + canonicalization.
    Writes artifact and returns typed normalization.
    """
    barcode = (event.payload or {}).get("barcode") if isinstance(event.payload, dict) else None
    if not barcode:
        raise ValueError("Missing barcode in event payload")

    norm = normalize_barcode(str(barcode))
    artifact_store.write_json(f"{event.event_id}.barcode_normalization.json", norm.model_dump())
    return norm


def _write_score(event_id: str, normalization: ProductNormalizationResult, enrichment: Optional[AIEnrichmentResult]) -> str:
    score = score_product(event_id=event_id, normalization=normalization, enrichment=enrichment)
    return artifact_store.write_json(f"{event_id}.score_result.json", score.model_dump())


def execute_decision(event: Event, decision: Decision) -> ActionResult:
    action_id = str(uuid.uuid4())

    # -------------------------------
    # AUTO_PROCESS_BARCODE (new default)
    # -------------------------------
    if decision.route == "AUTO_PROCESS_BARCODE":
        # 1) Normalize
        try:
            normalization = _write_normalization(event)
        except Exception as e:
            return ActionResult(
                action_id=action_id,
                event_id=event.event_id,
                decision_id=decision.decision_id,
                action_type="auto_process_barcode",
                status="failed",
                artifact_path=None,
                reason=f"Normalization failed: {e}",
                error_code="NORMALIZATION_FAILED",
                next_steps="Verify barcode payload and OFF availability; retry scan.",
            )

        # 2) Enrich unknown ingredients (schema-gated; may fail/reject)
        unknown = [i.name for i in normalization.ingredients if i.ingredient_id is None or i.provenance == "unknown"]
        enrichment_obj: Optional[AIEnrichmentResult] = None
        enrichment_path: Optional[str] = None

        if unknown:
            try:
                enrichment_obj = _attempt_ai_enrichment(event.event_id, normalization.barcode, unknown)
                # accepted -> store validated schema
                enrichment_path = artifact_store.write_json(
                    f"{event.event_id}.ingredient_enrichment_ai.json",
                    enrichment_obj.model_dump(),
                )
            except Exception as e:
                # Distinguish schema invalid vs call failure:
                # - if model_validate fails => schema invalid -> reject
                # - if call fails => failed
                raw_artifact = None
                status = "failed"
                err = str(e)

                # If it's a schema validation error, reject and preserve raw if possible
                # We can’t reliably capture raw here without changing llm_client, so we preserve the error.
                # (If you want raw capture, we can adjust llm_client to return raw + exception.)
                if "validation" in err.lower() or "field" in err.lower() or "pydantic" in err.lower():
                    status = "rejected"

                enrichment_path = _write_enrichment_artifact(
                    event_id=event.event_id,
                    barcode=normalization.barcode,
                    unknown=unknown,
                    raw=raw_artifact,
                    status=status,
                    error=err,
                )
                enrichment_obj = None
        else:
            # No unknown ingredients: write accepted empty result for audit consistency
            enrichment_obj = AIEnrichmentResult(
                event_id=event.event_id,
                barcode=normalization.barcode,
                unknown_ingredients=[],
                enrichments=[],
                model="mock-llm-v0",
                status="accepted",
                error=None,
            )
            enrichment_path = artifact_store.write_json(
                f"{event.event_id}.ingredient_enrichment_ai.json",
                enrichment_obj.model_dump(),
            )

        # 3) Score (always runs deterministically)
        score_path = _write_score(event.event_id, normalization, enrichment_obj if enrichment_obj and enrichment_obj.status == "accepted" else None)

        # Return ActionResult with all artifact paths (semicolon-separated)
        artifacts = [
            str(_artifact_base_dir() / f"{event.event_id}.barcode_normalization.json"),
            enrichment_path,
            score_path,
        ]
        artifacts = [a for a in artifacts if a]

        return ActionResult(
            action_id=action_id,
            event_id=event.event_id,
            decision_id=decision.decision_id,
            action_type="auto_process_barcode",
            status="executed",
            artifact_path=";".join(artifacts),
            reason="Auto-processed barcode: normalized, enriched (if needed), and scored",
        )

    # -------------------------------
    # Debug / manual phase routes (keep)
    # -------------------------------

    # Stage A only
    if decision.route == "NORMALIZE_BARCODE":
        try:
            normalization = _write_normalization(event)
        except Exception as e:
            return ActionResult(
                action_id=action_id,
                event_id=event.event_id,
                decision_id=decision.decision_id,
                action_type="normalize_barcode",
                status="failed",
                artifact_path=None,
                reason=f"Normalization failed: {e}",
                error_code="NORMALIZATION_FAILED",
                next_steps="Verify barcode payload and OFF availability; retry scan.",
            )

        artifact_path = str(_artifact_base_dir() / f"{event.event_id}.barcode_normalization.json")
        return ActionResult(
            action_id=action_id,
            event_id=event.event_id,
            decision_id=decision.decision_id,
            action_type="normalize_barcode",
            status="executed",
            artifact_path=artifact_path,
            reason="Barcode normalized via Open Food Facts; ingredient normalization complete",
        )

    # Stage B only
    if decision.route == "ENRICH_UNKNOWN_INGREDIENTS_AI":
        norm_raw = _read_artifact_json(f"{event.event_id}.barcode_normalization.json")
        if not norm_raw:
            return ActionResult(
                action_id=action_id,
                event_id=event.event_id,
                decision_id=decision.decision_id,
                action_type="ai_enrich_unknown_ingredients",
                status="failed",
                artifact_path=None,
                reason="Normalization artifact not found; run NORMALIZE_BARCODE first",
                error_code="MISSING_NORMALIZATION_ARTIFACT",
                next_steps="Run barcode normalization before AI enrichment.",
            )

        normalization = ProductNormalizationResult.model_validate(norm_raw)
        unknown = [i.name for i in normalization.ingredients if i.ingredient_id is None or i.provenance == "unknown"]

        if not unknown:
            empty = AIEnrichmentResult(
                event_id=event.event_id,
                barcode=normalization.barcode,
                unknown_ingredients=[],
                enrichments=[],
                model="mock-llm-v0",
                status="accepted",
                error=None,
            )
            path = artifact_store.write_json(f"{event.event_id}.ingredient_enrichment_ai.json", empty.model_dump())
            return ActionResult(
                action_id=action_id,
                event_id=event.event_id,
                decision_id=decision.decision_id,
                action_type="ai_enrich_unknown_ingredients",
                status="executed",
                artifact_path=path,
                reason="No unknown ingredients; AI enrichment skipped",
            )

        # Attempt enrichment
        try:
            enr = _attempt_ai_enrichment(event.event_id, normalization.barcode, unknown)
            path = artifact_store.write_json(f"{event.event_id}.ingredient_enrichment_ai.json", enr.model_dump())
            return ActionResult(
                action_id=action_id,
                event_id=event.event_id,
                decision_id=decision.decision_id,
                action_type="ai_enrich_unknown_ingredients",
                status="executed",
                artifact_path=path,
                reason="AI enrichment accepted and stored",
            )
        except Exception as e:
            # Write failure artifact
            path = _write_enrichment_artifact(
                event_id=event.event_id,
                barcode=normalization.barcode,
                unknown=unknown,
                raw=None,
                status="failed",
                error=str(e),
            )
            return ActionResult(
                action_id=action_id,
                event_id=event.event_id,
                decision_id=decision.decision_id,
                action_type="ai_enrich_unknown_ingredients",
                status="failed",
                artifact_path=path,
                reason="AI enrichment failed",
                error_code="AI_ENRICHMENT_FAILED",
                next_steps="Retry enrichment or proceed with deterministic scoring fallback.",
            )

    # Stage C only
    if decision.route == "SCORE_PRODUCT":
        norm_raw = _read_artifact_json(f"{event.event_id}.barcode_normalization.json")
        if not norm_raw:
            return ActionResult(
                action_id=action_id,
                event_id=event.event_id,
                decision_id=decision.decision_id,
                action_type="score_product",
                status="failed",
                artifact_path=None,
                reason="Normalization artifact not found; run NORMALIZE_BARCODE first",
                error_code="MISSING_NORMALIZATION_ARTIFACT",
                next_steps="Run barcode normalization before scoring.",
            )

        normalization = ProductNormalizationResult.model_validate(norm_raw)

        enr_raw = _read_artifact_json(f"{event.event_id}.ingredient_enrichment_ai.json")
        enrichment: Optional[AIEnrichmentResult] = None
        if enr_raw:
            try:
                enrichment = AIEnrichmentResult.model_validate(enr_raw)
            except Exception:
                enrichment = None

        score_path = _write_score(event.event_id, normalization, enrichment if enrichment and enrichment.status == "accepted" else None)

        return ActionResult(
            action_id=action_id,
            event_id=event.event_id,
            decision_id=decision.decision_id,
            action_type="score_product",
            status="executed",
            artifact_path=score_path,
            reason="Deterministic scoring complete; score artifact written",
        )

    # Legacy: draft ticket
    if decision.route == "CREATE_DRAFT_TICKET":
        rel = f"{event.event_id}.draft_ticket.json"
        payload: Dict[str, Any] = {
            "event_id": event.event_id,
            "decision_id": decision.decision_id,
            "route": decision.route,
            "risk_level": decision.risk_level,
            "reason": decision.reason,
            "proposed_action": decision.proposed_action,
        }
        artifact_path = artifact_store.write_json(rel, payload)
        return ActionResult(
            action_id=action_id,
            event_id=event.event_id,
            decision_id=decision.decision_id,
            action_type="create_ticket_draft",
            status="executed",
            artifact_path=artifact_path,
            reason="Draft ticket artifact written",
        )

    return ActionResult(
        action_id=action_id,
        event_id=event.event_id,
        decision_id=decision.decision_id,
        action_type="noop",
        status="noop",
        artifact_path=None,
        reason=f"No action executed for route: {decision.route}",
    )
