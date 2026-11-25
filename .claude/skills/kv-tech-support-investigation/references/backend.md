# Backend investigation notes

Use with SKILL.md steps 3 to 6 when the ticket is about an API, a bot flow, a campaign, a callback, a report or a background job.

## Stack you are reading

- NestJS services in an Nx monorepo: controllers, services, guards, interceptors, pipes, `@OnEvent` RabbitMQ consumers via `@chatomate/intercom`, Kafka producers, BullMQ workers (excalibur)
- MongoDB through Mongoose repositories in `libs/models/*`, Redis (ioredis) for state and caches, StarRocks for analytics, PostgreSQL for whatsapp-middleware, Qdrant for knowledge bases
- Python services: chronos (scheduler) and agentic-ai-knowledge-trainer
- Responses go through `HttpSuccessHandlerService` / `HttpErrorGeneratorService`, so an HTTP error code usually maps to a named error in the service's error map

## Finding the code path fast

1. Route: grep the URL path segment in `apps/<service>/src/**/*.controller.ts` (or `apps/platform/src/routes` for legacy).
2. Queue: grep the route key, `grep -rnE "\b(<ROUTE_KEY>|<route.value>)\b" apps libs --include='*.ts'`. Publishers use `route: IntercomEvents.X`, consumers `@OnEvent(IntercomEvents.X)`.
3. Log message from the anchor: grep the literal `msg` string. It is the shortest path from log to code.
4. Error code in the response: grep it in the service's error map (`*.errors.ts`, `*-errors.ts`).

## Patterns that cause most tickets

- **Unhandled rejection or swallowed error**: an `await` missing inside a loop or a `catch` that logs and continues. Look for the last success log, then the gap.
- **Stale Redis state**: iKit keeps conversation state and published flow steps in Redis. A flow edited but not republished, or a key left from an old session, explains "bot replies with an old message".
- **Async tail**: the HTTP call succeeds, the real work happens later in a queue consumer or callback. Follow the business id into the next service a few seconds to minutes later.
- **Provider errors**: Meta / RCS / SMS responses are often logged as an `err.response.data` object. The provider error code (for example Meta `131047`, `132000`) usually explains everything. Quote it.
- **Configuration**: expired or revoked Meta token, template not approved or paused, channel disabled, messaging limit tier, opt-out list, 24-hour window closed. These are expected behaviour. Say so clearly so support can answer the customer.
- **Environment drift**: TTL and Netcore share the code but not the configuration. Check environment-specific branches (`process.env`, config namespaces, feature flags) before calling it a code bug.

## Cause checklist (rule each one in or out with evidence)

| Class | Evidence that confirms it |
|---|---|
| Code bug | a log line or its absence that only this branch explains, plus the code line |
| Data / config | the Mongo document or bot setting that drives the branch |
| External dependency | the provider response or timeout in logs, with status and code |
| Infrastructure | connection errors, pod restarts (`k8s.pod.name` changing), timeouts across many bots at once |
| Expected behaviour | the rule in code or docs that produces this outcome |

## Proposing the fix

- Code: file path, function, the minimal change, and the regression it risks. Follow chatomate's `.claude/rules/` (repository-only DB access, shared constants in `libs/shared-tokens`, typed errors).
- Configuration: the exact setting and where it is changed (admin panel, app panel, env var), and who owns it.
- Logging gap: if the investigation was slow because something was not logged, propose the single log line (with ids, without secrets) that would have shown it.
