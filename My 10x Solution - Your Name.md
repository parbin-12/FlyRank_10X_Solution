# My 10x Solution — PulseLog

*(Rename this file to `My 10x Solution - {Your Name and Surname}.md` before submitting — I don't have your name, so I've left it as a placeholder.)*

## 1. What problem am I solving?

When something goes wrong in a backend service, the first thing anyone
asks is "what happened this week?" Answering that today means opening a
log file or dashboard, manually counting errors and warnings, spotting the
recurring ones, and writing up a short summary — usually by hand, usually
under time pressure, usually taking 15–20 minutes even for a small
service. If you check on multiple services, that time multiplies.

**Who has this problem:** any backend engineer or team lead who is
responsible for a running service and needs to report on its health —
weekly status updates, on-call handoffs, or just "is this thing okay?"
checks.

**The 10x claim:** PulseLog turns that 15–20 minute manual process into a
single API call that returns a finished PDF report — stats, top recurring
issues, and an AI-written plain-English summary — in under 2 seconds. The
stats themselves are served from a background-refreshed cache, so checking
in on a service's health is instant, not "let me go grep the logs."

**Non-goal:** PulseLog does not ship or collect logs from real running
infrastructure. It assumes log events arrive via a simple `POST /events`
call. Building a real log-shipping agent is explicitly out of scope for
this capstone.

## 2. How did I implement it?

PulseLog is two small services that talk to each other over HTTP:

- **`python-api/`** — a FastAPI service that owns everything stateful: the
  SQLite database, user accounts, log event ingestion, the (deliberately
  expensive) stats aggregation, PDF report generation, and the LLM
  summarization job.
- **`go-cache-cron/`** — a small Go worker with no state of its own. On a
  fixed schedule (a `time.Ticker`, default every 30 seconds) it calls the
  Python API's stats endpoint for each configured service and holds the
  result in memory, guarded by a `sync.RWMutex`. Reads from its own
  `/cache/stats/{id}` endpoint are then instant, no matter how slow the
  underlying aggregation is. It authenticates itself automatically on
  startup by logging into the demo account, so there's no manual
  token-copying step to run the whole stack.

### The 5+ concepts (all 7 core concepts implemented, 0 swaps used)

| Concept | Implementation |
|---|---|
| **API endpoints** | FastAPI routes in `python-api/app/main.py` — registration, login, service management, event ingestion, stats, reports, and LLM summary, all with Pydantic request validation and correct status codes (201 on create, 401 on bad/missing auth, 404 on missing resource, 409 on duplicate email). |
| **Database** | `python-api/app/db.py` — a real SQLite schema (users, services, events, llm_cost_log) with a foreign key and an index on `(service_id, ts)` for the stats query. Data is written to a file on disk, so it survives a process restart — verified by stopping and restarting the API and confirming the demo data was still there. |
| **Authentication** | `python-api/app/auth.py` — passwords are salted and hashed with PBKDF2-HMAC-SHA256 (never stored in plaintext), sessions are stateless JWTs, and a `require_user` FastAPI dependency protects every route that touches user data. I tested this by confirming an unauthenticated request to `/services` returns 401. |
| **Background jobs / cron** | `go-cache-cron/main.go`, function `runCronLoop` — a `time.Ticker` refreshes every configured service's stats on a fixed interval, completely off any user's request path. |
| **Reporting (PDF)** | `python-api/app/reports.py` + `POST /reports/{id}` — builds an actual one-page PDF (via `reportlab`) combining the stats breakdown, top recurring messages, and the AI incident summary. I generated one during testing and confirmed with `file` that it's a valid PDF. |
| **Caching logic** | `go-cache-cron/main.go`, the `statsCache` type — the expensive stats result is computed once per refresh interval and served many times from memory via `/cache/stats/{id}`, with `X-Cache-Status` (`fresh`/`stale`) and `X-Cache-Age-Seconds` response headers so a caller can see exactly how fresh the data is. |
| **LLM integration** | `python-api/app/llm.py` + `POST /llm/summarize` — a narrow job: turn a batch of recent WARN/ERROR log lines into a short plain-English incident summary. Input is capped at 6,000 characters before being sent to the model (validation), and every call is logged to the `llm_cost_log` table with an estimated cost. If no `ANTHROPIC_API_KEY` is configured, it falls back to a deterministic local summarizer so the whole demo still runs end-to-end for free. |

I didn't need any swaps — the seven core concepts from the brief mapped
directly onto the pieces PulseLog actually needed.

### Steps to run it

**Docker (one command):**
```bash
docker compose up --build
```
The API seeds a demo user and 500 realistic demo log events automatically
on first startup. Demo login: `demo@pulselog.dev` / `demo1234`.

**Without Docker:**
```bash
# terminal 1
cd python-api
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000

# terminal 2
cd go-cache-cron
go build -o cachecron .
./cachecron
```

Full walkthrough (login → expensive stats → cached stats → AI summary →
PDF report) is in `README.md` under "5-minute demo path."

### What I'd build next (future ideas, deliberately not built now)

- A real log-shipping agent instead of manual event posting
- Alerting when the ERROR rate crosses a threshold
- Swapping SQLite for Postgres if this ever needed real concurrent writes
