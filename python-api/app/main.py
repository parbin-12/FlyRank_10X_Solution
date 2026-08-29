"""
PulseLog API — Concept #1: API endpoints (real HTTP API, correct status
codes, request validation via Pydantic).

Run: uvicorn app.main:app --reload --port 8000
"""
import time

from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, EmailStr, Field
import io

from . import auth, db, llm, reports

app = FastAPI(title="PulseLog API", version="1.0.0")


@app.on_event("startup")
def on_startup():
    db.init_db()
    db.seed_demo_data()


# ---------- schemas ----------
class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class ServiceCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class EventCreateRequest(BaseModel):
    service_id: int
    level: str = Field(pattern="^(INFO|WARN|ERROR)$")
    message: str = Field(min_length=1, max_length=500)


# ---------- auth ----------
@app.post("/auth/register", status_code=201)
def register(req: RegisterRequest):
    with db.db() as conn:
        existing = conn.execute("SELECT id FROM users WHERE email = ?", (req.email,)).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="Email already registered")
        pw_hash, salt = auth.hash_password(req.password)
        conn.execute(
            "INSERT INTO users (email, password_hash, salt, created_at) VALUES (?, ?, ?, ?)",
            (req.email, pw_hash, salt, time.time()),
        )
    return {"message": "registered"}


@app.post("/auth/login")
def login(req: LoginRequest):
    with db.db() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (req.email,)).fetchone()
    if not row or not auth.verify_password(req.password, row["salt"], row["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    token = auth.issue_token(row["id"], row["email"])
    return {"access_token": token, "token_type": "bearer"}


# ---------- services ----------
@app.post("/services", status_code=201)
def create_service(req: ServiceCreateRequest, user=Depends(auth.require_user)):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO services (owner_id, name, created_at) VALUES (?, ?, ?)",
            (user["id"], req.name, time.time()),
        )
        service_id = conn.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    return {"id": service_id, "name": req.name}


@app.get("/services")
def list_services(user=Depends(auth.require_user)):
    with db.db() as conn:
        rows = conn.execute("SELECT id, name FROM services WHERE owner_id = ?", (user["id"],)).fetchall()
    return [dict(r) for r in rows]


# ---------- events (ingestion) ----------
@app.post("/events", status_code=201)
def create_event(req: EventCreateRequest, user=Depends(auth.require_user)):
    with db.db() as conn:
        svc = conn.execute(
            "SELECT id FROM services WHERE id = ? AND owner_id = ?", (req.service_id, user["id"])
        ).fetchone()
        if not svc:
            raise HTTPException(status_code=404, detail="Service not found")
        conn.execute(
            "INSERT INTO events (service_id, level, message, ts) VALUES (?, ?, ?, ?)",
            (req.service_id, req.level, req.message, time.time()),
        )
    return {"message": "event recorded"}


# ---------- stats (this is the "expensive" endpoint the Go layer caches) ----------
@app.get("/stats/{service_id}")
def get_stats(service_id: int, user=Depends(auth.require_user)):
    with db.db() as conn:
        svc = conn.execute(
            "SELECT id, name FROM services WHERE id = ? AND owner_id = ?", (service_id, user["id"])
        ).fetchone()
        if not svc:
            raise HTTPException(status_code=404, detail="Service not found")
        cutoff = time.time() - 7 * 24 * 3600
        rows = conn.execute(
            "SELECT level, message FROM events WHERE service_id = ? AND ts >= ?", (service_id, cutoff)
        ).fetchall()
    # Deliberately unoptimized in-Python aggregation over the row set, standing in for
    # "expensive to compute" — this is what benefits from the Go caching layer.
    stats = reports.compute_stats(rows)
    return {"service": svc["name"], **stats}


# ---------- LLM ----------
class SummarizeRequest(BaseModel):
    service_id: int


@app.post("/llm/summarize")
def summarize(req: SummarizeRequest, user=Depends(auth.require_user)):
    with db.db() as conn:
        svc = conn.execute(
            "SELECT id, name FROM services WHERE id = ? AND owner_id = ?", (req.service_id, user["id"])
        ).fetchone()
        if not svc:
            raise HTTPException(status_code=404, detail="Service not found")
        cutoff = time.time() - 24 * 3600
        rows = conn.execute(
            "SELECT level, message FROM events WHERE service_id = ? AND ts >= ? AND level IN ('WARN','ERROR')",
            (req.service_id, cutoff),
        ).fetchall()
    lines = [f"[{r['level']}] {r['message']}" for r in rows]
    result = llm.summarize_incidents(lines, service_id=req.service_id)
    return result


# ---------- PDF report ----------
@app.post("/reports/{service_id}")
def generate_report(service_id: int, user=Depends(auth.require_user)):
    with db.db() as conn:
        svc = conn.execute(
            "SELECT id, name FROM services WHERE id = ? AND owner_id = ?", (service_id, user["id"])
        ).fetchone()
        if not svc:
            raise HTTPException(status_code=404, detail="Service not found")
        cutoff = time.time() - 7 * 24 * 3600
        rows = conn.execute(
            "SELECT level, message FROM events WHERE service_id = ? AND ts >= ?", (service_id, cutoff)
        ).fetchall()
        err_rows = conn.execute(
            "SELECT level, message FROM events WHERE service_id = ? AND ts >= ? AND level IN ('WARN','ERROR')",
            (service_id, time.time() - 24 * 3600),
        ).fetchall()

    stats = reports.compute_stats(rows)
    lines = [f"[{r['level']}] {r['message']}" for r in err_rows]
    llm_result = llm.summarize_incidents(lines, service_id=service_id)
    pdf_bytes = reports.build_report_pdf(svc["name"], stats, llm_result["summary"])

    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="pulselog-report-{svc["name"]}.pdf"'},
    )


@app.get("/health")
def health():
    return {"status": "ok"}
