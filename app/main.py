import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.database import get_connection, init_db, run_readonly_sql

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="TraceGate — QA, API & Support Triage Sandbox", version="1.0.0")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

FAULT_STATE = {
    "payment_timeout": False,
    "drop_webhook": False,
    "force_500_booking": False,
    "strict_rate_limit": False,
}

REQUEST_TIMESTAMPS: list[float] = []


class BookingRequest(BaseModel):
    customer_name: str = Field(..., min_length=2, max_length=80)
    customer_email: str = Field(..., pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    service_type: str = Field(..., min_length=2, max_length=80)
    amount: float = Field(..., gt=0, le=100000)
    idempotency_key: Optional[str] = None


class WebhookPayload(BaseModel):
    booking_id: str
    event: str
    transaction_id: str


class SqlDiagnosticRequest(BaseModel):
    query: str


class FaultUpdateRequest(BaseModel):
    payment_timeout: Optional[bool] = None
    drop_webhook: Optional[bool] = None
    force_500_booking: Optional[bool] = None
    strict_rate_limit: Optional[bool] = None


def log_api_event(
    method: str,
    endpoint: str,
    status_code: int,
    latency_ms: float,
    payload_summary: str = "",
    error_detail: Optional[str] = None,
) -> None:
    active_faults = [k for k, v in FAULT_STATE.items() if v]
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO api_logs (timestamp, method, endpoint, status_code, latency_ms, fault_active, payload_summary, error_detail)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            datetime.now(timezone.utc).isoformat(),
            method,
            endpoint,
            status_code,
            round(latency_ms, 2),
            ",".join(active_faults) if active_faults else "NONE",
            payload_summary[:300],
            error_detail,
        ),
    )
    conn.commit()
    conn.close()


@app.on_event("startup")
def startup_event() -> None:
    init_db()


@app.get("/")
def serve_dashboard() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health_check() -> dict:
    return {"status": "healthy", "service": "TraceGate", "faults": FAULT_STATE}


@app.get("/api/faults")
def get_faults() -> dict:
    return FAULT_STATE


@app.post("/api/faults")
def update_faults(payload: FaultUpdateRequest) -> dict:
    for key, val in payload.model_dump(exclude_none=True).items():
        FAULT_STATE[key] = val
    log_api_event("POST", "/api/faults", 200, 1.2, str(FAULT_STATE))
    return FAULT_STATE


@app.post("/api/bookings", status_code=201)
def create_booking(booking: BookingRequest, request: Request):
    start = time.perf_counter()

    if FAULT_STATE["strict_rate_limit"]:
        now_ts = time.time()
        REQUEST_TIMESTAMPS[:] = [t for t in REQUEST_TIMESTAMPS if now_ts - t < 10]
        REQUEST_TIMESTAMPS.append(now_ts)
        if len(REQUEST_TIMESTAMPS) > 3:
            latency = (time.perf_counter() - start) * 1000
            log_api_event("POST", "/api/bookings", 429, latency, booking.model_dump_json(), "Rate limit exceeded (429)")
            raise HTTPException(status_code=429, detail="Rate limit exceeded. Too many booking attempts.")

    if FAULT_STATE["force_500_booking"]:
        latency = (time.perf_counter() - start) * 1000
        log_api_event("POST", "/api/bookings", 500, latency, booking.model_dump_json(), "Simulated DB connection failure")
        raise HTTPException(status_code=500, detail="Simulated internal server error while persisting booking.")

    conn = get_connection()
    try:
        if booking.idempotency_key:
            existing = conn.execute(
                "SELECT * FROM bookings WHERE idempotency_key = ?", (booking.idempotency_key,)
            ).fetchone()
            if existing:
                latency = (time.perf_counter() - start) * 1000
                log_api_event("POST", "/api/bookings", 200, latency, booking.model_dump_json(), "Idempotent replay returned existing record")
                return JSONResponse(status_code=200, content=dict(existing))

        booking_id = f"BK-{uuid.uuid4().hex[:6].upper()}"
        created_at = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            INSERT INTO bookings (id, customer_name, customer_email, service_type, amount, status, payment_status, idempotency_key, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                booking_id,
                booking.customer_name,
                booking.customer_email,
                booking.service_type,
                booking.amount,
                "PENDING_PAYMENT",
                "AWAITING_WEBHOOK",
                booking.idempotency_key,
                created_at,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
        latency = (time.perf_counter() - start) * 1000
        log_api_event("POST", "/api/bookings", 201, latency, booking.model_dump_json())
        return dict(row)
    finally:
        conn.close()


@app.post("/api/payments/webhook")
def process_payment_webhook(payload: WebhookPayload):
    start = time.perf_counter()
    conn = get_connection()
    try:
        booking = conn.execute("SELECT * FROM bookings WHERE id = ?", (payload.booking_id,)).fetchone()
        if not booking:
            latency = (time.perf_counter() - start) * 1000
            log_api_event("POST", "/api/payments/webhook", 404, latency, payload.model_dump_json(), "Booking ID not found")
            raise HTTPException(status_code=404, detail=f"Booking {payload.booking_id} not found")

        if FAULT_STATE["payment_timeout"]:
            conn.execute(
                "UPDATE bookings SET status = ?, payment_status = ? WHERE id = ?",
                ("FAILED", "TIMEOUT_504", payload.booking_id),
            )
            conn.commit()
            latency = (time.perf_counter() - start) * 1000
            log_api_event("POST", "/api/payments/webhook", 504, latency, payload.model_dump_json(), "Payment gateway callback timed out (504)")
            raise HTTPException(status_code=504, detail="Gateway timeout while confirming payment callback.")

        if FAULT_STATE["drop_webhook"]:
            conn.execute(
                "UPDATE bookings SET status = ?, payment_status = ? WHERE id = ?",
                ("PENDING_PAYMENT", "DEDUCTED_WEBHOOK_MISSED", payload.booking_id),
            )
            ticket_id = f"TCK-{uuid.uuid4().hex[:4].upper()}"
            conn.execute(
                """
                INSERT INTO support_tickets (id, booking_id, title, description, severity, status, resolution_note, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticket_id,
                    payload.booking_id,
                    f"Payment deducted but booking {payload.booking_id} stuck in PENDING",
                    f"Transaction {payload.transaction_id} captured at gateway, webhook dropped in transit.",
                    "HIGH",
                    "OPEN",
                    None,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()
            latency = (time.perf_counter() - start) * 1000
            log_api_event("POST", "/api/payments/webhook", 202, latency, payload.model_dump_json(), "Webhook accepted but state transition dropped")
            return {"status": "accepted_with_lag", "warning": "Webhook dropped; support ticket auto-created", "ticket_id": ticket_id}

        if payload.event == "payment.captured":
            conn.execute(
                "UPDATE bookings SET status = ?, payment_status = ? WHERE id = ?",
                ("CONFIRMED", "PAID", payload.booking_id),
            )
            conn.commit()
            latency = (time.perf_counter() - start) * 1000
            log_api_event("POST", "/api/payments/webhook", 200, latency, payload.model_dump_json())
            return {"status": "processed", "booking_id": payload.booking_id, "new_state": "CONFIRMED"}

        raise HTTPException(status_code=400, detail="Unsupported webhook event type")
    finally:
        conn.close()


@app.post("/api/support/reconcile/{booking_id}")
def reconcile_booking(booking_id: str):
    start = time.perf_counter()
    conn = get_connection()
    try:
        booking = conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
        if not booking:
            raise HTTPException(status_code=404, detail="Booking not found")

        conn.execute(
            "UPDATE bookings SET status = 'CONFIRMED', payment_status = 'RECONCILED_PAID' WHERE id = ?",
            (booking_id,),
        )
        conn.execute(
            """
            UPDATE support_tickets
            SET status = 'RESOLVED', resolution_note = 'Verified gateway capture via SQL & replayed idempotent webhook.'
            WHERE booking_id = ? AND status = 'OPEN'
            """,
            (booking_id,),
        )
        conn.commit()
        latency = (time.perf_counter() - start) * 1000
        log_api_event("POST", f"/api/support/reconcile/{booking_id}", 200, latency, f"Reconciled {booking_id}")
        return {"status": "reconciled", "booking_id": booking_id}
    finally:
        conn.close()


@app.get("/api/overview")
def get_overview():
    conn = get_connection()
    try:
        bookings = [dict(r) for r in conn.execute("SELECT * FROM bookings ORDER BY created_at DESC LIMIT 25").fetchall()]
        tickets = [dict(r) for r in conn.execute("SELECT * FROM support_tickets ORDER BY created_at DESC LIMIT 25").fetchall()]
        logs = [dict(r) for r in conn.execute("SELECT * FROM api_logs ORDER BY id DESC LIMIT 30").fetchall()]
        return {"bookings": bookings, "tickets": tickets, "logs": logs, "faults": FAULT_STATE}
    finally:
        conn.close()


@app.post("/api/support/sql")
def execute_sql_diagnostic(req: SqlDiagnosticRequest):
    try:
        return run_readonly_sql(req.query)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/qa/run-tests")
def run_automated_tests():
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-v", "--tb=short"],
        cwd=str(BASE_DIR),
        capture_output=True,
        text=True,
        timeout=30,
    )
    return {
        "passed": result.returncode == 0,
        "exit_code": result.returncode,
        "output": result.stdout + ("\n" + result.stderr if result.stderr else ""),
    }


@app.post("/api/reset")
def reset_sandbox():
    REQUEST_TIMESTAMPS.clear()
    for k in FAULT_STATE:
        FAULT_STATE[k] = False
    conn = get_connection()
    conn.executescript("DELETE FROM bookings; DELETE FROM support_tickets; DELETE FROM api_logs;")
    conn.commit()
    conn.close()
    init_db()
    return {"status": "reset_complete"}
