"""Pre-execution checks that keep log-table queries narrow and cheap.

The guard parses the query with sqlglot and applies rules only when the query reads a
configured log table (default `otel_logs`):

- exactly one SELECT statement (DESCRIBE, EXPLAIN, SHOW and EXISTS pass through)
- no `SELECT *` on the log table
- a lower bound on a time column, and a window no longer than the configured maximum
  (a longer window is allowed when the query pins an exact TraceId)
- an outer LIMIT no larger than the configured maximum

This is an accident guard for AI agents, not a security boundary. The ClickHouse user
profile and quota for the MCP user are the hard limit.
"""

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import sqlglot
from sqlglot import exp

from mcp_clickhouse.support_guard.config import GuardConfig

# sqlglot logs a warning for every statement it falls back to parsing as a Command.
logging.getLogger("sqlglot").setLevel(logging.ERROR)

_PASSTHROUGH_RE = re.compile(r"^\s*(DESCRIBE|DESC|EXPLAIN|SHOW|EXISTS)\b", re.IGNORECASE)
_TABLE_FUNCTIONS = {"merge", "remote", "remotesecure", "cluster", "clusterallreplicas"}
_NOW_FUNCTIONS = {"now", "now64", "nowinblock", "current_timestamp", "localtimestamp"}
_UNIT_SECONDS = {
    "SECOND": 1,
    "MINUTE": 60,
    "HOUR": 3600,
    "DAY": 86400,
    "WEEK": 604800,
}
_SHIFT_FUNCTION_RE = re.compile(r"^(add|subtract)(second|minute|hour|day|week)s$", re.IGNORECASE)
_INTERVAL_FUNCTION_RE = re.compile(r"^tointerval(second|minute|hour|day|week)$", re.IGNORECASE)
_FRACTION_RE = re.compile(r"(\.\d{6})\d+")

_COLUMN_HINT = (
    "Select explicit columns such as Timestamp, ServiceName, SeverityText, Body, "
    "TraceId, LogAttributes['<key>']."
)
_TIME_HINT = (
    "Use literal timestamps, for example "
    "Timestamp >= toDateTime64('2026-11-06 10:00:00', 9, 'Asia/Kolkata') AND "
    "Timestamp < toDateTime64('2026-11-06 10:30:00', 9, 'Asia/Kolkata'), "
    "or a relative window such as Timestamp >= now() - INTERVAL 30 MINUTE."
)


class QueryRejected(Exception):
    """Raised when a log query breaks a guard rule. The message is shown to the agent."""


@dataclass(frozen=True)
class GuardDecision:
    """Outcome of inspecting one query."""

    touches_log_table: bool
    window_minutes: Optional[float] = None


@dataclass
class _Bounds:
    lower: Optional[datetime] = None
    upper: Optional[datetime] = None
    pins_trace: bool = False
    unparsed: Optional[str] = None


def inspect_query(
    query: str,
    params: Optional[dict[str, Any]],
    config: GuardConfig,
    now: Optional[datetime] = None,
) -> GuardDecision:
    """Validate a query against the log guard rules.

    @param query - SQL sent to run_query
    @param params - bound {name:Type} parameter values, if any
    @param config - guard limits
    @param now - clock override for tests
    @returns the guard decision; raises QueryRejected when a rule is broken
    """
    if not _mentions_log_table(query, config):
        return GuardDecision(touches_log_table=False)
    if _PASSTHROUGH_RE.match(query):
        return GuardDecision(touches_log_table=True)

    statement = _parse_single_statement(query)
    guarded_selects = _guarded_selects(statement, config)
    if not guarded_selects:
        return GuardDecision(touches_log_table=False)

    current = now or datetime.now(timezone.utc)
    widest = 0.0
    for select in guarded_selects:
        _reject_star(select)
        widest = max(widest, _check_window(select, params or {}, config, current))
    _check_limit(statement, config)
    return GuardDecision(touches_log_table=True, window_minutes=widest)


def _mentions_log_table(query: str, config: GuardConfig) -> bool:
    lowered = query.lower()
    return any(re.search(rf"\b{re.escape(table)}\b", lowered) for table in config.log_tables) or (
        any(re.search(rf"\b{name}\s*\(", lowered) for name in _TABLE_FUNCTIONS)
    )


def _parse_single_statement(query: str) -> exp.Expression:
    try:
        statements = [s for s in sqlglot.parse(query, dialect="clickhouse") if s is not None]
    except sqlglot.errors.ParseError as err:
        hint = ""
        if re.search(r"\bLIMIT\s+\d+\s+BY\b", query, re.IGNORECASE):
            hint = (
                " LIMIT n BY is not supported by the log guard; "
                "use GROUP BY with any()/argMax() instead."
            )
        raise QueryRejected(f"Could not parse the log query: {err}.{hint}") from err
    if len(statements) != 1:
        raise QueryRejected("Send exactly one statement per log query.")
    statement = statements[0]
    if not isinstance(statement, exp.Query):
        raise QueryRejected("Only SELECT queries are allowed on log tables.")
    return statement


def _guarded_selects(statement: exp.Expression, config: GuardConfig) -> list[exp.Select]:
    selects: list[exp.Select] = []
    for table in statement.find_all(exp.Table):
        if not _is_log_table(table, config):
            continue
        select = table.find_ancestor(exp.Select)
        if select is not None and all(select is not s for s in selects):
            selects.append(select)
    return selects


def _is_log_table(table: exp.Table, config: GuardConfig) -> bool:
    if isinstance(table.this, exp.Anonymous):
        return str(table.this.name).lower() in _TABLE_FUNCTIONS
    return table.name.lower() in config.log_tables


def _reject_star(select: exp.Select) -> None:
    for projection in select.expressions:
        if isinstance(projection, exp.Star) or (
            isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star)
        ):
            raise QueryRejected(f"SELECT * is not allowed on log tables. {_COLUMN_HINT}")


def _check_window(
    select: exp.Select, params: dict[str, Any], config: GuardConfig, now: datetime
) -> float:
    bounds = _Bounds()
    for clause in ("prewhere", "where"):
        node = select.args.get(clause)
        if node is not None:
            for predicate in _conjuncts(node.this):
                _collect_bounds(predicate, bounds, params, config, now)

    if bounds.lower is None:
        detail = f" Could not evaluate: {bounds.unparsed}." if bounds.unparsed else ""
        raise QueryRejected(
            "Log queries need a lower time bound on "
            f"{'/'.join(config.time_columns)} combined with AND.{detail} {_TIME_HINT}"
        )
    upper = bounds.upper or now
    window = (upper - bounds.lower).total_seconds() / 60
    if window <= 0:
        raise QueryRejected("The time range is empty or inverted. Check the bounds and time zone.")

    limit = config.max_trace_window_minutes if bounds.pins_trace else config.max_window_minutes
    if window > limit:
        raise QueryRejected(
            f"Time window is {window:.0f} minutes; the maximum is {limit} minutes"
            + ("" if bounds.pins_trace else " (longer windows need an exact TraceId match)")
            + ". Narrow the window around the reported time, or split it into smaller steps."
        )
    return window


def _conjuncts(node: exp.Expression) -> list[exp.Expression]:
    if isinstance(node, exp.Paren):
        return _conjuncts(node.this)
    if isinstance(node, exp.And):
        return _conjuncts(node.this) + _conjuncts(node.expression)
    return [node]


def _collect_bounds(
    predicate: exp.Expression,
    bounds: _Bounds,
    params: dict[str, Any],
    config: GuardConfig,
    now: datetime,
) -> None:
    if isinstance(predicate, exp.Between) and _is_time_expr(predicate.this, config):
        _set_lower(bounds, predicate.args.get("low"), params, now)
        _set_upper(bounds, predicate.args.get("high"), params, now)
        return

    if isinstance(predicate, (exp.EQ, exp.In)) and _is_column(predicate.this, config.trace_columns):
        bounds.pins_trace = True
        return

    if not isinstance(predicate, (exp.GT, exp.GTE, exp.LT, exp.LTE)):
        return
    left, right = predicate.this, predicate.expression
    is_lower = isinstance(predicate, (exp.GT, exp.GTE))
    if _is_time_expr(left, config) and not _is_time_expr(right, config):
        value = right
    elif _is_time_expr(right, config) and not _is_time_expr(left, config):
        value, is_lower = left, not is_lower
    else:
        return
    if is_lower:
        _set_lower(bounds, value, params, now)
    else:
        _set_upper(bounds, value, params, now)


def _set_lower(bounds: _Bounds, node: Optional[exp.Expression], params: dict, now: datetime):
    value = _eval_time(node, params, now)
    if value is None:
        bounds.unparsed = node.sql(dialect="clickhouse") if node is not None else None
    elif bounds.lower is None or value > bounds.lower:
        bounds.lower = value


def _set_upper(bounds: _Bounds, node: Optional[exp.Expression], params: dict, now: datetime):
    value = _eval_time(node, params, now)
    if value is not None and (bounds.upper is None or value < bounds.upper):
        bounds.upper = value


def _is_column(node: exp.Expression, names: tuple[str, ...]) -> bool:
    return isinstance(node, exp.Column) and node.name.lower() in names


def _is_time_expr(node: exp.Expression, config: GuardConfig) -> bool:
    if _is_column(node, config.time_columns):
        return True
    if isinstance(node, exp.Func):
        columns = list(node.find_all(exp.Column))
        return len(columns) == 1 and _is_column(columns[0], config.time_columns)
    return False


def _eval_time(
    node: Optional[exp.Expression], params: dict[str, Any], now: datetime
) -> Optional[datetime]:
    """Evaluate a constant time expression to an aware datetime, or None if unsupported."""
    if node is None:
        return None
    if isinstance(node, exp.Paren):
        return _eval_time(node.this, params, now)
    if isinstance(node, exp.Literal):
        return _parse_datetime(node.this) if node.is_string else None
    if isinstance(node, exp.Placeholder):
        return _parse_datetime(params.get(str(node.name)))
    if isinstance(node, (exp.CurrentTimestamp, exp.CurrentDate)):
        return now
    if isinstance(node, exp.Column) and node.name.lower() in _NOW_FUNCTIONS:
        return now
    if isinstance(node, exp.Cast):
        return _eval_time(node.this, params, now)
    if isinstance(node, (exp.Add, exp.Sub)):
        return _eval_shift(node, params, now)
    if isinstance(node, exp.Func):
        return _eval_function(node, params, now)
    return None


def _eval_shift(node: exp.Binary, params: dict[str, Any], now: datetime) -> Optional[datetime]:
    base = _eval_time(node.this, params, now)
    seconds = _interval_seconds(node.expression)
    if base is None or seconds is None:
        return None
    delta = timedelta(seconds=seconds)
    return base + delta if isinstance(node, exp.Add) else base - delta


def _eval_function(node: exp.Func, params: dict[str, Any], now: datetime) -> Optional[datetime]:
    name = (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).lower()
    raw_args = node.expressions if isinstance(node, exp.Anonymous) else node.args.values()
    args = [arg for arg in raw_args if isinstance(arg, exp.Expression)]
    if name in _NOW_FUNCTIONS:
        return now
    if name == "today":
        return datetime.combine(now.date(), time.min, tzinfo=now.tzinfo)
    if name == "yesterday":
        return datetime.combine(now.date() - timedelta(days=1), time.min, tzinfo=now.tzinfo)

    shift = _SHIFT_FUNCTION_RE.match(name)
    if shift and len(args) == 2:
        base = _eval_time(args[0], params, now)
        amount = _int_literal(args[1])
        if base is None or amount is None:
            return None
        delta = timedelta(seconds=amount * _UNIT_SECONDS[shift.group(2).upper()])
        return base + delta if shift.group(1).lower() == "add" else base - delta

    if ("date" in name or "time" in name) and args:
        zone = _zone_argument(args[1:])
        if zone is not None and isinstance(args[0], exp.Literal) and args[0].is_string:
            return _parse_datetime(args[0].this, zone)
        return _eval_time(args[0], params, now)
    return None


def _zone_argument(args: list[Any]) -> Optional[ZoneInfo]:
    for arg in args:
        if isinstance(arg, exp.Literal) and arg.is_string:
            try:
                return ZoneInfo(arg.this)
            except (ZoneInfoNotFoundError, ValueError):
                continue
    return None


def _interval_seconds(node: exp.Expression) -> Optional[int]:
    if isinstance(node, exp.Interval):
        amount = _int_literal(node.this)
        unit = str(node.args.get("unit").name if node.args.get("unit") else "").upper()
        unit = unit[:-1] if unit.endswith("S") else unit
        if amount is None or unit not in _UNIT_SECONDS:
            return None
        return amount * _UNIT_SECONDS[unit]
    if isinstance(node, exp.Anonymous):
        match = _INTERVAL_FUNCTION_RE.match(node.name)
        if match and len(node.expressions) == 1:
            amount = _int_literal(node.expressions[0])
            return None if amount is None else amount * _UNIT_SECONDS[match.group(1).upper()]
    return None


def _int_literal(node: exp.Expression) -> Optional[int]:
    if isinstance(node, exp.Literal):
        try:
            return int(str(node.this).strip())
        except ValueError:
            return None
    return None


def _parse_datetime(value: Any, zone: Optional[ZoneInfo] = None) -> Optional[datetime]:
    """Parse a ClickHouse-style datetime string. Naive values are read as UTC unless a zone is given."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    elif isinstance(value, str):
        text = _FRACTION_RE.sub(r"\1", value.strip().replace("Z", "+00:00"))
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone or timezone.utc)
    return parsed


def _check_limit(statement: exp.Expression, config: GuardConfig) -> None:
    limits = _outer_limits(statement)
    if not limits:
        raise QueryRejected(
            f"Log queries need an outer LIMIT of at most {config.max_limit}. "
            "Start with LIMIT 20 and widen only if needed."
        )
    for limit in limits:
        value = _int_literal(limit.expression) if limit.expression is not None else None
        if value is None:
            raise QueryRejected("LIMIT must be a plain integer on log queries.")
        if value > config.max_limit:
            raise QueryRejected(
                f"LIMIT {value} is above the maximum of {config.max_limit}. "
                "Aggregate first (count() GROUP BY ServiceName, SeverityText) to find where to look."
            )


def _outer_limits(statement: exp.Expression) -> list[exp.Limit]:
    limit = statement.args.get("limit")
    if isinstance(limit, exp.Limit):
        return [limit]
    if isinstance(statement, exp.SetOperation):
        branches = [_outer_limits(statement.this), _outer_limits(statement.expression)]
        return branches[0] + branches[1] if all(branches) else []
    if isinstance(statement, exp.Subquery):
        return _outer_limits(statement.this)
    return []

