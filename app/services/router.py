import json
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from app.domain.schemas import Event, Decision

_CONFIG_PATH = Path("configs/routing.json")


def _load_config() -> Dict[str, Any]:
    return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))


def _get_text(event: Event) -> str:
    payload: Any = event.payload or {}
    if isinstance(payload, dict):
        text = payload.get("text")
        if isinstance(text, str):
            return text
    return ""


def _get_by_path(event: Event, path: str) -> Any:
    """
    Resolve dotted paths like:
      payload.barcode
      metadata.normalized.order_id

    Supports Event attributes and nested dicts.
    """
    parts = path.split(".")
    cur: Any = event
    for part in parts:
        if cur is None:
            return None

        # Event attribute access
        if hasattr(cur, part):
            cur = getattr(cur, part)
            continue

        # Dict traversal
        if isinstance(cur, dict):
            cur = cur.get(part)
            continue

        return None
    return cur


def _missing_fields(event: Event, field_paths: list[str]) -> list[str]:
    missing: list[str] = []
    for p in field_paths:
        v = _get_by_path(event, p)
        if v in (None, "", [], {}):
            missing.append(p)
    return missing


def route_event(event: Event) -> Decision:
    """
    Deterministic, config-driven router.

    Rule order:
      1) Security keywords -> ESCALATE_HUMAN
      2) Source policy (if configured):
          - required_fields gate -> REQUEST_MORE_INFO
          - otherwise -> source default_route
      3) Legacy fallback (support/ticket):
          - missing urgency -> REQUEST_MORE_INFO
          - otherwise -> CREATE_DRAFT_TICKET
    """
    config = _load_config()
    decision_id = str(uuid.uuid4())
    text = _get_text(event).lower()

    # Rule 1: Security keywords -> ESCALATE_HUMAN
    security_keywords = config.get("security_keywords", ["password", "credential", "security", "breach"])
    if any(k in text for k in security_keywords):
        return Decision(
            decision_id=decision_id,
            event_id=event.event_id,
            route="ESCALATE_HUMAN",
            reason="Security-related keyword detected",
            risk_level="high",
            proposed_action={},
            error_code="SECURITY_KEYWORD_DETECTED",
        )

    # Rule 2: Source policies override
    source_policies: Dict[str, Any] = config.get("source_policies", {})
    policy: Optional[Dict[str, Any]] = source_policies.get(event.source)

    if policy:
        req_fields = policy.get("required_fields", [])
        missing = _missing_fields(event, req_fields)

        if missing:
            question = policy.get("clarification_question") or "Missing required fields."
            return Decision(
                decision_id=decision_id,
                event_id=event.event_id,
                route="REQUEST_MORE_INFO",
                reason=f"Missing required field(s): {', '.join(missing)}",
                risk_level="medium",
                proposed_action={
                    "question": question,
                    "missing_fields": missing,
                },
                error_code="MISSING_REQUIRED_FIELD",
                missing_fields=missing,
                next_steps=question,
            )

        default_route = policy.get("default_route", config.get("default_route", "REQUEST_MORE_INFO"))
        return Decision(
            decision_id=decision_id,
            event_id=event.event_id,
            route=default_route,
            reason=f"Routed via source policy: {event.source}",
            risk_level="low",
            proposed_action={},
        )

    # Rule 3: Legacy fallback (support request style)
    urgency = None
    if isinstance(event.payload, dict):
        urgency = event.payload.get("urgency")

    if not urgency:
        question = config.get("clarification_question", "How urgent is this? (low / medium / high)")
        return Decision(
            decision_id=decision_id,
            event_id=event.event_id,
            route="REQUEST_MORE_INFO",
            reason="Missing required field: urgency",
            risk_level="medium",
            proposed_action={
                "question": question,
                "missing_fields": ["urgency"],
            },
            error_code="MISSING_URGENCY",
            missing_fields=["urgency"],
            next_steps=question,
        )

    # Default -> CREATE_DRAFT_TICKET
    summary = "Support request"
    if text:
        summary = text[:80]

    return Decision(
        decision_id=decision_id,
        event_id=event.event_id,
        route=config.get("default_route", "CREATE_DRAFT_TICKET"),
        reason="Standard support request",
        risk_level="low",
        proposed_action={
            "type": "create_ticket_draft",
            "queue": config.get("default_queue", "IT"),
            "priority": str(urgency).lower(),
            "summary": summary,
            "description": (event.payload if isinstance(event.payload, dict) else {"text": text}),
        },
    )
