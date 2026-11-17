# FAU Parking

A small Python client, REST API, and MCP server for **FAU campus parking
availability**, built by reverse-engineering the official FAU Parking Android
app (ElevenX / ExactPark "navhub" API). See
[`analysis/REVERSE_ENGINEERING.md`](analysis/REVERSE_ENGINEERING.md) for how the
API contract was recovered.

```
fau_parking/
  client.py       # ElevenXClient — HMAC-signed requests + one method per endpoint
  rest_api.py     # FastAPI/uvicorn REST API (JSON proxy, leveled logging)
  mcp_server.py   # MCP server over stdio (JSON-RPC 2.0, no external SDK)
  __init__.py     # package exports
```

## Install

```bash
pip install -r requirements.txt
```

The package is importable as `fau_parking` from the repo root.

## Client

```python
from fau_parking import ElevenXClient, ElevenXError

c = ElevenXClient(site_id="fau", lang="en-CA")

# Discover the campus site_id from coordinates (the app does this at runtime).
site_id = c.discover_site_id(lat=26.3727, lng=-80.1020)

zones   = c.info_zones(site_id)          # zone metadata + occupancy
stalls  = c.status_stalls(site_id)       # live per-stall status
nearby  = c.info_nearby(lat=26.3727, lng=-80.1020, radius=2000)
providers = c.payment_providers(site_id)
```

Every request is HMAC-SHA256 signed (`Authorization` + `X-Timestamp` +
`Cache-Control` headers), exactly as the app signs it.

## REST API

A FastAPI app served by **uvicorn** (ASGI). It wraps `ElevenXClient` behind a
small JSON HTTP API.

```bash
python3 -m fau_parking.rest_api          # http://127.0.0.1:5000
FAU_PORT=8080 python3 -m fau_parking.rest_api
uvicorn fau_parking.rest_api:app --port 8080
```

| Endpoint | Description |
| --- | --- |
| `GET /health` | Liveness check. |
| `GET /sites` | Discover the `site_id` for a campus (from lat/lng). |
| `GET /zones` | `api/v2/info/zones`. |
| `GET /zones/summary` | Normalized per-zone availability. |
| `GET /stalls` | `api/v2/status/stalls`. |
| `GET /stalls/summary` | Normalized per-stall status. |
| `GET /nearby` | `api/v2/info/nearby`. |
| `GET /payment/providers` | `api/v2/payment/providers`. |

Query params: `site_id`, `zone_id` (repeatable), `stall_id`, `lat`, `lng`,
`radius`.

### Basic usage

The examples below use the FAU Boca Raton campus, whose live `site_id` is `2`
and whose main zone is `37`. If you're on another campus, discover your own
`site_id` first (step 2).

**1. Start the server.** It listens on `http://127.0.0.1:5000` by default
(`FAU_PORT` to change the port):

```bash
python3 -m fau_parking.rest_api
```

Leave it running, then issue the requests below from another terminal.

**2. Discover the campus `site_id`** from coordinates (the app does this at
runtime; you only need it once):

```bash
curl -s "http://127.0.0.1:5000/sites?lat=26.3727&lng=-80.1020"
# -> {"site_id":"2","lat":26.3727,"lng":-80.102}
```

**3. Get zones** — zone metadata plus current occupancy (occupied/vacant):

```bash
# Raw api/v2/info/zones payload
curl -s "http://127.0.0.1:5000/zones?site_id=2&zone_id=37"

# Compact, normalized summary (recommended)
curl -s "http://127.0.0.1:5000/zones/summary?site_id=2&zone_id=37"
# -> {"site_id":"2","zones":[{"site_id":2,"zone_id":37,"name":"Florida Atlantic University","occupied":1354,"vacant":2268,...}]}
```

**4. Get zone spots (stalls)** — live per-stall status within a zone:

```bash
# Raw api/v2/status/stalls payload
curl -s "http://127.0.0.1:5000/stalls?site_id=2&zone_id=37"

# Compact, normalized summary (recommended)
curl -s "http://127.0.0.1:5000/stalls/summary?site_id=2&zone_id=37"
# -> {"site_id":"2","stalls":[{...}]}
```

> The upstream API requires a `zone_id` on the zones/stalls endpoints — an
> empty one returns the API's non-JSON landing page, which this server surfaces
> as an HTTP 502. Use a real zone id from step 3 (or from `/nearby`).

Other handy endpoints:

```bash
# Zones near a coordinate (also lists every zone id for the campus)
curl -s "http://127.0.0.1:5000/nearby?lat=26.3727&lng=-80.1020"

# Payment providers for the campus
curl -s "http://127.0.0.1:5000/payment/providers?site_id=2"

# Liveness check
curl -s "http://127.0.0.1:5000/health"
```

### Logging

Verbosity is controlled by the `FAU_LOG_LEVEL` environment variable (default
`INFO`); all standard levels are honored: `DEBUG`, `INFO`, `WARNING`, `ERROR`,
`CRITICAL`.

| Level | What is logged |
| --- | --- |
| `DEBUG` | Resolved query params per endpoint, the raw upstream request/response, and each request's query string. |
| `INFO` | One line per request (method, path, status, latency) plus per-endpoint result summaries and the server start line. |
| `WARNING` | Non-fatal anomalies — e.g. the upstream API returning its non-JSON "Welcome to nav hub" landing page (surfaced as HTTP 502 rather than crashing). |
| `ERROR` | Upstream HTTP errors and unhandled exceptions. |
| `CRITICAL` | Fatal misconfiguration. |

```bash
FAU_LOG_LEVEL=DEBUG python3 -m fau_parking.rest_api
```

## MCP server

The MCP server speaks JSON-RPC 2.0 over stdio and needs **no external SDK**
(the `mcp` package is optional). Run it as an MCP client would:

```bash
python3 -m fau_parking.mcp_server
# optional: --site-id fau --lang en-CA
```

Tools exposed:

| Tool | Maps to |
| --- | --- |
| `fau_parking_discover_site` | resolve `site_id` |
| `fau_parking_zones` | `api/v2/info/zones` |
| `fau_parking_zones_summary` | normalized per-zone availability |
| `fau_parking_stalls` | `api/v2/status/stalls` |
| `fau_parking_stalls_summary` | normalized per-stall status |
| `fau_parking_nearby` | `api/v2/info/nearby` |
| `fau_parking_payment_providers` | `api/v2/payment/providers` |

### Example MCP session

```bash
$ python3 -m fau_parking.mcp_server
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{}}}
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"fau_parking_zones_summary","arguments":{"site_id":"fau"}}}
```

### Registering with a client (Claude / Claude Code)

Add to your MCP client config:

```json
{
  "mcpServers": {
    "fau-parking": {
      "command": "python3",
      "args": ["-m", "fau_parking.mcp_server"],
      "env": { "FAU_SITE_ID": "fau" }
    }
  }
}
```

## Notes

* `site_id` is **dynamic** — the app discovers it from `api/v2/info/nearby`.
  The default `"fau"` (FAU Boca Raton) is a convenience fallback; override it
  with `site_id=...`, `--site-id`, or the `FAU_SITE_ID` env var.
* Signing is time-sensitive: the `X-Timestamp` nonce must be current or the
  server rejects the request.
* The app also pushes live stall updates over Pusher (channel
  `zone-{siteId}-{zoneId}`, event `parking.status`); the clients here poll
  rather than subscribe.
