"""Tests for the tech-support log guard: query rules, redaction, and middleware wiring."""

import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from mcp_clickhouse import clients
from mcp_clickhouse.support_guard import middleware as guard_middleware
from mcp_clickhouse.support_guard.config import get_guard_config
from mcp_clickhouse.support_guard.query_guard import QueryRejected, inspect_query
from mcp_clickhouse.support_guard.redaction import (
    REDACTED,
    is_sensitive_key,
    redact_text,
    sanitize_tool_text,
)

NOW = datetime(2026, 11, 6, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def config(monkeypatch):
    for name in list(__import__("os").environ):
        if name.startswith("SUPPORT_GUARD_"):
            monkeypatch.delenv(name)
    return get_guard_config()


def check(query, config, params=None):
    return inspect_query(query, params, config, now=NOW)


class TestQueryGuard:
    @pytest.mark.parametrize(
        "query",
        [
            "SELECT Timestamp, Body FROM cm_otel.otel_logs WHERE ServiceName = 'ikit' "
            "AND Timestamp >= now() - INTERVAL 30 MINUTE ORDER BY Timestamp DESC LIMIT 50",
            "SELECT Body FROM otel_logs WHERE Timestamp BETWEEN "
            "toDateTime64('2026-11-06 10:00:00', 9, 'Asia/Kolkata') AND "
            "toDateTime64('2026-11-06 10:45:00', 9, 'Asia/Kolkata') LIMIT 20",
            "SELECT ServiceName, count() FROM otel_logs WHERE Timestamp >= {start:DateTime64(9)} "
            "AND Timestamp < {end:DateTime64(9)} GROUP BY ServiceName LIMIT 50",
            "SELECT Body FROM otel_logs WHERE (Timestamp > now64() - toIntervalMinute(15)) LIMIT 5",
            "SELECT Body FROM otel_logs WHERE subtractMinutes(now(), 10) <= Timestamp LIMIT 5",
        ],
    )
    def test_accepts_narrow_queries(self, query, config):
        params = {"start": "2026-11-06 11:00:00", "end": "2026-11-06 11:30:00"}
        decision = check(query, config, params)
        assert decision.touches_log_table
        assert decision.window_minutes <= config.max_window_minutes

    def test_ignores_other_tables(self, config):
        assert not check("SELECT * FROM system.tables", config).touches_log_table

    @pytest.mark.parametrize(
        "query",
        [
            "DESCRIBE TABLE otel_logs",
            "SHOW CREATE TABLE cm_otel.otel_logs",
            "EXPLAIN ESTIMATE SELECT Body FROM otel_logs WHERE Timestamp > now() - INTERVAL 1 DAY",
        ],
    )
    def test_passes_metadata_statements(self, query, config):
        assert check(query, config).touches_log_table

    @pytest.mark.parametrize(
        "query, message",
        [
            ("SELECT Body FROM otel_logs LIMIT 10", "lower time bound"),
            (
                "SELECT Body FROM otel_logs WHERE Timestamp > now() - INTERVAL 1 DAY LIMIT 10",
                "maximum is 60 minutes",
            ),
            (
                "SELECT Body FROM otel_logs WHERE Timestamp >= '2026-11-01 00:00:00' LIMIT 10",
                "Time window",
            ),
            (
                "SELECT Body FROM otel_logs WHERE Timestamp > now() - INTERVAL 10 MINUTE",
                "LIMIT",
            ),
            (
                "SELECT Body FROM otel_logs WHERE Timestamp > now() - INTERVAL 10 MINUTE LIMIT 5000",
                "above the maximum",
            ),
            (
                "SELECT * FROM otel_logs WHERE Timestamp > now() - INTERVAL 10 MINUTE LIMIT 5",
                "SELECT *",
            ),
            (
                "SELECT Body FROM otel_logs WHERE ServiceName = 'ikit' OR "
                "Timestamp > now() - INTERVAL 10 MINUTE LIMIT 5",
                "lower time bound",
            ),
            (
                "SELECT Body FROM otel_logs WHERE Timestamp > now() - INTERVAL 10 MINUTE "
                "LIMIT 1 BY ServiceName LIMIT 10",
                "LIMIT n BY",
            ),
            (
                "SELECT Body FROM merge('cm_otel', '^otel') WHERE ServiceName = 'ikit' LIMIT 5",
                "lower time bound",
            ),
            (
                "SELECT x FROM (SELECT Body AS x FROM otel_logs WHERE ServiceName = 'ikit') LIMIT 5",
                "lower time bound",
            ),
            (
                "SELECT Body FROM otel_logs WHERE Timestamp > now() - INTERVAL 5 MINUTE "
                "AND Timestamp < now() - INTERVAL 10 MINUTE LIMIT 5",
                "empty or inverted",
            ),
            ("SELECT 1; SELECT Body FROM otel_logs", "exactly one statement"),
        ],
    )
    def test_rejects_unsafe_queries(self, query, message, config):
        with pytest.raises(QueryRejected, match=message):
            check(query, config)

    def test_trace_lookup_allows_wider_window(self, config):
        query = (
            "SELECT Timestamp, ServiceName, Body FROM otel_logs WHERE TraceId = 'abc123' "
            "AND Timestamp > now() - INTERVAL 6 HOUR ORDER BY Timestamp LIMIT 200"
        )
        assert check(query, config).window_minutes == 360

    def test_window_limit_is_configurable(self, config):
        narrow = replace(config, max_window_minutes=15)
        with pytest.raises(QueryRejected, match="maximum is 15 minutes"):
            check("SELECT Body FROM otel_logs WHERE Timestamp > now() - INTERVAL 30 MINUTE LIMIT 5", narrow)


class TestRedaction:
    @pytest.mark.parametrize(
        "key, expected",
        [
            ("authorization", True),
            ("req.headers.authorization", True),
            ("x-api-key", True),
            ("accessToken", True),
            ("whatsapp_access_token", True),
            ("client_secret", True),
            ("x-hub-signature-256", True),
            ("botId", False),
            ("totalTokens", False),
            ("phoneNumber", False),
            ("msg", False),
        ],
    )
    def test_sensitive_keys(self, key, expected):
        assert is_sensitive_key(key) is expected

    @pytest.mark.parametrize(
        "text, secret",
        [
            ('{"req":{"headers":{"authorization":"Bearer abc.def.ghi"}}}', "abc.def.ghi"),
            ('{\\"authorization\\":\\"Bearer zzzzzzzzzzzz\\"}', "zzzzzzzzzzzz"),
            ("calling with Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA"),
            (
                "token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N",
                "eyJhbGciOiJIUzI1NiJ9",
            ),
            ("graph call access_token=EAAGm0PX4ZCpsBAKZCZBabcdefghijklmnopqrstu", "EAAGm0PX4ZC"),
            ("https://x.blob.core.windows.net/f.csv?sv=2024&sig=AbCdEf1234567890%3D", "AbCdEf1234"),
            ("key sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123", "abcdefghijklmnop"),
            ('{"x-api-key": "live_9f8e7d6c5b4a"}', "live_9f8e7d6c5b4a"),
            ("cookie=connect.sid%3Ds%253Aabcdef123456", "abcdef123456"),
        ],
    )
    def test_redacts_secrets_in_text(self, text, secret):
        redacted = redact_text(text)
        assert secret not in redacted
        assert REDACTED in redacted

    def test_keeps_investigation_fields(self):
        text = '{"botId":"64f1c0ffee","phoneNumber":"919876543210","msg":"template send failed"}'
        assert redact_text(text) == text

    def test_sanitizes_rows_maps_and_long_cells(self):
        payload = {
            "columns": ["Body", "LogAttributes"],
            "rows": [
                [
                    "x" * 50 + ' {"authorization":"Bearer secret-value-123"}',
                    {"req.headers.authorization": "Bearer abc", "botId": "b1"},
                ]
            ],
        }
        out = json.loads(sanitize_tool_text(json.dumps(payload), 40, 10000))
        body, attributes = out["rows"][0]
        assert "secret-value-123" not in body
        assert "[truncated" in body
        assert attributes == {"req.headers.authorization": REDACTED, "botId": "b1"}

    def test_cuts_rows_to_response_budget(self):
        payload = {"columns": ["Body"], "rows": [["log line " + "y" * 200] for _ in range(100)]}
        out = sanitize_tool_text(json.dumps(payload), 1000, 3000)
        decoded = json.loads(out)
        assert len(out) <= 3000
        assert 0 < len(decoded["rows"]) < 100
        assert "of 100 rows" in decoded["notice"]

    def test_non_json_text_is_redacted(self):
        assert "abcdefghijk" not in sanitize_tool_text("Bearer abcdefghijk", 100, 1000)


@pytest.mark.asyncio
class TestMiddleware:
    @pytest.fixture
    def server(self, config):
        mcp = FastMCP("guard-test")
        calls = []

        @mcp.tool(name="run_query")
        def run_query(query: str, params: dict | None = None) -> str:
            calls.append(query)
            return json.dumps(
                {
                    "columns": ["Body", "LogAttributes"],
                    "rows": [['{"authorization":"Bearer leaked-token-1"}', {"cookie": "sid=1"}]],
                }
            )

        @mcp.tool(name="list_databases")
        def list_databases() -> str:
            return json.dumps(["Bearer not-run-query"])

        mcp.add_middleware(guard_middleware.SupportGuardMiddleware(config))
        return mcp, calls

    async def test_rejected_query_never_runs(self, server):
        mcp, calls = server
        async with Client(mcp) as client:
            with pytest.raises(ToolError, match="Log guard"):
                await client.call_tool("run_query", {"query": "SELECT Body FROM otel_logs LIMIT 5"})
        assert calls == []

    async def test_results_are_redacted(self, server):
        mcp, calls = server
        query = "SELECT Body, LogAttributes FROM otel_logs WHERE Timestamp > now() - INTERVAL 5 MINUTE LIMIT 5"
        async with Client(mcp) as client:
            result = await client.call_tool("run_query", {"query": query})
        text = result.content[0].text
        assert "leaked-token-1" not in text
        assert "sid=1" not in text
        assert calls == [query]

    async def test_other_tools_pass_through(self, server):
        mcp, _ = server
        async with Client(mcp) as client:
            result = await client.call_tool("list_databases", {})
        assert "not-run-query" in result.content[0].text

    async def test_rate_limit(self, server, config):
        mcp, _ = server
        mcp.middleware.clear()
        mcp.add_middleware(
            guard_middleware.SupportGuardMiddleware(replace(config, max_log_queries_per_minute=1))
        )
        query = "SELECT Body FROM otel_logs WHERE Timestamp > now() - INTERVAL 5 MINUTE LIMIT 5"
        async with Client(mcp) as client:
            await client.call_tool("run_query", {"query": query})
            with pytest.raises(ToolError, match="log queries in the last minute"):
                await client.call_tool("run_query", {"query": query})


class TestQuerySettings:
    def test_adds_limits_unless_server_is_strict_readonly(self, config, monkeypatch):
        monkeypatch.setattr(clients, "build_query_settings", lambda client: {"readonly": client})
        guard_middleware._install_query_settings(config)
        relaxed = clients.build_query_settings("1")
        strict = clients.build_query_settings("2")
        assert relaxed["max_execution_time"] == "25" and relaxed["readonly"] == "1"
        assert strict == {"readonly": "2"}

    def test_install_is_idempotent(self, config, monkeypatch):
        monkeypatch.setattr(clients, "build_query_settings", lambda client: {})
        guard_middleware._install_query_settings(config)
        wrapped = clients.build_query_settings
        guard_middleware._install_query_settings(config)
        assert clients.build_query_settings is wrapped
