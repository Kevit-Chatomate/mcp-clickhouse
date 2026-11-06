# Chatomate map for support investigations

Where a symptom usually lives, what the service is called in the logs, and which identifiers it logs. Service names in ClickHouse come from the Kubernetes deployment, not the Nx project, so **confirm them with the discovery query** in `log-search.md` before relying on this table. Names seen in production logs in the past are marked with a check.

## Environments

| Environment | Namespace (`ResourceAttributes['k8s.namespace.name']`) | Notes |
|---|---|---|
| Chatomate prod | `prod-chatomate` | database `cm_otel` |
| Chatomate beta | `beta-chatomate` | same database, filter by namespace |
| TTL | `prod-chatomate-ttl` | same code, roughly 10x the log volume: keep windows short |
| Netcore | `prod-netcore` | some services are prefixed, e.g. `prod-netcore-wa-callback-processor` |

Confirm the database for TTL and Netcore with `list_databases`. Add a namespace filter whenever several environments share one table.

## Symptom -> service -> log name

| Symptom | Service (chatomate path) | Likely `ServiceName` | Identifiers in its logs | Read first |
|---|---|---|---|---|
| Bot did not reply, wrong flow step, flow stuck | iKit `apps/ikit` | `ikit` (check) | `botId`, `botUserId`, channel user id or phone | `docs/flows/ikit-message-processing.md` |
| Panel action failed (legacy CRUD: bots, users, templates, campaigns) | platform `apps/platform` | `platform` (check) | `botId`, `req.id`, user id from JWT | `apps/platform/CLAUDE.md` |
| Panel action failed (newer features, analytics, KB, segments, opt-out) | platform-apis `apps/platform-apis` | `platform-apis` (check) | `botId`, `req.id` | `apps/platform-apis/CLAUDE.md` |
| Campaign stuck, partly sent, wrong counts | excalibur `apps/jobs/excalibur` | `excalibur` (check), `campaign-monitor` (check) | `campaignId`, `botId` | `docs/domain/campaign/` |
| Message status wrong (sent / delivered / read / failed) | wa-callbacks-processor | `wa-callback-processor` (check) | `wamid` / `messageId`, `botId`, phone | `apps/wa-callbacks-processor/CLAUDE.md` |
| RCS status wrong | rcs-callbacks-processor | `rcs-callback-processor` (check) | message id, `botId` | `apps/rcs-callbacks-processor/CLAUDE.md` |
| SMS not sent / status wrong | sms-middleware, sms-callbacks-processor | confirm | DLT template id, phone, `campaignId` | `apps/sms/sms-middleware/CLAUDE.md` |
| WhatsApp Cloud API errors, template send rejected by Meta | whatsapp-middleware `apps/whatsapp/whatsapp-middleware` | confirm (`wa-proxy` (check) is the older name) | `wabaId`, phone number id, `wamid` | `apps/whatsapp/whatsapp-middleware/CLAUDE.md` |
| Partner (BSP) API call failed | bsp-api-proxy | confirm | API key owner, `req.id` | `apps/bsp-api-proxy/CLAUDE.md` |
| Customer public API (segments, campaigns, SMS send) failed | public-apis | `public-service-apis` (check) or confirm | API key id, `req.id` | `docs/domain/public-apis.md` |
| API step in a flow failed | api-tool-proxy, external-callbacks-processor | `api-tool-proxy` (check), `external-callbacks-processor` (check) | `botId`, target URL host | `apps/api-tool-proxy/CLAUDE.md` |
| Live-chat updates not reaching agents | socket-city | `socket-city` (check) | `botId`, agent user id, `handoffId` | `docs/domain/live-chat/` |
| Scheduled job did not fire | chronos (Python) | `chronos` (check) | job id, destination queue | `apps/chronos/CLAUDE.md` |
| Report not generated / not emailed | reporter, wa-reporter, sms-reporter | `reporter` (check), confirm others | `reportId`, `botId` | `docs/domain/reports/` |
| Template sync, config sync from Meta | sync-bridge | `sync-bridge` (check) | `botId`, `wabaId` | `apps/jobs/sync-bridge/CLAUDE.md` |
| Knowledge base training / AI answers | agentic-ai-knowledge-trainer (Python), platform-apis, ikit | `agentic-ai-knowledge-trainer` (check) | `botId`, KB id | `docs/domain/agentic-ai/` |
| Short link / click tracking | ichnaea | `ichnaea` (check) | short code | `apps/ichnaea/CLAUDE.md` |
| Shopify, MoEngage, Zoho, Leadmastro | integration services | `shopify` (check), `moengage-integration` (check), `leadmastro-integration` (check) | shop domain, `botId` | `docs/domain/integrations.md` |
| Instagram / social publishing | insta-publisher, social-media-publisher | `instagram-publisher` (check) | post id, `botId` | |

## What the logs look like

- NestJS services log with `nestjs-pino`. In production each line is JSON with `level` (number: 20 debug, 30 info, 40 warn, 50 error), `time`, `msg`, and whatever object was passed (`{ botId, err, data }`).
- During an HTTP request every line also carries the request: `req.id` (UUID), `req.method`, `req.url`, and **`req.headers`, including `authorization`**. That is why tokens appear in logs. The MCP server redacts them. The permanent fix is a pino `redact` config in each service.
- Errors are usually logged as `{ err }` or `{ error }` with a stack. The `msg` is the string to grep in code.
- Queue consumers (RabbitMQ `@OnEvent`, Kafka, BullMQ) have no `req`. Follow them with business ids.
- The legacy `platform` service uses its own pino logger (`apps/platform/src/services/logger.ts`).

## Common identifier fields in log objects

`botId`, `botUserId`, `campaignId`, `phoneNumber`, `handoffId`, `wabaId`, `messageId`, `groupId`, `reportId`, `userId`, `channel`. A WhatsApp `botUserId` is usually the customer's phone number with country code.
