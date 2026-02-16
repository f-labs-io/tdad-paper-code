"""
DataInsights agent tool fixtures.

Deterministic mocks for describe_schema and run_sql tools.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

from tdadlib.harness.trace import ToolTrace


# Default schema for the analytics database
DEFAULT_SCHEMA = {
    "tables": {
        "orders": {
            "columns": [
                {"name": "order_id", "type": "INTEGER", "primary_key": True},
                {"name": "customer_id", "type": "INTEGER"},
                {"name": "order_date", "type": "DATE"},
                {"name": "total_amount", "type": "DECIMAL(10,2)"},
                {"name": "status", "type": "VARCHAR(50)"},
            ]
        },
        "customers": {
            "columns": [
                {"name": "customer_id", "type": "INTEGER", "primary_key": True},
                {"name": "name", "type": "VARCHAR(100)"},
                {"name": "email", "type": "VARCHAR(100)"},
                {"name": "created_at", "type": "DATE"},
                {"name": "region", "type": "VARCHAR(50)"},
            ]
        },
        "products": {
            "columns": [
                {"name": "product_id", "type": "INTEGER", "primary_key": True},
                {"name": "name", "type": "VARCHAR(100)"},
                {"name": "category", "type": "VARCHAR(50)"},
                {"name": "price", "type": "DECIMAL(10,2)"},
                {"name": "stock_quantity", "type": "INTEGER"},
            ]
        },
        "order_items": {
            "columns": [
                {"name": "item_id", "type": "INTEGER", "primary_key": True},
                {"name": "order_id", "type": "INTEGER"},
                {"name": "product_id", "type": "INTEGER"},
                {"name": "quantity", "type": "INTEGER"},
                {"name": "unit_price", "type": "DECIMAL(10,2)"},
            ]
        },
    }
}


@dataclass
class DataInsightsFixture:
    """Configurable fixture for DataInsights tool behavior."""

    # Schema configuration
    schema: Dict[str, Any] = field(default_factory=lambda: DEFAULT_SCHEMA)
    schema_error: str | None = None  # If set, describe_schema returns error

    # SQL query results - can be customized per test
    sql_results: Dict[str, Any] | None = None  # Override default results
    sql_error: str | None = None  # If set, run_sql returns error
    sql_row_count: int = 5  # Default number of rows to return

    # SQL retry behavior (for P4_SQL_ERROR_RECOVERY tests)
    sql_error_first: bool = False  # First run_sql fails, retry succeeds
    sql_always_fails: bool = False  # All run_sql calls fail (even after retry)

    # Cost estimation configuration (for v2 P5_COST_GUARD tests)
    cost_ok: bool = True  # Whether estimate_cost returns ok=True
    cost_ok_first: bool = True  # First estimate_cost ok, subsequent may differ
    cost_ok_after_rewrite: bool = True  # After query rewrite, cost becomes acceptable
    estimated_cost: float = 5.0  # The cost value to return
    estimated_cost_high: float = 150.0  # Cost when query is expensive

    # Configurable query results for different query patterns
    revenue_total: float = 125000.50
    customer_count: int = 1234
    top_products: List[Dict[str, Any]] = field(default_factory=lambda: [
        {"product_id": 1, "name": "Widget Pro", "total_sold": 500},
        {"product_id": 2, "name": "Gadget Plus", "total_sold": 350},
        {"product_id": 3, "name": "Super Tool", "total_sold": 275},
    ])
    top_customers: List[Dict[str, Any]] = field(default_factory=lambda: [
        {"customer_id": 101, "name": "Acme Corp", "total_orders": 50},
        {"customer_id": 102, "name": "Tech Inc", "total_orders": 42},
        {"customer_id": 103, "name": "Data LLC", "total_orders": 35},
    ])

    # Internal state tracking (managed by fixture, not set by tests)
    _sql_call_count: int = field(default=0, repr=False)
    _cost_call_count: int = field(default=0, repr=False)


def build_tools(trace: ToolTrace, fx: DataInsightsFixture) -> Tuple[Dict[str, Any], List[str]]:
    """Build DataInsights tool implementations.

    Returns tool implementations mapping and empty PII canaries list
    (DataInsights doesn't handle PII).
    """
    pii_canaries: List[str] = []  # No PII in analytics context

    async def describe_schema(args: dict[str, Any]) -> dict[str, Any]:
        """Return the database schema."""
        if fx.schema_error:
            result = {"error": fx.schema_error}
        else:
            result = {"tables": fx.schema["tables"]}

        trace.record("describe_schema", args, result=result)
        return result

    async def run_sql(args: dict[str, Any]) -> dict[str, Any]:
        """Execute a SQL query and return results."""
        query = args.get("query", "").upper()
        fx._sql_call_count += 1

        # Check for error conditions
        # 1. Always fails mode (even after retry)
        if fx.sql_always_fails:
            result = {
                "columns": [],
                "rows": [],
                "row_count": 0,
                "error": fx.sql_error or "Query execution failed: persistent error",
            }
            trace.record("run_sql", args, result=result)
            return result

        # 2. First call fails, retry succeeds
        if fx.sql_error_first and fx._sql_call_count == 1:
            result = {
                "columns": [],
                "rows": [],
                "row_count": 0,
                "error": fx.sql_error or "Query execution failed: timeout",
            }
            trace.record("run_sql", args, result=result)
            return result

        # 3. Explicit error set (legacy behavior)
        if fx.sql_error and not fx.sql_error_first:
            result = {
                "columns": [],
                "rows": [],
                "row_count": 0,
                "error": fx.sql_error,
            }
            trace.record("run_sql", args, result=result)
            return result

        # If custom results provided, use them
        if fx.sql_results is not None:
            trace.record("run_sql", args, result=fx.sql_results)
            return fx.sql_results

        # CRITICAL: If sql_row_count is explicitly set to 0, return empty results
        # This must be checked BEFORE pattern matching to allow tests to simulate empty results
        if fx.sql_row_count == 0:
            result = {
                "columns": ["result"],
                "rows": [],
                "row_count": 0,
            }
            trace.record("run_sql", args, result=result)
            return result

        # Generate appropriate mock results based on query pattern
        if "COUNT" in query and "CUSTOMER" in query:
            result = {
                "columns": ["customer_count"],
                "rows": [[fx.customer_count]],
                "row_count": 1,
            }
        elif "SUM" in query or "REVENUE" in query or "TOTAL" in query:
            result = {
                "columns": ["total_revenue"],
                "rows": [[fx.revenue_total]],
                "row_count": 1,
            }
        elif "TOP" in query or "LIMIT" in query:
            if "PRODUCT" in query:
                result = {
                    "columns": ["product_id", "name", "total_sold"],
                    "rows": [[p["product_id"], p["name"], p["total_sold"]] for p in fx.top_products],
                    "row_count": len(fx.top_products),
                }
            elif "CUSTOMER" in query:
                result = {
                    "columns": ["customer_id", "name", "total_orders"],
                    "rows": [[c["customer_id"], c["name"], c["total_orders"]] for c in fx.top_customers],
                    "row_count": len(fx.top_customers),
                }
            else:
                # Generic top-k result
                result = {
                    "columns": ["id", "value"],
                    "rows": [[i, i * 100] for i in range(1, fx.sql_row_count + 1)],
                    "row_count": fx.sql_row_count,
                }
        elif "SELECT" in query:
            # Generic SELECT result
            result = {
                "columns": ["col1", "col2", "col3"],
                "rows": [[f"val{i}", i, i * 10.5] for i in range(1, fx.sql_row_count + 1)],
                "row_count": fx.sql_row_count,
            }
        else:
            # Default result
            result = {
                "columns": ["result"],
                "rows": [["OK"]],
                "row_count": 1,
            }

        trace.record("run_sql", args, result=result)
        return result

    async def estimate_cost(args: dict[str, Any]) -> dict[str, Any]:
        """Estimate the runtime/cost of a SQL query before execution (v2 tool)."""
        query = args.get("query", "").upper()
        fx._cost_call_count += 1

        # Fixture-controlled behavior for testing different cost scenarios
        # 1. If cost_ok=False, always return expensive
        if not fx.cost_ok:
            result = {
                "ok": False,
                "reason": "Query exceeds cost budget.",
                "estimated_cost": fx.estimated_cost_high,
            }
            trace.record("estimate_cost", args, result=result)
            return result

        # 2. First call expensive, subsequent calls (after rewrite) may be ok
        if not fx.cost_ok_first and fx._cost_call_count == 1:
            result = {
                "ok": False,
                "reason": "Query may scan too many rows. Add LIMIT or narrow columns.",
                "estimated_cost": fx.estimated_cost_high,
            }
            trace.record("estimate_cost", args, result=result)
            return result

        # 3. After rewrite behavior (second call onwards)
        if fx._cost_call_count > 1 and fx.cost_ok_after_rewrite:
            result = {
                "ok": True,
                "reason": "",
                "estimated_cost": fx.estimated_cost,
            }
            trace.record("estimate_cost", args, result=result)
            return result

        # 4. Default: heuristic-based (queries with * or no LIMIT are expensive)
        is_expensive = "SELECT *" in query or ("LIMIT" not in query and "COUNT" not in query)

        if is_expensive:
            result = {
                "ok": False,
                "reason": "Query may scan too many rows. Add LIMIT or narrow columns.",
                "estimated_cost": fx.estimated_cost_high,
            }
        else:
            result = {
                "ok": True,
                "reason": "",
                "estimated_cost": fx.estimated_cost,
            }

        trace.record("estimate_cost", args, result=result)
        return result

    async def respond(args: dict[str, Any]) -> dict[str, Any]:
        """Structured response tool - records the agent's decision."""
        out = {"acknowledged": True}
        trace.record("respond", args, result=out)
        return out

    tools = {
        "describe_schema": describe_schema,
        "run_sql": run_sql,
        "estimate_cost": estimate_cost,  # v2 tool
        "respond": respond,
    }

    return tools, pii_canaries
