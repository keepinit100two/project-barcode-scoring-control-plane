import json
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

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


def _read_json(relative_path: str) -> Optional[Dict[str, Any]]:
    base_dir = getattr(artifact_store, "base_dir", DEFAULT_DRAFT_DIR)
    path = Path(base_dir) / relative_path
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def execute_decision(event: Event, decision: Decision) -> ActionResult:
    action_id = str(uuid.uuid4())

    # Stage A: Normalize barcode (OFF + deterministic ingredient normalization)
    if decision.route == "NORMALIZE_BARCODE":
        barcode = (event.payload or {}).get("barcode") if isinstance(event.payload, dict) else None
        if not barcode:
            return ActionResult(
                action_id=action_id,
                event_id=event.event_id,
                decision_id=decision.decision_id,
                action_type="normalize_barcode",
                status="failed",
                artifact_path=None,
                reason="Missing barcode in event payload",
                error_code="MISSING_BARCODE",
                next_steps="Rescan barcode and retry.",
            )

        result = normalize_barcode(str(barcode))
        rel = f"{event.event_id}.barcode_normalization.json"
        artifact_path = artifact_store.write_json(rel, result.model_dump())

        return ActionResult(
            action_id=action_id,
            event_id=event.event_id,
            decision_id=decision.decision_id,
            action_type="normalize_barcode",
            status="executed",
            artifact_path=artifact_path,
            reason="Barcode normalized via Open Food Facts; ingredient normalization complete",
        )

    # Stage B: AI enrichment for unknown ingredients ONLY
    if decision.route == "ENRICH_UNKNOWN_INGREDIENTS_AI":
        norm_rel = f"{event.event_id}.barcode_normalization.json"
        norm_raw = _read_json(norm_rel)
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
        barcode = normalization.barcode

        unknown = [ing.name for ing in normalization.ingredients if ing.ingredient_id is None or ing.provenance == "unknown"]

        # Always write an artifact for audit, even if no unknown ingredients
        if not unknown:
            empty = AIEnrichmentResult(
                event_id=event.event_id,
                barcode=barcode,
                unknown_ingredients=[],
                enrichments=[],
                model="mock-llm-v0",
                status="accepted",
                error=None,
            )
            rel = f"{event.event_id}.ingredient_enrichment_ai.json"
            artifact_path = artifact_store.write_json(rel, empty.model_dump())
            return ActionResult(
                action_id=action_id,
                event_id=event.event_id,
                decision_id=decision.decision_id,
                action_type="ai_enrich_unknown_ingredients",
                status="executed",
                artifact_path=artifact_path,
                reason="No unknown ingredients; AI enrichment skipped",
            )

        # Call mock LLM (Tier-2)
        try:
            raw = call_mock_llm_enrichment(
                event_id=event.event_id,
                barcode=barcode,
                unknown_ingredients=unknown,
            )
        except Exception as e:
            rel = f"{event.event_id}.ingredient_enrichment_ai.json"
            artifact_path = artifact_store.write_json(
                rel,
                {
                    "schema_version": "ai_enrichment_v0",
                    "event_id": event.event_id,
                    "barcode": barcode,
                    "unknown_ingredients": unknown,
                    "enrichments": [],
                    "model": "mock-llm-v0",
                    "status": "failed",
                    "error": str(e),
                },
            )
            return ActionResult(
                action_id=action_id,
                event_id=event.event_id,
                decision_id=decision.decision_id,
                action_type="ai_enrich_unknown_ingredients",
                status="failed",
                artifact_path=artifact_path,
                reason="AI enrichment call failed",
                error_code="AI_ENRICHMENT_FAILED",
                next_steps="Retry enrichment or score deterministically without unknown enrichment.",
            )

        # Strict schema validation
        try:
            validated = AIEnrichmentResult.model_validate(raw)
            rel = f"{event.event_id}.ingredient_enrichment_ai.json"
            artifact_path = artifact_store.write_json(rel, validated.model_dump())
            return ActionResult(
                action_id=action_id,
                event_id=event.event_id,
                decision_id=decision.decision_id,
                action_type="ai_enrich_unknown_ingredients",
                status="executed",
                artifact_path=artifact_path,
                reason="AI enrichment accepted and stored",
            )
        except Exception as e:
            rel = f"{event.event_id}.ingredient_enrichment_ai.json"
            artifact_path = artifact_store.write_json(
                rel,
                {
                    "schema_version": "ai_enrichment_v0",
                    "event_id": event.event_id,
                    "barcode": barcode,
                    "unknown_ingredients": unknown,
                    "enrichments": [],
                    "model": "mock-llm-v0",
                    "status": "rejected",
                    "error": f"Schema validation failed: {str(e)}",
                    "raw": raw,
                },
            )
            return ActionResult(
                action_id=action_id,
                event_id=event.event_id,
                decision_id=decision.decision_id,
                action_type="ai_enrich_unknown_ingredients",
                status="failed",
                artifact_path=artifact_path,
                reason="AI enrichment output rejected (schema invalid)",
                error_code="AI_SCHEMA_INVALID",
                next_steps="Retry enrichment; if repeated, ignore unknown ingredients or escalate to manual review.",
            )

    # Stage C: Deterministic scoring
    if decision.route == "SCORE_PRODUCT":
        norm_rel = f"{event.event_id}.barcode_normalization.json"
        norm_raw = _read_json(norm_rel)
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

        enr_rel = f"{event.event_id}.ingredient_enrichment_ai.json"
        enr_raw = _read_json(enr_rel)
        enrichment = None
        if enr_raw:
            try:
                enrichment = AIEnrichmentResult.model_validate(enr_raw)
            except Exception:
                enrichment = None

        score = score_product(
            event_id=event.event_id,
            normalization=normalization,
            enrichment=enrichment,
        )

        rel = f"{event.event_id}.score_result.json"
        artifact_path = artifact_store.write_json(rel, score.model_dump())

        return ActionResult(
            action_id=action_id,
            event_id=event.event_id,
            decision_id=decision.decision_id,
            action_type="score_product",
            status="executed",
            artifact_path=artifact_path,
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
