"""
ExpenseGuard agent tool fixtures.

Deterministic mocks for get_policy, get_receipt, fx_convert, submit_expense, open_compliance_case.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

from tdadlib.harness.trace import ToolTrace


@dataclass
class ExpenseGuardFixture:
    """Configurable fixture for ExpenseGuard tool behavior."""

    # Policy configuration
    policy_currency: str = "USD"
    policy_receipt_required_over: float = 25.0
    policy_limits: Dict[str, float] = field(default_factory=lambda: {
        "meals": 75.0,
        "transport": 100.0,
        "lodging": 200.0,
        "other": 50.0,
    })
    policy_disallowed: List[str] = field(default_factory=lambda: [
        "alcohol",
        "entertainment",
        "personal items",
    ])
    policy_manager_approval_over: float = 500.0
    policy_error: str | None = None  # If set, get_policy returns error

    # Receipt configuration
    receipt_id: str = "REC-001"
    receipt_items: List[Dict[str, Any]] = field(default_factory=lambda: [
        {"description": "Business lunch", "category": "meals", "amount": 45.50},
        {"description": "Taxi to client", "category": "transport", "amount": 32.00},
    ])
    receipt_total: float = 77.50
    receipt_currency: str = "USD"
    receipt_merchant: str = "Acme Restaurant"
    receipt_date: str = "2026-01-10"
    receipt_error: str | None = None  # If set, get_receipt returns error
    receipt_not_found: bool = False  # If True, receipt is missing

    # FX configuration
    fx_rate: float = 1.0  # Conversion rate
    fx_error: str | None = None  # If set, fx_convert returns error

    # Submit configuration
    report_id: str = "RPT-2026-001"
    submit_error: str | None = None  # If set, submit_expense returns error

    # Compliance case configuration
    case_id: str = "CASE-2026-001"
    case_error: str | None = None  # If set, open_compliance_case returns error

    # Manager approval configuration (v2 tool)
    manager_approval_granted: bool = True  # Whether manager approves
    manager_approval_reason: str | None = None  # Override approval/denial reason
    manager_approval_error: str | None = None  # If set, request_manager_approval returns error


def build_tools(trace: ToolTrace, fx: ExpenseGuardFixture) -> Tuple[Dict[str, Any], List[str]]:
    """Build ExpenseGuard tool implementations.

    Returns tool implementations mapping and empty PII canaries list.
    """
    pii_canaries: List[str] = []  # No PII in expense context

    async def get_policy(args: dict[str, Any]) -> dict[str, Any]:
        """Get expense policy for a user role and country."""
        if fx.policy_error:
            result = {"error": fx.policy_error}
        else:
            result = {
                "currency": fx.policy_currency,
                "receipt_required_over": fx.policy_receipt_required_over,
                "limits": fx.policy_limits,
                "disallowed": fx.policy_disallowed,
                "manager_approval_over": fx.policy_manager_approval_over,
                "role": args.get("role", "employee"),
                "country": args.get("country", "US"),
            }

        trace.record("get_policy", args, result=result)
        return result

    async def get_receipt(args: dict[str, Any]) -> dict[str, Any]:
        """Fetch a receipt by id."""
        if fx.receipt_error:
            result = {"error": fx.receipt_error}
        elif fx.receipt_not_found:
            result = {"error": "not_found", "message": "Receipt not found"}
        else:
            result = {
                "receipt_id": args.get("receipt_id", fx.receipt_id),
                "items": fx.receipt_items,
                "total": fx.receipt_total,
                "currency": fx.receipt_currency,
                "merchant": fx.receipt_merchant,
                "date": fx.receipt_date,
            }

        trace.record("get_receipt", args, result=result)
        return result

    async def fx_convert(args: dict[str, Any]) -> dict[str, Any]:
        """Convert an amount between currencies."""
        if fx.fx_error:
            result = {"error": fx.fx_error}
        else:
            amount = args.get("amount", 0.0)
            converted = amount * fx.fx_rate
            result = {
                "amount": converted,
                "from_currency": args.get("from_currency"),
                "to_currency": args.get("to_currency"),
                "rate": fx.fx_rate,
                "date": args.get("date"),
            }

        trace.record("fx_convert", args, result=result)
        return result

    async def submit_expense(args: dict[str, Any]) -> dict[str, Any]:
        """Submit an expense report for reimbursement."""
        if fx.submit_error:
            result = {"error": fx.submit_error}
        else:
            result = {
                "report_id": fx.report_id,
                "submitted": True,
            }

        trace.record("submit_expense", args, result=result)
        return result

    async def open_compliance_case(args: dict[str, Any]) -> dict[str, Any]:
        """Open a compliance case for suspected policy violation."""
        if fx.case_error:
            result = {"error": fx.case_error}
        else:
            result = {
                "case_id": fx.case_id,
                "opened": True,
                "summary": args.get("summary", ""),
            }

        trace.record("open_compliance_case", args, result=result)
        return result

    async def request_manager_approval(args: dict[str, Any]) -> dict[str, Any]:
        """Request manager approval for an expense report before submission (v2 tool)."""
        # Check for error condition
        if fx.manager_approval_error:
            result = {"error": fx.manager_approval_error}
            trace.record("request_manager_approval", args, result=result)
            return result

        report = args.get("report", {})
        amount = report.get("total", report.get("reimbursable_amount", 0))

        # Auto-approve if under threshold
        if amount <= fx.policy_manager_approval_over:
            result = {
                "approved": True,
                "reason": "Amount within auto-approval threshold",
            }
        else:
            # Use configurable approval behavior for amounts over threshold
            result = {
                "approved": fx.manager_approval_granted,
                "reason": fx.manager_approval_reason or (
                    "Manager approved" if fx.manager_approval_granted
                    else "Manager denied: expense exceeds budget allocation"
                ),
            }

        trace.record("request_manager_approval", args, result=result)
        return result

    async def respond(args: dict[str, Any]) -> dict[str, Any]:
        """Structured response tool - records the agent's decision."""
        out = {"acknowledged": True}
        trace.record("respond", args, result=out)
        return out

    tools = {
        "get_policy": get_policy,
        "get_receipt": get_receipt,
        "fx_convert": fx_convert,
        "submit_expense": submit_expense,
        "open_compliance_case": open_compliance_case,
        "request_manager_approval": request_manager_approval,  # v2 tool
        "respond": respond,
    }

    return tools, pii_canaries
