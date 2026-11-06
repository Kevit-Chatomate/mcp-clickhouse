"""Environment-backed settings for the tech-support log guard."""

import os
from dataclasses import dataclass


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    """Read a positive integer environment variable, failing clearly on bad input."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as err:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from err
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    return value


def _list_env(name: str, default: str) -> tuple[str, ...]:
    """Read a comma separated environment variable as a lower-cased tuple."""
    raw = os.getenv(name, default)
    return tuple(item.strip().lower() for item in raw.split(",") if item.strip())


def _settings_env(name: str, default: str) -> dict[str, str]:
    """Parse `key=value,key=value` ClickHouse settings."""
    raw = os.getenv(name, default)
    settings: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        key, sep, value = pair.partition("=")
        if not sep or not key.strip() or not value.strip():
            raise ValueError(f"{name} entries must look like key=value, got {pair!r}")
        settings[key.strip()] = value.strip()
    return settings


@dataclass(frozen=True)
class GuardConfig:
    """Limits applied to queries and results for tech-support investigations."""

    log_tables: tuple[str, ...]
    time_columns: tuple[str, ...]
    trace_columns: tuple[str, ...]
    max_window_minutes: int
    max_trace_window_minutes: int
    max_limit: int
    max_cell_chars: int
    max_response_chars: int
    max_concurrent_log_queries: int
    max_log_queries_per_minute: int
    query_settings: dict[str, str]


def get_guard_config() -> GuardConfig:
    """Build the guard configuration from SUPPORT_GUARD_* environment variables."""
    return GuardConfig(
        log_tables=_list_env("SUPPORT_GUARD_LOG_TABLES", "otel_logs"),
        time_columns=_list_env("SUPPORT_GUARD_TIME_COLUMNS", "Timestamp,TimestampTime"),
        trace_columns=_list_env("SUPPORT_GUARD_TRACE_COLUMNS", "TraceId"),
        max_window_minutes=_int_env("SUPPORT_GUARD_MAX_WINDOW_MINUTES", 60),
        max_trace_window_minutes=_int_env("SUPPORT_GUARD_MAX_TRACE_WINDOW_MINUTES", 1440),
        max_limit=_int_env("SUPPORT_GUARD_MAX_LIMIT", 200),
        max_cell_chars=_int_env("SUPPORT_GUARD_MAX_CELL_CHARS", 3000, minimum=100),
        max_response_chars=_int_env("SUPPORT_GUARD_MAX_RESPONSE_CHARS", 60000, minimum=1000),
        max_concurrent_log_queries=_int_env("SUPPORT_GUARD_MAX_CONCURRENT_LOG_QUERIES", 2),
        max_log_queries_per_minute=_int_env("SUPPORT_GUARD_MAX_LOG_QUERIES_PER_MINUTE", 20),
        query_settings=_settings_env(
            "SUPPORT_GUARD_QUERY_SETTINGS",
            "max_execution_time=25,max_threads=8,max_memory_usage=4000000000",
        ),
    )
