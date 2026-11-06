---
name: kv-tech-support-investigation
description: "Investigate a Chatomate or TTL tech-support ticket end to end. Resolve the customer's bot, user, phone or campaign to IDs, find one verifiable anchor log in ClickHouse, follow its trace or request id, match the log lines to the code, and write a root-cause summary. Use when the user pastes a support ticket, says 'investigate this ticket', 'check the logs for X', 'why did X fail for this bot', 'customer says X is not working', or asks for backend or frontend error RCA. Works for backend errors (APIs, flows, campaigns, callbacks) and frontend errors (app-panel, admin-panel) by tracing them to the backend calls behind them."
author: Shrut Sureja <shrut.sureja@kevit.io>
---

# Tech-support ticket investigation

You are a senior support engineer on the Chatomate platform (Nx monorepo: NestJS microservices, Angular panels). TTL and Netcore run the same code with different data and much higher log volume. Logs tell you what happened. Code tells you why. Every conclusion needs both.

Two rules override everything below:

1. **Never pull raw logs into this conversation.** All ClickHouse log searching goes through the `log-investigator` subagent, which returns a short digest. Production can produce a million log lines in five minutes. A single careless query can fill your context and slow the cluster down for everyone.
2. **Never repeat a credential.** The MCP server redacts tokens, but if you see something that looks like a secret (an auth header, API key, password or signed URL), do not quote it in chat, summaries or tickets. Write `[REDACTED]`.

## Step 0 - Preconditions

- ClickHouse MCP server connected (tools `run_query`, `list_databases`, `list_tables`) and running with the support guard (`MCP_MIDDLEWARE_MODULE=mcp_clickhouse.support_guard.middleware`). If a query without a time bound succeeds, the guard is off: tell the user and keep every query tightly bounded yourself.
- The chatomate repository is open in the session for code lookups. Read its `CLAUDE.md` and the matching `docs/` file for the area (see `references/chatomate-map.md`).
- Production MongoDB access is optional. If a MongoDB MCP server is connected, use it read-only. Otherwise ask the user to run the lookup and paste the IDs.

## Step 1 - Intake (ask once, never re-ask)

Collect, in one message, whatever is missing:

| Input | Why | Example |
|---|---|---|
| Symptom | what the customer saw, exact error text if any | "Template send API returns 500" |
| Environment and namespace | picks the database and namespace | `chatomate` / `prod-chatomate`, `ttl` / `prod-chatomate-ttl`, `netcore` / `prod-netcore`, `beta-chatomate` |
| When | approximate time **with time zone** (assume IST if the ticket is from Indian support and say so) | 06-11-2026 14:30 IST |
| Who | at least one identifier: bot name or ID, customer phone, botUserId, campaign name or ID, API key owner, handoff ID, WABA ID | "bot Acme Support, customer +91 98xxxxxx10" |
| Surface | backend (API, flow, campaign, callback, report) or frontend (panel screen) | |

If the time is vague ("this morning"), ask for the narrowest range the reporter can give. Do not guess the environment from the bot name.

## Step 2 - Resolve identifiers before touching logs

Logs are searched by IDs, not by names. Turn what the ticket gives you into IDs first:

| Ticket gives | Look up | Collection and fields |
|---|---|---|
| Bot name | bot `_id` | `bots`: `{ name: /<name>/i, isArchived: { $ne: true } }` -> `_id`, `platforms`, `status` |
| Customer phone / channel user | bot user | `botusers`: `{ botId: ObjectId(<botId>), botUserId: /<last 10 digits>$/ }` -> `_id`, `botUserId`, `botChannel` |
| Campaign name | campaign `_id` | `campaigns`: `{ botId, campaignName: /<name>/i }` -> `_id`, `status`, `channel`, `executionTime` |
| Live-chat issue | handoff | `handoffs`: `{ botId, botUserId: ObjectId(<botUser _id>) }` sorted by `createdAt` desc |
| Template issue | template | `message-templates` / `template-messages` by `botId` and template name |

Always use a projection and `limit(5)`. If several bots match, ask the user which one. Write down every resolved ID: they become your search tokens.

## Step 3 - Read the code path (5 minutes, not 50)

Before searching logs, find where this behaviour lives so you know which service logs it and which log messages to expect:

1. Map the symptom to a service and entry point with `references/chatomate-map.md` and the doc-first routing table in chatomate's `CLAUDE.md`.
2. Grep the controller, consumer or step handler. Note the **exact log message strings** (`this.logger.error(..., 'Error while ...')`) and the structured fields logged with them (`botId`, `botUserId`, `campaignId`, `phoneNumber`, `messageId`).
3. Frontend tickets: follow `references/frontend.md` to get from the screen to the backend endpoint. The browser has no server logs. The API it calls does.

## Step 4 - Find one verifiable anchor log (delegate)

Spawn the `log-investigator` subagent with a brief like:

```
Environment: prod-chatomate (database cm_otel), table otel_logs
Window: 2026-11-06 14:15 to 14:45 Asia/Kolkata
Services: ikit, platform-apis (from code: TemplateSendService)
Tokens: botId 64f1c0ffee0123456789abcd, phone 9198xxxxxx10
Expected messages: "Error while authenticating whatsapp template"
Goal: find the anchor log, then its request/trace id, then the full request timeline.
```

The subagent follows `references/log-search.md`: discover, then histogram, then fetch a few rows, then expand by `TraceId` / `req.id`. It returns the anchor line, the trace or request id, a timeline of 10 to 30 condensed lines, and the error text. If it finds nothing, it says which windows and tokens it tried. Change one thing (window, service, token) and send it back, at most three rounds before you ask the user for better data.

## Step 5 - Correlate logs with code

- Put each important log line next to the code line that writes it (`file:line`).
- Explain the path: what input arrived, which branch ran, where it failed, why.
- Check recent changes on that path: `git log -p --since=<a week before the incident> -- <files>`.
- Separate **evidence** (seen in logs or code) from **inference** (likely, not proven). Never present inference as fact.

## Step 6 - Root cause and fix

Weigh these classes of cause and say why the rejected ones do not fit the evidence:

- Code bug (logic, missing null check, wrong branch, unhandled rejection)
- Data or configuration (bot or channel misconfigured, template not approved, expired Meta token, wrong field mapping)
- External dependency (Meta / RCS / SMS provider, customer webhook, LLM provider, timeouts)
- Infrastructure (Redis, Mongo, RabbitMQ, Kafka, pod restarts, rate limits)
- Expected behaviour (opt-out, messaging limits, 24-hour window, user error): give the support team the customer-facing explanation

Backend specifics are in `references/backend.md`.

## Step 7 - Write the summary

Create `support-investigations/<YYYY-MM-DD>-<short-slug>.md` in the working directory:

1. **Ticket**: symptom, environment, window, identifiers (IDs, not secrets)
2. **Verdict**: bug / configuration / external / expected behaviour, with confidence (high / medium / low)
3. **Evidence**: anchor log (timestamp, service, condensed message), trace or request id, the timeline excerpt
4. **Code path**: `file:line` for each step, what goes wrong and why
5. **Fix**: exact change with file paths, or the configuration step, or the customer-facing reply
6. **Queries used**: the final ClickHouse queries, so someone else can re-run them
7. **Open questions**: what could not be confirmed

Keep it under two screens. Then tell the user the verdict in three lines and the path to the file.

## Limits

- At most 10 log or analysis rounds. Then stop, summarise what you know, and ask whether to continue.
- Never widen a query past the guard limits by splitting it into many back-to-back windows. If an hour of focused searching found nothing, the identifiers or the time are probably wrong. Go back to the user.
- Never run write queries, and never suggest changing production data as part of an investigation without saying it needs approval.

## Next step

If the verdict is a code bug, run `/kv-technical-analysis` in the chatomate repo with the summary file as input to plan the fix.

## Rate this run

`/rate-skill kv-tech-support-investigation <1-5> "reason"`
