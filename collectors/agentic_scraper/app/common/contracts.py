"""Contract models for SquadOne request and response envelopes."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .errors import CanonicalError, invalid_request, output_schema_mismatch
from .idempotency import build_idempotency_key, validate_idempotency_key

PROTOCOL_VERSION = "1.0.0"
COMPLETED = "completed"
PARTIAL = "partial"
FAILED = "failed"
REQUIRED_ENVELOPE_FIELDS = ("status", "result", "evidence", "warnings", "error", "timing", "meta")
REQUIRED_EVIDENCE_FIELDS = ("source", "summary")
SUPPORTED_EVIDENCE_TYPES = frozenset({"string", "url", "number", "boolean", "array", "object"})


@dataclass(slots=True)
class ToolDirective:
    """SquadOne tool invocation directive as delivered to MCP servers."""

    protocol_version: str
    request_id: str
    trace_id: str
    task_id: str
    attempt: int
    idempotency_key: str
    tool_name: str
    tool_input: dict[str, Any]
    schema_ref: str = ""
    input_schema_ref: str = ""
    evidence_required: list[str] = field(default_factory=list)
    evidence_schema: dict[str, Any] = field(default_factory=dict)
    expected_output_schema: dict[str, Any] = field(default_factory=dict)
    fallback_hint: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def fallback_tool(self) -> str | None:
        return self.fallback_hint or None

    @property
    def metadata(self) -> dict[str, Any]:
        return self.meta

    @property
    def provider_override(self) -> str | None:
        input_provider = self.tool_input.get("provider")
        if input_provider is not None:
            return _optional_non_empty_str(input_provider, "input.provider")
        meta_provider = self.meta.get("provider")
        if meta_provider is not None:
            return _optional_non_empty_str(meta_provider, "meta.provider")
        return None


@dataclass(slots=True)
class ResponseError:
    """Canonical error payload."""

    code: str
    message: str
    origin: str = "mcp_server"
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ResponseEnvelope:
    """Response envelope required by SquadOne_AI contract."""

    status: str
    result: dict[str, Any] | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: ResponseError | None = None
    timing: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_tool_directive(
    payload: dict[str, Any],
    *,
    expected_tool_name: str | None = None,
    expected_protocol_version: str = PROTOCOL_VERSION,
) -> ToolDirective:
    """Validate and normalize a SquadOne invoke envelope."""
    if not isinstance(payload, dict):
        raise invalid_request("request payload must be a JSON object")

    required_fields = ("protocol_version", "trace_id", "task_id", "attempt", "idempotency_key", "tool_name")
    missing = [field_name for field_name in required_fields if field_name not in payload]
    if "input" not in payload and "tool_input" not in payload:
        missing.append("input")
    if missing:
        raise invalid_request(f"Missing required fields: {', '.join(missing)}")

    protocol_version = _non_empty_str(payload.get("protocol_version"), "protocol_version")
    if protocol_version != expected_protocol_version:
        raise invalid_request(
            f"Unsupported protocol_version: {protocol_version!r}; expected {expected_protocol_version!r}"
        )

    tool_name = _non_empty_str(payload.get("tool_name"), "tool_name")
    if expected_tool_name and tool_name != expected_tool_name:
        raise invalid_request(f"Unsupported tool_name: {tool_name!r}; expected {expected_tool_name!r}")

    attempt = _positive_int(payload.get("attempt"), "attempt")
    tool_input = payload.get("input", payload.get("tool_input"))
    if not isinstance(tool_input, dict):
        raise invalid_request("input must be an object")

    directive = ToolDirective(
        protocol_version=protocol_version,
        request_id=str(payload.get("request_id") or ""),
        trace_id=_non_empty_str(payload.get("trace_id"), "trace_id"),
        task_id=_non_empty_str(payload.get("task_id"), "task_id"),
        attempt=attempt,
        idempotency_key=_non_empty_str(payload.get("idempotency_key"), "idempotency_key"),
        tool_name=tool_name,
        tool_input=dict(tool_input),
        schema_ref=str(payload.get("schema_ref") or ""),
        input_schema_ref=str(payload.get("input_schema_ref") or ""),
        evidence_required=_string_list(payload.get("evidence_required"), "evidence_required"),
        evidence_schema=_evidence_schema_or_empty(payload),
        expected_output_schema=_dict_or_empty(payload.get("expected_output_schema"), "expected_output_schema"),
        fallback_hint=str(payload.get("fallback_hint") or payload.get("fallback_tool") or ""),
        meta=_dict_or_empty(payload.get("meta", payload.get("metadata", {})), "meta"),
    )
    validate_idempotency_key(directive, directive.idempotency_key)
    return directive


def build_success_envelope(
    *,
    result: dict[str, Any],
    evidence: list[dict[str, Any]],
    status: str = COMPLETED,
    warnings: list[str] | None = None,
    timing: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> ResponseEnvelope:
    if status not in {COMPLETED, PARTIAL}:
        raise output_schema_mismatch(f"success status must be completed or partial, got {status!r}")
    validate_evidence(evidence)
    return ResponseEnvelope(
        status=status,
        result=result,
        evidence=evidence,
        warnings=warnings or [],
        error=None,
        timing=timing or {},
        meta=meta or {},
    )


def build_error_envelope(
    *,
    code: str,
    message: str,
    retryable: bool = False,
    origin: str = "mcp_server",
    details: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    timing: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> ResponseEnvelope:
    return ResponseEnvelope(
        status=FAILED,
        result={},
        evidence=[],
        warnings=warnings or [],
        error=ResponseError(
            code=code,
            message=message,
            origin=origin,
            retryable=retryable,
            details=details or {},
        ),
        timing=timing or {},
        meta=meta or {},
    )


def build_error_envelope_from_exception(
    exc: CanonicalError,
    *,
    timing: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> ResponseEnvelope:
    return build_error_envelope(
        code=exc.code,
        message=exc.message,
        retryable=exc.retryable,
        origin=exc.origin,
        details=exc.details,
        timing=timing,
        meta=meta,
    )


def attach_contract_meta(
    envelope: ResponseEnvelope,
    directive: ToolDirective | None,
    *,
    protocol_version: str = PROTOCOL_VERSION,
) -> ResponseEnvelope:
    envelope.meta.setdefault("protocol_version", protocol_version)
    if directive is not None:
        envelope.meta.setdefault("request_id", directive.request_id)
        envelope.meta.setdefault("trace_id", directive.trace_id)
        envelope.meta.setdefault("task_id", directive.task_id)
        envelope.meta.setdefault("tool_name", directive.tool_name)
        envelope.meta.setdefault("idempotency_key", build_idempotency_key(directive))
        if directive.fallback_tool:
            envelope.meta.setdefault("fallback_tool", directive.fallback_tool)
    return envelope


def validate_response_envelope(envelope: ResponseEnvelope) -> None:
    payload = envelope.to_dict()
    missing = [field_name for field_name in REQUIRED_ENVELOPE_FIELDS if field_name not in payload]
    if missing:
        raise output_schema_mismatch(f"response envelope missing fields: {', '.join(missing)}")
    if envelope.status not in {COMPLETED, PARTIAL, FAILED}:
        raise output_schema_mismatch(f"invalid response status: {envelope.status!r}")
    if envelope.status != FAILED:
        validate_evidence(envelope.evidence)


def validate_evidence(evidence: list[dict[str, Any]]) -> None:
    if not isinstance(evidence, list) or not evidence:
        raise output_schema_mismatch("evidence must be a non-empty list")
    for index, item in enumerate(evidence):
        if not isinstance(item, dict):
            raise output_schema_mismatch(f"evidence[{index}] must be an object")
        missing = [
            field_name
            for field_name in REQUIRED_EVIDENCE_FIELDS
            if not str(item.get(field_name) or "").strip()
        ]
        if missing:
            raise output_schema_mismatch(
                f"evidence[{index}] missing required fields: {', '.join(missing)}"
            )


def _non_empty_str(value: Any, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise invalid_request(f"{field_name} must be a non-empty string")
    return normalized


def _positive_int(value: Any, field_name: str) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise invalid_request(f"{field_name} must be an integer") from exc
    if normalized < 1:
        raise invalid_request(f"{field_name} must be >= 1")
    return normalized


def _dict_or_empty(value: Any, field_name: str) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise invalid_request(f"{field_name} must be an object")
    return dict(value)


def _string_list(value: Any, field_name: str) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise invalid_request(f"{field_name} must be a list of strings")
    return list(value)


def _optional_non_empty_str(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise invalid_request(f"{field_name} must be a string")
    normalized = value.strip()
    return normalized or None


def _evidence_schema_or_empty(payload: dict[str, Any]) -> dict[str, Any]:
    direct_schema = payload.get("evidence_schema")
    if direct_schema is None:
        direct_schema = payload.get("evidence_contract")
    if direct_schema is None:
        meta = _dict_or_empty(payload.get("meta", payload.get("metadata", {})), "meta")
        direct_schema = meta.get("evidence_schema")
    if direct_schema in (None, ""):
        return {}
    if not isinstance(direct_schema, dict):
        raise invalid_request("evidence_schema must be an object")

    normalized: dict[str, Any] = {}
    for key, value in direct_schema.items():
        normalized_key = str(key).strip()
        if not normalized_key:
            raise invalid_request("evidence_schema contains an empty field name")
        if not isinstance(value, dict):
            raise invalid_request(f"evidence_schema.{normalized_key} must be an object")
        item = dict(value)
        evidence_type = item.get("type")
        if evidence_type is not None:
            normalized_type = str(evidence_type).strip().lower()
            if normalized_type not in SUPPORTED_EVIDENCE_TYPES:
                supported = ", ".join(sorted(SUPPORTED_EVIDENCE_TYPES))
                raise invalid_request(
                    f"evidence_schema.{normalized_key}.type must be one of: {supported}"
                )
            item["type"] = normalized_type
        normalized[normalized_key] = item
    return normalized
