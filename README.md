# PulseLog

**FlyRank Internship — Backend Track — Praveen Yadav**

PulseLog takes raw service logs and turns them into a status report you can actually trust. You point it at a service's events (INFO/WARN/ERROR), and it hands back uptime-style stats, an AI-written incident summary, and a one-click PDF — basically the report a backend engineer usually has to piece together by hand once a week.

**The 10x claim:** doing this by hand — grepping, counting, skimming, writing it all up — takes about **20 minutes**. PulseLog spits out the same report in **under 2 seconds** (`POST /reports/1` → PDF), and the stats behind it are served **instantly** from cache the rest of the time instead of being recomputed on every request.

## Architecture

Two small services, on purpose — this is meant to be a walking skeleton, not a microservices showcase:

```
┌─────────────────┐         ┌──────────────────────┐
│  python-api/     │  HTTP   │  go-cache-cron/       │
│  FastAPI + SQLite│◄────────│  background job that  │
│  auth, ingestion,│  GET    │  polls the "expensive"│
│  PDF, LLM        │ /stats  │  stats endpoint on a  │
│                  │         │  schedule and serves  │
│  :8000           │         │  it from memory       │
│                  │         │  :9000                │
└─────────────────┘         └──────────────────────┘
```

- **`python-api/`** — this is the system of record. It owns the database, auth, log ingestion, the expensive stats aggregation, the LLM summary job, and PDF report generation.
- **`go-cache-cron/`** — a background worker with no state of its own. Every `PULSELOG_REFRESH_SECONDS` (default 30s) it hits the Python API's `/stats/{id}` endpoint for each configured service and caches the result in memory, so reads from `/cache/stats/{id}` come back instantly no matter how slow the underlying aggregation is. It logs itself into the API on startup using the demo account, so there's no manual token-copying involved.

## Concepts implemented (all 7 core concepts, 0 swaps — 2 more than required)

| # | Concept | Where it lives |
|---|---------|-----------------|
| 1 | API endpoints | `python-api/app/main.py` — FastAPI routes, Pydantic validation, real status codes (201/401/404/409) |
| 2 | Database | `python-api/app/db.py` — SQLite (stdlib `sqlite3`), schema + indexes, survives restart |
| 3 | Authentication | `python-api/app/auth.py` — PBKDF2 password hashing, JWT sessions, `require_user` dependency protects every non-public route |
| 4 | Background jobs / cron | `go-cache-cron/main.go` (`runCronLoop`) — `time.Ticker` refreshes every service's stats on a fixed schedule, off the request path |
| 5 | Reporting (PDF) | `python-api/app/reports.py` + `POST /reports/{id}` — generates a real PDF with `reportlab` |
| 6 | Caching logic | `go-cache-cron/main.go` (`statsCache`) — the expensive `/stats/{id}` result is stored and served from memory via `/cache/stats/{id}`, with `X-Cache-Status` / `X-Cache-Age-Seconds` headers |
| 7 | LLM integration | `python-api/app/llm.py` + `POST /llm/summarize` — a narrow job (log lines → incident summary), with input-length validation, and cost logged to the `llm_cost_log` table |

No swaps were needed here — the seven core concepts mapped cleanly onto the problem, so I didn't end up touching the second table in the brief.

## Non-goal

PulseLog isn't trying to be a log *shipping* pipeline — no agents, no Kafka, no tailing logs off real servers. It just assumes events show up via a simple `POST /events` call. A real log shipper is future work, not something this capstone covers.

## Run it — Docker (recommended, one command)

```bash
docker compose up --build
```

Then:
- API docs: http://localhost:8000/docs
- Cached stats: http://localhost:9000/cache/stats/1
- Cache dashboard: http://localhost:9000/cache/status

The API seeds a demo user and 500 realistic demo events on first startup, so there's nothing to set up by hand.

Demo login: `demo@pulselog.dev` / `demo1234`

## Run it — without Docker

**1. Python API**
```bash
cd python-api
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

**2. Go cache/cron worker** (in a second terminal)
```bash
cd go-cache-cron
go build -o cachecron .
./cachecron
```
It'll auto-login with the demo account and start polling `/stats/1` every 30s.

## 5-minute demo path

1. Start both services (Docker or manual, above).
2. Open http://localhost:8000/docs, `POST /auth/login` with the demo credentials, and copy the `access_token`.
3. Click "Authorize" in the Swagger UI, paste in `Bearer <token>`.
4. Call `GET /stats/1` — this is the "expensive" aggregation.
5. Call `GET http://localhost:9000/cache/stats/1` in a browser or with curl — same data, but served instantly from the Go cache. Check the `X-Cache-Status` response header to confirm.
6. Call `POST /llm/summarize` with `{"service_id": 1}` to see the AI incident summary (this works with no API key via a local fallback — set `ANTHROPIC_API_KEY` if you want a real LLM-written summary).
7. Call `POST /reports/1` to download a PDF report that combines the stats with the AI summary.

## Environment variables

| Variable | Used by | Default | Purpose |
|---|---|---|---|
| `PULSELOG_JWT_SECRET` | python-api | dev value | Signs session JWTs — **set a real secret in production** |
| `PULSELOG_DB_PATH` | python-api | `./pulselog.db` | Where the SQLite file lives |
| `ANTHROPIC_API_KEY` | python-api | unset | If set, `/llm/summarize` calls the real Anthropic API instead of the local fallback |
| `PULSELOG_API_BASE` | go-cache-cron | `http://localhost:8000` | Where to find the Python API |
| `PULSELOG_API_TOKEN` | go-cache-cron | unset | Bearer token to use; if unset, the service logs in itself |
| `PULSELOG_API_EMAIL` / `PULSELOG_API_PASSWORD` | go-cache-cron | demo account | Used for self-login when no token is set |
| `PULSELOG_SERVICE_IDS` | go-cache-cron | `1` | Comma-separated service IDs to keep cached |
| `PULSELOG_REFRESH_SECONDS` | go-cache-cron | `30` | Cron interval |
| `PULSELOG_CACHE_PORT` | go-cache-cron | `9000` | Port for the cache HTTP server |

No secrets are committed — see `.gitignore`. Feel free to copy the table above into a local `.env` if you want to override the defaults; none of it is required to just run the demo.

## Tests

There's no automated test suite in this capstone (it wasn't one of the 5+ concepts I chose to build) — the "5-minute demo path" above and the `/docs` Swagger UI are the manual verification path instead. A natural next step (see "Future ideas") would be a `pytest` suite around `auth.py` and `reports.py`, plus a Go test for the cache expiry logic.

## Future ideas (explicitly out of scope for this capstone)

- Real log shipping agent instead of manual `POST /events`
- Multi-tenant service groups / teams, not just per-user services
- Alerting (push a Slack message when the ERROR rate crosses a threshold)
- Swap SQLite for Postgres for real concurrent-write workloads