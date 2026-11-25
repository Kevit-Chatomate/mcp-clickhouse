"""FastMCP middleware that applies the tech-support guard to run_query.

Enable it with MCP_MIDDLEWARE_MODULE=mcp_clickhouse.support_guard.middleware. It keeps
all changes out of upstream files so the fork can keep syncing with ClickHouse/mcp-clickhouse.
"""

import asyncio
import logging
import time
from collections import deque
from typing import Any, Optional

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult
from mcp.types import TextContent

from mcp_clickhouse import clients
from mcp_clickhouse.support_guard.config import GuardConfig, get_guard_config
from mcp_clickhouse.support_guard.query_guard import QueryRejected, inspect_query
from mcp_clickhouse.support_guard.redaction import sanitize_tool_text

logger = logging.getLogger("mcp-clickhouse.support-guard")

GUARDED_TOOLS = {"run_query"}
_QUEUE_WAIT_SECONDS = 30


class SupportGuardMiddleware(Middleware):
    """Validate log queries, cap their rate, and sanitize every run_query result."""

    def __init__(self, config: GuardConfig):
        """Create the middleware.

        @param config - guard limits
        """
        self.config = config
        self._semaphore = asyncio.Semaphore(config.max_concurrent_log_queries)
        self._recent: deque[float] = deque()

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext) -> ToolResult:
        """Guard run_query calls and pass every other tool through unchanged.

        @param context - FastMCP middleware context for the tool call
        @param call_next - the next handler in the chain
        @returns the sanitized tool result
        """
        message = context.message
        if getattr(message, "name", None) not in GUARDED_TOOLS:
            return await call_next(context)

        arguments = getattr(message, "arguments", None) or {}
        query = str(arguments.get("query", ""))
        params = arguments.get("params")
        try:
            decision = inspect_query(query, params if isinstance(params, dict) else None, self.config)
        except QueryRejected as err:
            logger.info("Rejected log query: %s", err)
            raise ToolError(f"Log guard: {err}") from err

        if not decision.touches_log_table:
            return self._sanitize(await call_next(context))

        self._check_rate()
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=_QUEUE_WAIT_SECONDS)
        except asyncio.TimeoutError as err:
            raise ToolError(
                "Log guard: other log queries are still running. Wait for them to finish "
                "instead of sending more in parallel."
            ) from err
        try:
            return self._sanitize(await call_next(context))
        finally:
            self._semaphore.release()

    def _check_rate(self) -> None:
        now = time.monotonic()
        while self._recent and now - self._recent[0] > 60:
            self._recent.popleft()
        if len(self._recent) >= self.config.max_log_queries_per_minute:
            raise ToolError(
                f"Log guard: more than {self.config.max_log_queries_per_minute} log queries in "
                "the last minute. Review what you already have before querying again."
            )
        self._recent.append(now)

    def _sanitize(self, result: ToolResult) -> ToolResult:
        content = [self._sanitize_block(block) for block in (result.content or [])]
        structured = _sanitize_structured(result.structured_content, self.config)
        return ToolResult(
            content=content,
            structured_content=structured,
            meta=result.meta,
            is_error=getattr(result, "is_error", False),
        )

    def _sanitize_block(self, block: Any) -> Any:
        if isinstance(block, TextContent):
            text = sanitize_tool_text(
                block.text, self.config.max_cell_chars, self.config.max_response_chars
            )
            return block.model_copy(update={"text": text})
        return block


def _sanitize_structured(structured: Optional[Any], config: GuardConfig) -> Optional[Any]:
    if not isinstance(structured, dict):
        return structured
    return {
        key: (
            sanitize_tool_text(value, config.max_cell_chars, config.max_response_chars)
            if isinstance(value, str)
            else value
        )
        for key, value in structured.items()
    }


def _install_query_settings(config: GuardConfig) -> None:
    """Add resource limits to every ClickHouse query the server runs."""
    if not config.query_settings or getattr(clients.build_query_settings, "_support_guard", False):
        return
    original = clients.build_query_settings

    def build_query_settings_with_limits(client) -> dict[str, str]:
        settings = original(client)
        # readonly=2 forbids changing settings per query; the server profile applies then.
        if settings.get("readonly") != "2":
            settings = {**config.query_settings, **settings}
        return settings

    build_query_settings_with_limits._support_guard = True
    clients.build_query_settings = build_query_settings_with_limits


def setup_middleware(mcp) -> None:
    """Register the support guard on the FastMCP server.

    @param mcp - the FastMCP server instance
    @returns nothing
    """
    config = get_guard_config()
    _install_query_settings(config)
    mcp.add_middleware(SupportGuardMiddleware(config))
    logger.info(
        "Support guard enabled: tables=%s window<=%sm trace_window<=%sm limit<=%s settings=%s",
        ",".join(config.log_tables),
        config.max_window_minutes,
        config.max_trace_window_minutes,
        config.max_limit,
        ",".join(sorted(config.query_settings)),
    )
