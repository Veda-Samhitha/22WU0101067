# URL Shortener Microservice (FastAPI + SQLite)

## How to run
```bash
pip install fastapi uvicorn "pydantic<3"
python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

> Uses only the Python standard library for algorithms (custom base62 + validation). Storage is SQLite via stdlib `sqlite3`. Custom logging middleware is included.

## Endpoints

### Create short URL
`POST /shorturls`

**Body**
```json
{
  "url": "https://example.com/some/very/long/link",
  "validity": 30,
  "shortcode": "optional-custom-code"
}
```

**Response (201)**
```json
{
  "shortcode": "X9a",
  "original_url": "https://example.com/some/very/long/link",
  "created_at": "2025-08-11T06:00:00+00:00",
  "expiry": "2025-08-11T06:30:00+00:00"
}
```

### Redirect
`GET /{shortcode}` → 307 redirect to original URL  
- Logs timestamp, referrer (if available), **coarse** location via masked IP (/24 for IPv4, /48 for IPv6) and `Accept-Language`.

**Expired:** returns `410 Gone` JSON.

### Stats
`GET /shorturls/{shortcode}`

**Response (200)**
```json
{
  "shortcode": "X9a",
  "original_url": "https://example.com/some/very/long/link",
  "created_at": "...",
  "expiry": "...",
  "total_clicks": 3,
  "clicks": [
    {"ts":"...","referrer":"https://google.com","client_ip_masked":"10.0.0.*","locale_hint":"en-US,en;q=0.9"}
  ]
}
```

## Design / Architecture

- **Framework:** FastAPI (async ASGI), Starlette under the hood.
- **Storage:** SQLite; two tables
  - `shorturls(id, original_url, shortcode UNIQUE, created_at, expiry)`
  - `click_logs(id, shortcode, ts, referrer, client_ip_masked, locale_hint)`
- **Shortcode generation:** No external libraries. Insert row to get `id`, encode `id` to Base62 → `shortcode`. Custom validation for user-supplied `shortcode` (regex `[0-9A-Za-z_-]{4,32}`) + unique index.
- **Logging:** Custom HTTP middleware prints per-request line: method, path, client IP, status, duration. Can be redirected to a file or shipped to ELK.
- **Errors:** Consistent JSON: `{ "error": "...", "message": "..." }` with proper HTTP status codes (404, 409, 410, 422).
- **Security:** No auth by requirement. Only coarse location—no external GeoIP. Avoids PII by masking IPs.
- **TTL / Expiry:** Default 30 minutes; configurable via `validity` minutes (1 to 43200). Expired links return 410.

## Assumptions
- "Coarse location" is satisfied by IP masking and `Accept-Language` header; no external geo lookup permitted.
- No user authentication per constraints.
- Screenshots will be taken by you in Postman/Insomnia against your running server (not a third-party test server).

## Postman quick test (cURL equivalents)

Create a short URL:
```bash
curl -s -X POST http://localhost:8000/shorturls \
 -H "Content-Type: application/json" \
 -d '{"url":"https://example.com/abc?x=1","validity":5}'
```

Follow a redirect (your browser or curl -I):
```bash
curl -I http://localhost:8000/X9a
```

Get stats:
```bash
curl -s http://localhost:8000/shorturls/X9a | jq
```

## API Testing screenshots to capture
1. `POST /shorturls`: request body, 201 response, response time.
2. `GET /{shortcode}`: 307 response (use "Follow redirects" disabled), response time.
3. `GET /shorturls/{shortcode}`: stats JSON, response time.

## Tech Justification
- **FastAPI**: quick development, type-checked models, clear validation → production-friendly.
- **SQLite**: lightweight, transactional, file-based; perfect for a microservice demo; can swap to Postgres by changing the `sqlite3` usage.
- **No external algorithm libs**: Base62 + regex implemented from scratch.
- **Custom middleware**: one place for request logging, extensible for correlation IDs later.
