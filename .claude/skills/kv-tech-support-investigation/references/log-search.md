# Log search playbook (ClickHouse `otel_logs`)

How to find one verifiable log line quickly without flooding the agent context or loading the cluster. The `log-investigator` subagent follows this file. Humans can too.

## The table

Logs from every pod are shipped by the OpenTelemetry collector into `<db>.otel_logs`. `cm_otel` is the Chatomate database. Confirm the database for TTL and Netcore with `list_databases` once per session.

| Column | Type | Use |
|---|---|---|
| `Timestamp` | DateTime64(9) | Always bounded. Stored in UTC |
| `ServiceName` | LowCardinality(String) | First column of the sort key: the cheapest filter you have |
| `SeverityText` | LowCardinality(String) | `ERROR`, `WARN`, `INFO`, `DEBUG` (pino levels may also appear as numbers inside `Body`) |
| `Body` | String | The log line. Chatomate services log pino JSON, so this is usually a JSON string |
| `TraceId`, `SpanId` | String | Filled only when the collector or service sets them. Check before relying on it |
| `ResourceAttributes` | Map | `k8s.namespace.name`, `k8s.pod.name`, `k8s.container.name` |
| `LogAttributes` | Map | Parsed fields from the log line when the collector extracts them |

Run `SHOW CREATE TABLE <db>.otel_logs` once per session to confirm the sort key and skip indexes. The standard exporter schema sorts by `ServiceName` then time and adds a `tokenbf_v1` index on `Body` plus bloom filters on `TraceId` and map keys and values. The rules below assume that layout.

## Guard rules the server enforces

Queries on `otel_logs` are rejected unless they have:

- a lower time bound on `Timestamp` combined with `AND` (not inside an `OR`), and a window of at most **60 minutes** (up to **24 hours** when the query also pins `TraceId = '...'`)
- an outer `LIMIT` of at most **200**
- explicit columns (no `SELECT *`)
- one statement only. `LIMIT n BY` is not supported, use `GROUP BY` instead

Results are redacted (auth headers, cookies, API keys, JWTs, Meta / OpenAI / AWS tokens, signed URL signatures), long cells are cut at ~3000 characters, and the whole result is cut at ~60K characters with a `notice`. At most 2 log queries run at once and 20 per minute. If you hit a limit, that means narrow down, not retry.

## Writing the time bound

Reported times are usually IST. Pass the zone explicitly so nobody has to convert by hand:

```sql
Timestamp >= toDateTime64('2026-11-06 14:15:00', 9, 'Asia/Kolkata')
AND Timestamp <  toDateTime64('2026-11-06 14:45:00', 9, 'Asia/Kolkata')
```

For "it is failing right now": `Timestamp >= now() - INTERVAL 15 MINUTE`.

Start with a **30-minute** window centred on the reported time. Widen to 60 only after a zero-hit histogram. Beyond that, move the window instead of widening it, and stop after three moves.

## Searching for an identifier

| Need | Use | Why |
|---|---|---|
| A whole token in `Body` (Mongo ObjectId, numeric phone, UUID part) | `hasToken(Body, '64f1c0ffee0123456789abcd')` | Uses the `Body` token index: skips most data |
| A phrase or partial string in `Body` | `position(Body, 'Error while authenticating') > 0` | Full scan of the window: pair it with `ServiceName` |
| A known attribute | `LogAttributes['botId'] = '...'` | Uses the map value bloom filter |
| A value under any attribute key | `has(mapValues(LogAttributes), '...')` | Exact value match, indexed |
| Substring under any attribute key | `mapExists((k, v) -> position(v, '...') > 0, LogAttributes)` | Slow: last resort |
| A field inside the JSON body | `JSONExtractString(Body, 'botId') = '...'` | Parses every row: only after a cheap filter |

`hasToken` needs a single token: only letters and digits. For a phone number use the plain digits without `+` (`919876543210`). For a UUID use its longest segment (`hasToken(Body, '934ac269d236')`) and confirm with `position` on the full value.

## The query ladder

Go down one rung at a time. Each rung is cheap and tells you where to aim the next one.

**1. Discover what is logging** (only once per session, or when the service name is unknown):

```sql
SELECT ServiceName, count() AS lines
FROM cm_otel.otel_logs
WHERE Timestamp >= now() - INTERVAL 5 MINUTE
GROUP BY ServiceName
ORDER BY lines DESC
LIMIT 100
```

**2. Histogram the identifier** across services and levels in the incident window:

```sql
SELECT ServiceName, SeverityText, count() AS hits, min(Timestamp) AS first, max(Timestamp) AS last
FROM cm_otel.otel_logs
WHERE Timestamp >= toDateTime64('2026-11-06 14:15:00', 9, 'Asia/Kolkata')
  AND Timestamp <  toDateTime64('2026-11-06 14:45:00', 9, 'Asia/Kolkata')
  AND hasToken(Body, '64f1c0ffee0123456789abcd')
GROUP BY ServiceName, SeverityText
ORDER BY hits DESC
LIMIT 50
```

Zero hits: the id is not logged as a token there, the window is wrong, or the environment is wrong. Try the next identifier before widening the window.

**3. Fetch a handful of rows** from the most promising service, errors first:

```sql
SELECT Timestamp, SeverityText, Body, ResourceAttributes['k8s.pod.name'] AS pod, TraceId
FROM cm_otel.otel_logs
WHERE ServiceName = 'ikit'
  AND Timestamp >= toDateTime64('2026-11-06 14:15:00', 9, 'Asia/Kolkata')
  AND Timestamp <  toDateTime64('2026-11-06 14:45:00', 9, 'Asia/Kolkata')
  AND SeverityText IN ('ERROR', 'WARN')
  AND hasToken(Body, '64f1c0ffee0123456789abcd')
ORDER BY Timestamp DESC
LIMIT 10
```

No errors? Drop the severity filter and look at `INFO` / `DEBUG`. Many failures in this code base are logged at info level, or not logged until a later step. Keep `LIMIT` at 10 to 20 for raw rows.

**4. Pick the anchor.** One line that matches the ticket (right bot, right user or phone, right time, right symptom) and that you can point to in code. Record its `Timestamp`, `ServiceName`, pod, `TraceId` and request id.

**5. Expand by request or trace id.** HTTP services generate a UUID request id (`genReqId`), and nestjs-pino stamps it on every line written during that request as `req.id` inside the JSON body. Look at the anchor body to see the field, then:

```sql
SELECT Timestamp, ServiceName, SeverityText, Body
FROM cm_otel.otel_logs
WHERE ServiceName = 'ikit'
  AND Timestamp >= toDateTime64('2026-11-06 14:25:00', 9, 'Asia/Kolkata')
  AND Timestamp <  toDateTime64('2026-11-06 14:35:00', 9, 'Asia/Kolkata')
  AND hasToken(Body, '934ac269d236')
  AND position(Body, 'ed581d68-ae29-42d7-8a3f-934ac269d236') > 0
ORDER BY Timestamp
LIMIT 100
```

When `TraceId` is filled, prefer it: it crosses services and the guard allows a wider window for it:

```sql
SELECT Timestamp, ServiceName, SeverityText, Body
FROM cm_otel.otel_logs
WHERE TraceId = '4bf92f3577b34da6a3ce929d0e0e4736'
  AND Timestamp >= toDateTime64('2026-11-06 12:00:00', 9, 'Asia/Kolkata')
  AND Timestamp <  toDateTime64('2026-11-06 18:00:00', 9, 'Asia/Kolkata')
ORDER BY Timestamp
LIMIT 200
```

Work that hops services over RabbitMQ or Kafka has no shared request id. Follow it with the business id instead (`botId` + `botUserId`, `campaignId`, `messageId`, WhatsApp `wamid`) in the next service, a few minutes after the anchor.

**6. Pull only the columns you need.** `Body` plus `Timestamp` is usually enough. Avoid `ResourceAttributes`, `ScopeAttributes` and full `LogAttributes` in raw-row queries unless you need a specific key.

## Before running a query you are unsure about

`EXPLAIN ESTIMATE <query>` returns the rows and marks ClickHouse expects to read, without running it. If it says hundreds of millions of rows, add `ServiceName` or shrink the window first.

## Things that waste time

- Searching `ERROR` only. Many real causes are `INFO` lines just before a generic error.
- Searching the bot name. Logs carry IDs. Resolve names in MongoDB first.
- Searching `+91...` or formatted phone numbers. Logs usually carry the plain digits with the country code.
- Fetching 200 raw rows to "look around". Histogram first.
- Re-running the same query after a guard rejection. Read the message: it tells you what to change.
