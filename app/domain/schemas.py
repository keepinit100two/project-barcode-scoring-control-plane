from datetime import datetime
from typing import Any, Dict, Optional, List, Literal

from pydantic import BaseModel, Field


# ---------------------------
# Ingest schemas
# ---------------------------

class IngestRequest(BaseModel):
    event_type: str = Field(..., description="Type of event, e.g. support_request, barcode_scan")
    source: str = Field("api", description="Where this event came from, e.g. api, barcode_scan")
    actor: Optional[str] = Field(None, description="Who initiated the event (user/device), if available")
    payload: Dict[str, Any] = Field(..., description="Core content of the event")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Extra context for debugging/routing")


class BarcodeScanIngestRequest(BaseModel):
    barcode: str = Field(..., description="UPC/EAN code scanned by the user")
    symbology: Optional[str] = Field(None, description="Optional symbology hint: UPC-A, EAN-13, etc.")
    device_id: Optional[str] = Field(None, description="Stable device identifier (if available)")
    scan_session_id: Optional[str] = Field(None, description="Client-generated scan session id (if available)")
    app_version: Optional[str] = Field(None, description="Mobile app version")
    locale: Optional[str] = Field(None, description="Locale, e.g. en-US")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional client context")


# ---------------------------
# Canonical Event / Decision / Response
# ---------------------------

class Event(BaseModel):
    event_id: str = Field(..., description="Unique identifier for this event")
    event_type: str
    source: str
    timestamp: datetime
    actor: Optional[str] = None
    payload: Dict[str, Any]
    metadata: Dict[str, Any]


class Decision(BaseModel):
    decision_id: str = Field(..., description="Unique identifier for this decision")
    event_id: str = Field(..., description="The event this decision was derived from")
    route: str = Field(..., description="Chosen route, e.g. REQUEST_MORE_INFO, NORMALIZE_BARCODE")
    reason: str = Field(..., description="Human-readable reason")
    risk_level: str = Field("low", description="Risk level: low, medium, high")
    proposed_action: Dict[str, Any] = Field(default_factory=dict, description="Optional structured action request")

    # Error taxonomy fields (optional)
    error_code: Optional[str] = Field(
        None,
        description="Machine-readable error code for UI/ops (e.g. MISSING_REQUIRED_FIELD)",
    )
    missing_fields: List[str] = Field(
        default_factory=list,
        description="If request is incomplete, list missing fields here",
    )
    next_steps: Optional[str] = Field(
        None,
        description="Human-readable guidance for what should happen next",
    )


class IngestResponse(BaseModel):
    event: Event
    decision: Decision


class ActionResult(BaseModel):
    action_id: str = Field(..., description="Unique identifier for this action execution")
    event_id: str = Field(..., description="Event the action corresponds to")
    decision_id: str = Field(..., description="Decision that triggered this action")
    action_type: str = Field(..., description="Type of action executed (or attempted)")
    status: str = Field(..., description="Outcome: executed, skipped, noop, failed")
    artifact_path: Optional[str] = Field(None, description="Where an artifact was stored (if any)")
    reason: str = Field(..., description="Human-readable explanation of what happened")

    # Error taxonomy fields (optional)
    error_code: Optional[str] = Field(
        None,
        description="Machine-readable error code if action failed or was skipped for a known reason",
    )
    next_steps: Optional[str] = Field(
        None,
        description="Human-readable guidance for operator or caller",
    )


# ---------------------------
# Barcode normalization canonical output
# ---------------------------

class NormalizedIngredient(BaseModel):
    ingredient_id: Optional[str] = Field(None, description="Canonical internal id if matched")
    name: str = Field(..., description="Human-readable ingredient name")
    canonical_name: Optional[str] = Field(None, description="Canonical normalized name")
    provenance: str = Field(..., description="db_match | alias_match | unknown")
    notes: Optional[str] = Field(None, description="Explainability notes")


class ProductNormalizationResult(BaseModel):
    barcode: str
    product_name: Optional[str] = None
    brand: Optional[str] = None
    ingredients_raw: str
    ingredients: List[NormalizedIngredient] = Field(default_factory=list)
