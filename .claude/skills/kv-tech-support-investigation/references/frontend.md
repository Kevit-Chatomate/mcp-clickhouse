# Frontend investigation notes

Use with SKILL.md when the ticket is about a panel screen: something does not load, a button fails, data looks wrong, a builder misbehaves. The browser leaves no server logs, so the work is: find the component, find the API it calls, then investigate that API like a backend ticket.

## Extra intake for frontend tickets

- Which panel: `app-panel` (customers), `admin-panel` (internal), `flow-services-frontend`, Shopify or Zoho embedded apps
- Screen or route, and the exact steps to reproduce
- Console error and the failing network call (URL, status, response body) if support can capture them. A HAR file or screenshot of the network tab is ideal
- Browser and version, user role, consistent or intermittent

## Stack you are reading

- Angular 15 (app-panel) and Angular 19/20 (admin-panel), RxJS service-based state (no NgRx), reactive forms
- Shared design system `@chatomate/ui-components` (`c-` prefix) and `@chatomate/shared-components`
- jsPlumb and panzoom on the bot-builder canvas, ApexCharts for analytics, Socket.io client for live updates
- HttpClient interceptors add the auth token and handle errors centrally

## From screen to backend

1. Route: find the route path in the panel's routing modules, then the component.
2. Component -> service: find the service method behind the failing action (`this.<x>Service.<method>()`).
3. Service -> URL: read the URL it builds and the base URL it uses (platform vs platform-apis vs socket-city).
4. URL -> backend: grep the path in the backend controllers and continue with `backend.md`.
5. Logs: search the backend service for the request in the reported window using `botId` and the URL path (`position(Body, '/v1/segments') > 0`), then expand by `req.id`.

## Frontend-only causes to rule in or out

- Response shape changed: the API now returns a different structure, a renamed field, or `null` where an array was expected. Compare the backend DTO with the component's usage.
- Missing guards: no `?.`, no `Array.isArray`, no empty state, so one bad record breaks the screen.
- RxJS: subscription not torn down (duplicate calls), `switchMap` cancelling a needed call, error not caught so the stream dies after the first failure.
- Role or permission: the API returns 403 for this role and the UI shows a generic error. Check `RESOURCE_MAPPING` and the user's role.
- Cached build or feature flag: ask whether a hard refresh fixes it. Check environment-specific config (TTL vs Chatomate themes and features).
- Socket: live updates missing while a refresh shows correct data points at socket-city or the room the client joined.

## Fix proposal

Give the component and service file paths, the exact change (guard, error handling, mapping), and whether a backend change is needed too. If the root cause is backend, say so and hand over to the backend notes.
