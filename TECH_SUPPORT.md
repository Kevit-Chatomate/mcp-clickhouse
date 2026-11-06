# Kevit fork: tech-support log investigation

This fork tracks [ClickHouse/mcp-clickhouse](https://github.com/ClickHouse/mcp-clickhouse) and adds a guard for investigating Chatomate / TTL production logs with AI agents. Everything Kevit-specific lives in new files, so upstream releases merge without conflicts:

| Path | What |
|---|---|
| `mcp_clickhouse/support_guard/` | Query guard, credential redaction and result shaping, loaded as MCP middleware |
| `.claude/skills/kv-tech-support-investigation/` | Claude Code skill: ticket intake -> ID lookup -> code path -> anchor log -> root cause summary |
| `.claude/agents/log-investigator.md` | Subagent that runs the log queries in its own context and returns a short digest |
| `tests/test_support_guard.py` | Tests for the guard |
| `docker-compose.yaml`, `.example.env` | Local / VM deployment |

The only upstream files touched are `pyproject.toml` and `uv.lock` (adds `sqlglot`).

## Why

- Production logs are huge (TTL: about a million lines in five minutes). An agent that runs a broad query fills its own context and loads the cluster for everyone.
- Every request-scoped log line from the NestJS services contains the request headers, including `authorization`, because no service configures pino `redact`. Those tokens must not reach an AI chat.
- Error-only searching is not enough. Agents need `INFO` / `DEBUG` lines too, so the guard limits *how much* is read and returned, not *which levels*.

## What the guard does

Enabled with `MCP_MIDDLEWARE_MODULE=mcp_clickhouse.support_guard.middleware`.

**Before a query runs**, for any query that reads a log table (`otel_logs` by default, plus `merge()` / `remote()` / `cluster()` table functions):

- one SELECT statement only (`DESCRIBE`, `EXPLAIN`, `SHOW`, `EXISTS` pass through)
- a lower bound on `Timestamp` / `TimestampTime` joined with `AND`, and a window of at most 60 minutes. A query that pins `TraceId = '...'` may span 24 hours, because the TraceId bloom filter keeps it cheap
- an outer `LIMIT` of at most 200
- no `SELECT *`
- at most 2 log queries at once (others wait up to 30s) and 20 per minute

Rejections come back as tool errors that explain what to change, so the agent fixes the query instead of retrying it.

**On every query** the ClickHouse settings `max_execution_time=25`, `max_threads=8` and `max_memory_usage=4GB` are added, unless the server user is `readonly=2`, in which case its settings profile applies.

**After every `run_query`** (any table):

- values under credential-like keys are replaced with `[REDACTED]` (`authorization`, `cookie`, `*token`, `*secret`, `*password`, `*apikey`, `*signature`, ...), in maps and inside JSON text in `Body`
- token patterns anywhere are redacted: `Bearer` / `Basic` values, JWTs, Meta `EAA...`, OpenAI / Anthropic `sk-...`, Google `AIza...`, AWS `AKIA...`, GitHub and Slack tokens, signed-URL signatures
- strings longer than 3000 characters are cut, and the whole result is cut at 60K characters with a `notice` telling the agent how to narrow down

Phone numbers, bot ids and user ids are **not** redacted: they are what investigations search on.

This is an accident guard, not a security boundary. A determined user of the MCP server can still write expensive SQL against other tables. The ClickHouse user below is the hard limit.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `MCP_MIDDLEWARE_MODULE` | unset | Set to `mcp_clickhouse.support_guard.middleware` to enable the guard |
| `SUPPORT_GUARD_LOG_TABLES` | `otel_logs` | Comma separated table names the query rules apply to |
| `SUPPORT_GUARD_TIME_COLUMNS` | `Timestamp,TimestampTime` | Columns accepted as the time bound |
| `SUPPORT_GUARD_TRACE_COLUMNS` | `TraceId` | Columns whose equality match allows the longer window |
| `SUPPORT_GUARD_MAX_WINDOW_MINUTES` | `60` | Maximum time window for log queries |
| `SUPPORT_GUARD_MAX_TRACE_WINDOW_MINUTES` | `1440` | Maximum window when `TraceId` is pinned |
| `SUPPORT_GUARD_MAX_LIMIT` | `200` | Maximum outer `LIMIT` on log queries |
| `SUPPORT_GUARD_MAX_CELL_CHARS` | `3000` | Per-string truncation in results |
| `SUPPORT_GUARD_MAX_RESPONSE_CHARS` | `60000` | Total result budget, about 15K tokens |
| `SUPPORT_GUARD_MAX_CONCURRENT_LOG_QUERIES` | `2` | Log queries running at once |
| `SUPPORT_GUARD_MAX_LOG_QUERIES_PER_MINUTE` | `20` | Log queries per minute (whole server) |
| `SUPPORT_GUARD_QUERY_SETTINGS` | `max_execution_time=25,max_threads=8,max_memory_usage=4000000000` | ClickHouse settings added to every query. Empty disables |

TTL runs at much higher volume. Consider `SUPPORT_GUARD_MAX_WINDOW_MINUTES=30` for its deployment.

## ClickHouse user (do this once per cluster)

Give the MCP server its own user with a settings profile and a quota, so even a guard bypass cannot choke the cluster. Adjust the numbers to your hardware:

```sql
CREATE SETTINGS PROFILE IF NOT EXISTS mcp_support SETTINGS
    readonly = 1,
    max_execution_time = 30,
    max_threads = 8,
    max_memory_usage = 4000000000,
    max_rows_to_read = 500000000, read_overflow_mode = 'throw',
    max_result_rows = 10000, result_overflow_mode = 'throw',
    max_concurrent_queries_for_user = 4;

CREATE USER IF NOT EXISTS mcp_support
    IDENTIFIED WITH sha256_password BY '<generate a strong password>'
    SETTINGS PROFILE 'mcp_support';

CREATE QUOTA IF NOT EXISTS mcp_support_quota
    FOR INTERVAL 1 hour MAX queries = 600, read_rows = 20000000000, execution_time = 1800
    TO mcp_support;

GRANT SELECT ON cm_otel.otel_logs TO mcp_support;
```

`readonly = 1` (not 2) lets the server add its per-query limits. Grant only the log tables the investigations need.

## Run it

```bash
cp .example.env .env   # fill in the ClickHouse host, the mcp_support password and an auth token
docker compose up -d --build
```

Register it in Claude Code (HTTP transport, bearer token):

```bash
claude mcp add --transport http clickhouse http://<host>:8000/mcp --header "Authorization: Bearer <CLICKHOUSE_MCP_AUTH_TOKEN>"
```

Or run it locally over stdio without Docker:

```bash
uv sync
MCP_MIDDLEWARE_MODULE=mcp_clickhouse.support_guard.middleware uv run mcp-clickhouse
```

## Install the skill and subagent

Support engineers work in the chatomate repository, so install them at user level to use them there:

```bash
mkdir -p ~/.claude/skills ~/.claude/agents
cp -r .claude/skills/kv-tech-support-investigation ~/.claude/skills/
cp .claude/agents/log-investigator.md ~/.claude/agents/
```

Then, in a Claude Code session in the chatomate repo, paste the ticket or run `/kv-tech-support-investigation`.

## Keeping up with upstream

```bash
git remote add upstream https://github.com/ClickHouse/mcp-clickhouse   # once
git fetch upstream
git checkout main && git merge --ff-only upstream/main && git push origin main
git checkout feture/tech-support-mcp-server-agent && git merge main
uv lock && uv run pytest -q tests/test_support_guard.py
```

Upstream conflicts should only appear in `pyproject.toml` / `uv.lock`. Keep upstream's changes, re-add `sqlglot`, and run `uv lock`. If upstream renames `run_query` or changes `clients.build_query_settings`, `tests/test_support_guard.py` and the startup log line (`Support guard enabled: ...`) will show it.

## Permanent fix in chatomate

The redaction here is a safety net. The real fix is a few lines in each service's pino config, so tokens never reach the logs at all:

```ts
const PRODUCTION_LOGGER_CONFIGS = {
  level: process.env.LOG_LEVEL || 'debug',
  redact: {
    paths: ['req.headers.authorization', 'req.headers.cookie', 'req.headers["x-api-key"]', 'res.headers["set-cookie"]'],
    censor: '[REDACTED]',
  },
};
```
