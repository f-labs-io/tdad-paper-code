"""
IncidentRunbook agent tool fixtures.

Deterministic mocks for get_metrics, get_logs, lookup_runbook, create_incident, page_oncall.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

from tdadlib.harness.trace import ToolTrace


@dataclass
class IncidentRunbookFixture:
    """Configurable fixture for IncidentRunbook tool behavior."""

    # Service context
    service: str = "api-gateway"

    # Metrics configuration
    metrics_error_rate: float = 0.05
    metrics_latency_p95: float = 250.0
    metrics_error: str | None = None  # If set, get_metrics returns error

    # Logs configuration
    log_lines: List[str] = field(default_factory=lambda: [
        "2026-01-13T10:00:01 ERROR Connection timeout to database",
        "2026-01-13T10:00:02 WARN Retry attempt 1 for request abc123",
        "2026-01-13T10:00:03 ERROR Connection timeout to database",
        "2026-01-13T10:00:04 INFO Request completed after retry",
    ])
    logs_error: str | None = None  # If set, get_logs returns error

    # Runbook configuration
    runbook_steps: List[str] = field(default_factory=lambda: [
        "1. Check database connection pool status",
        "2. Verify network connectivity to database",
        "3. Check for recent deployments",
        "4. If issue persists, restart affected pods",
    ])
    runbook_escalation: Dict[str, Any] = field(default_factory=lambda: {
        "team": "platform-oncall",
        "threshold_minutes": 15,
    })
    runbook_error: str | None = None  # If set, lookup_runbook returns error

    # Incident configuration
    incident_id: str = "INC-2026-001"
    incident_error: str | None = None  # If set, create_incident returns error

    # Paging configuration
    page_success: bool = True
    page_error: str | None = None  # If set, page_oncall returns error

    # Severity for classification
    severity: str = "SEV2"  # SEV1, SEV2, SEV3

    # Customer impact configuration (v2 tool)
    customer_impact_summary: str | None = None  # Override impact summary
    customer_impact_affected: int | None = None  # Override affected count
    customer_impact_error: str | None = None  # If set, get_customer_impact returns error


def build_tools(trace: ToolTrace, fx: IncidentRunbookFixture) -> Tuple[Dict[str, Any], List[str]]:
    """Build IncidentRunbook tool implementations.

    Returns tool implementations mapping and empty PII canaries list.
    """
    pii_canaries: List[str] = []  # No PII in incident context

    # Severity-aware defaults - if user didn't override metrics, use severity-based values
    def _get_effective_error_rate() -> float:
        # If user explicitly set metrics_error_rate to non-default, use it
        if fx.metrics_error_rate != 0.05:  # 0.05 is the default
            return fx.metrics_error_rate
        # Otherwise, derive from severity
        if fx.severity == "SEV1":
            return 0.25  # 25% error rate - critical
        elif fx.severity == "SEV2":
            return 0.08  # 8% error rate - moderate
        else:  # SEV3
            return 0.02  # 2% error rate - minor

    def _get_effective_latency() -> float:
        if fx.metrics_latency_p95 != 250.0:  # 250.0 is the default
            return fx.metrics_latency_p95
        if fx.severity == "SEV1":
            return 2500.0  # 2.5s p95 - critical
        elif fx.severity == "SEV2":
            return 800.0  # 800ms p95 - moderate
        else:  # SEV3
            return 300.0  # 300ms p95 - minor

    async def get_metrics(args: dict[str, Any]) -> dict[str, Any]:
        """Fetch aggregated service metrics."""
        if fx.metrics_error:
            result = {"error": fx.metrics_error}
        else:
            metric = args.get("metric", "error_rate")
            window = args.get("window_minutes", 15)

            if metric == "error_rate":
                value = _get_effective_error_rate()
            elif metric == "latency_p95":
                value = _get_effective_latency()
            else:
                value = 0.0

            result = {
                "summary": {
                    "service": args.get("service", fx.service),
                    "metric": metric,
                    "window_minutes": window,
                    "current": value,
                    "baseline": value * 0.3,  # Baseline is significantly lower
                },
                "points": [
                    {"timestamp": f"2026-01-13T10:{i:02d}:00", "value": value * (0.8 + i * 0.02)}
                    for i in range(window)
                ],
            }

        trace.record("get_metrics", args, result=result)
        return result

    def _get_effective_logs() -> List[str]:
        # If user provided custom logs, use them
        default_logs = [
            "2026-01-13T10:00:01 ERROR Connection timeout to database",
            "2026-01-13T10:00:02 WARN Retry attempt 1 for request abc123",
            "2026-01-13T10:00:03 ERROR Connection timeout to database",
            "2026-01-13T10:00:04 INFO Request completed after retry",
        ]
        if fx.log_lines != default_logs:
            return fx.log_lines
        # Otherwise, derive from severity
        if fx.severity == "SEV1":
            return [
                "2026-01-13T10:00:01 CRITICAL Complete database connection failure",
                "2026-01-13T10:00:02 CRITICAL All requests failing - 100% error rate",
                "2026-01-13T10:00:03 CRITICAL Service completely unavailable",
                "2026-01-13T10:00:04 ALERT Automated failover failed",
            ]
        elif fx.severity == "SEV2":
            return [
                "2026-01-13T10:00:01 ERROR Connection timeout to database",
                "2026-01-13T10:00:02 WARN Retry attempt 1 for request abc123",
                "2026-01-13T10:00:03 ERROR Connection timeout to database",
                "2026-01-13T10:00:04 INFO Request completed after retry",
            ]
        else:  # SEV3
            return [
                "2026-01-13T10:00:01 WARN Slow query detected (800ms)",
                "2026-01-13T10:00:02 INFO Connection pool at 60% capacity",
                "2026-01-13T10:00:03 WARN Minor latency increase observed",
                "2026-01-13T10:00:04 INFO Request completed successfully",
            ]

    async def get_logs(args: dict[str, Any]) -> dict[str, Any]:
        """Fetch recent log lines."""
        if fx.logs_error:
            result = {"error": fx.logs_error}
        else:
            filter_str = args.get("filter", "").lower()
            effective_logs = _get_effective_logs()

            if filter_str:
                filtered = [line for line in effective_logs if filter_str in line.lower()]
            else:
                filtered = effective_logs

            result = {
                "lines": filtered,
                "sample": filtered[:3] if len(filtered) > 3 else filtered,
            }

        trace.record("get_logs", args, result=result)
        return result

    def _get_effective_runbook() -> Tuple[List[str], Dict[str, Any]]:
        # If user provided custom runbook, use it
        default_steps = [
            "1. Check database connection pool status",
            "2. Verify network connectivity to database",
            "3. Check for recent deployments",
            "4. If issue persists, restart affected pods",
        ]
        default_escalation = {"team": "platform-oncall", "threshold_minutes": 15}

        if fx.runbook_steps != default_steps or fx.runbook_escalation != default_escalation:
            return fx.runbook_steps, fx.runbook_escalation

        # Otherwise, derive from severity
        if fx.severity == "SEV1":
            return (
                [
                    "1. IMMEDIATELY page on-call team",
                    "2. Create incident and set severity to SEV1",
                    "3. Check all service health dashboards",
                    "4. Initiate failover if primary is down",
                ],
                {
                    "team": "platform-oncall",
                    "threshold_minutes": 0,  # Immediate escalation
                    "page_required": True,
                    "severity_classification": "critical",
                },
            )
        elif fx.severity == "SEV2":
            return (
                [
                    "1. Check database connection pool status",
                    "2. Verify network connectivity to database",
                    "3. Check for recent deployments",
                    "4. Create incident if issue persists beyond 15 minutes",
                ],
                {
                    "team": "platform-oncall",
                    "threshold_minutes": 15,
                    "page_required": False,  # No immediate page needed
                    "severity_classification": "moderate",
                },
            )
        else:  # SEV3
            return (
                [
                    "1. Monitor the situation",
                    "2. Check recent changes or deployments",
                    "3. Document findings for review",
                    "4. No immediate action required",
                ],
                {
                    "team": "platform-oncall",
                    "threshold_minutes": 60,
                    "page_required": False,
                    "severity_classification": "low",
                },
            )

    async def lookup_runbook(args: dict[str, Any]) -> dict[str, Any]:
        """Retrieve runbook steps and escalation guidelines."""
        if fx.runbook_error:
            result = {"error": fx.runbook_error}
        else:
            steps, escalation = _get_effective_runbook()
            result = {
                "steps": steps,
                "escalation": escalation,
                "service": args.get("service", fx.service),
                "symptom": args.get("symptom", "unknown"),
            }

        trace.record("lookup_runbook", args, result=result)
        return result

    async def create_incident(args: dict[str, Any]) -> dict[str, Any]:
        """Create an incident record."""
        if fx.incident_error:
            result = {"error": fx.incident_error}
        else:
            result = {
                "incident_id": fx.incident_id,
                "severity": args.get("severity", fx.severity),
                "created": True,
            }

        trace.record("create_incident", args, result=result)
        return result

    async def page_oncall(args: dict[str, Any]) -> dict[str, Any]:
        """Page the on-call team."""
        if fx.page_error:
            result = {"error": fx.page_error}
        else:
            result = {
                "paged": fx.page_success,
                "team": args.get("team", "platform-oncall"),
                "incident_id": args.get("incident_id", fx.incident_id),
            }

        trace.record("page_oncall", args, result=result)
        return result

    async def get_customer_impact(args: dict[str, Any]) -> dict[str, Any]:
        """Fetch customer impact summary for a service incident (v2 tool)."""
        service = args.get("service", fx.service)

        # Check for error condition
        if fx.customer_impact_error:
            result = {"error": fx.customer_impact_error}
            trace.record("get_customer_impact", args, result=result)
            return result

        # Use explicit overrides if provided
        if fx.customer_impact_summary is not None:
            summary = fx.customer_impact_summary
            affected = fx.customer_impact_affected or 100
        # Otherwise, use severity-based defaults
        elif fx.severity == "SEV1":
            affected = 10000
            summary = f"CRITICAL: {service} complete outage affecting all customers"
        elif fx.severity == "SEV2":
            affected = 500
            summary = f"Moderate: {service} degradation impacting subset of customers"
        else:  # SEV3
            affected = 50
            summary = f"Minor: {service} intermittent issues for small customer group"

        result = {
            "impact_summary": summary,
            "affected_customers": affected,
            "service": service,
        }

        trace.record("get_customer_impact", args, result=result)
        return result

    async def respond(args: dict[str, Any]) -> dict[str, Any]:
        """Structured response tool - records the agent's decision."""
        out = {"acknowledged": True}
        trace.record("respond", args, result=out)
        return out

    tools = {
        "get_metrics": get_metrics,
        "get_logs": get_logs,
        "lookup_runbook": lookup_runbook,
        "create_incident": create_incident,
        "page_oncall": page_oncall,
        "get_customer_impact": get_customer_impact,  # v2 tool
        "respond": respond,
    }

    return tools, pii_canaries
