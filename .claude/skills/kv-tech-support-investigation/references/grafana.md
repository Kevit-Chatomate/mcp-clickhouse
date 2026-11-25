# Grafana log links

Support engineers often need the full log in Grafana: to read more around it, or to share it with another team or a client. Every investigation hands the user a Grafana link next to the exact log lines. The link needs no ClickHouse access, so it is also the main output of skip-logs mode.

## Dashboards

All environments use the same dashboard (uid `f2bfb1fb-1fd8-42be-8267-f51c661f64a4`) on `https://mon.omni.tatatelebusiness.com`, in a different Grafana org:

| Environment | Path and org | `var-Namespace` |
|---|---|---|
| Chatomate prod | `/d/f2bfb1fb-1fd8-42be-8267-f51c661f64a4/logs-chatomate?orgId=2` | `prod-chatomate` |
| Chatomate beta | same as Chatomate prod (not verified) | `beta-chatomate` |
| TTL | `/d/f2bfb1fb-1fd8-42be-8267-f51c661f64a4/logs-smartflo?orgId=1` | `prod-chatomate-ttl` |
| Netcore | not known yet | `prod-netcore` |

For Netcore, or when a link does not open, ask the user for one working dashboard URL and use its path and `orgId`. Never guess an org.

## Parameters

Append to the path, in this order:

| Parameter | Value |
|---|---|
| `from`, `to` | UTC, ISO with milliseconds and `Z`: `2026-11-25T06:30:00.000Z`. Default: the anchor minus 5 minutes to the anchor plus 5 minutes, `from` on a whole minute, `to` at `:59.000Z` of its minute. Convert IST to UTC (IST = UTC + 5:30). |
| `timezone` | `browser` |
| `var-Namespace` | from the table above |
| `var-ServiceName` | one parameter per service, repeated: `&var-ServiceName=ikit&var-ServiceName=platform` |
| `var-AttributeValue` | the dashboard's search value: one identifier from the anchor (the `req.id`, `botUserId` or `messageId`), or empty |
| `var-Severity` | `$__all` (or one level, e.g. `ERROR`) |
| `var-LogWhereClause` | `1` |

URL-encode values that contain spaces or special characters (`encodeURIComponent`); `$__all` stays as it is.

Examples:

```
https://mon.omni.tatatelebusiness.com/d/f2bfb1fb-1fd8-42be-8267-f51c661f64a4/logs-chatomate?orgId=2&from=2026-11-25T06:30:00.000Z&to=2026-11-25T06:39:59.000Z&timezone=browser&var-Namespace=prod-chatomate&var-ServiceName=ikit&var-ServiceName=platform&var-AttributeValue=create&var-Severity=$__all&var-LogWhereClause=1

https://mon.omni.tatatelebusiness.com/d/f2bfb1fb-1fd8-42be-8267-f51c661f64a4/logs-smartflo?orgId=1&from=2026-11-25T06:30:00.000Z&to=2026-11-25T06:35:59.000Z&timezone=browser&var-Namespace=prod-chatomate-ttl&var-ServiceName=external-callbacks-processor&var-AttributeValue=&var-Severity=$__all&var-LogWhereClause=1
```

## What to hand over

- **Anchor link**: the anchor's services, the window around it, `var-AttributeValue` set to the most specific id (the `req.id` or `TraceId` value, else `botUserId`). It should show the anchor and its request.
- **Wide link**: same window and services, `var-AttributeValue=` empty, for the surroundings.
- In skip-logs mode: the same two links built from the ticket's time and the services from the code, with the best identifier from Step 2.
- Service names are the `ServiceName` values in the logs (`references/chatomate-map.md`), not the Nx project names.
