"""Safe diagnostic vocabulary: never persist SDK messages, contracts or credentials."""

from ..models import utcnow


class SafeDiagnostic:
    """Only application-defined errors may supply printable diagnostic details."""


class BrokerPermissionError(PermissionError, SafeDiagnostic):
    operation = "reqCompletedOrdersAsync"
    code = 321
    reason = "Gateway Read-Only API rejected completed-order retrieval; reconciliation remains blocked"


def diagnostic(operation, error):
    safe = isinstance(error, SafeDiagnostic)
    return {
        "operation": getattr(error, "operation", operation) if safe else operation,
        "error_type": type(error).__name__,
        "code": getattr(error, "code", None) if safe else None,
        "reason": getattr(
            error if safe else None,
            "reason",
            {
                "TimeoutError": "Request timed out; verify Gateway connectivity and data subscriptions",
                "ConnectionError": "Gateway disconnected or account session unavailable",
                "PermissionError": "Operation denied; inspect sanitized broker API diagnostics",
            }.get(type(error).__name__, "Request failed validation or provider unavailable"),
        ),
        "timestamp": utcnow().isoformat(),
    }


def api_diagnostic(request_id, code, message):
    lower = message.lower()
    reason = None
    operation = "IBKR request"
    if code == 321 and "read-only" in lower:
        operation, reason = BrokerPermissionError.operation, BrokerPermissionError.reason
    elif code in (354, 10089, 10090, 10167, 10168, 10186) or (
        code in (162, 200) and any(k in lower for k in ("permission", "subscrib"))
    ):
        reason = "Market-data permission/subscription missing; delayed data is not eligible for ENTER"
    elif code == 162:
        reason = "Historical/news request rejected: unavailable data, pacing or entitlement restriction"
    elif code == 200:
        reason = "Contract qualification failed: no matching security definition"
    elif code in (1100, 1300, 502, 504):
        reason = "Gateway/broker connection unavailable"
    elif code == 326:
        reason = "API client ID already in use"
    if reason is None:
        return None
    return {
        "operation": operation,
        "request_id": request_id,
        "code": code,
        "reason": reason,
        "timestamp": utcnow().isoformat(),
    }
