import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "tracegate.db"


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_connection()
    cur = conn.cursor()

    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS bookings (
            id TEXT PRIMARY KEY,
            customer_name TEXT NOT NULL,
            customer_email TEXT NOT NULL,
            service_type TEXT NOT NULL,
            amount REAL NOT NULL,
            status TEXT NOT NULL,
            payment_status TEXT NOT NULL,
            idempotency_key TEXT UNIQUE,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS api_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            method TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            status_code INTEGER NOT NULL,
            latency_ms REAL NOT NULL,
            fault_active TEXT,
            payload_summary TEXT,
            error_detail TEXT
        );

        CREATE TABLE IF NOT EXISTS support_tickets (
            id TEXT PRIMARY KEY,
            booking_id TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            severity TEXT NOT NULL,
            status TEXT NOT NULL,
            resolution_note TEXT,
            created_at TEXT NOT NULL
        );
        """
    )

    existing = cur.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]
    if existing == 0:
        now = datetime.now(timezone.utc).isoformat()
        cur.executemany(
            """
            INSERT INTO bookings (id, customer_name, customer_email, service_type, amount, status, payment_status, idempotency_key, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("BK-1001", "Arjun Nair", "arjun.nair@example.com", "Full Vehicle Service", 2499.0, "CONFIRMED", "PAID", "idem-1001", now),
                ("BK-1002", "Meera Menon", "meera.m@example.com", "Periodic Maintenance", 1850.0, "PENDING_PAYMENT", "DEDUCTED_WEBHOOK_MISSED", "idem-1002", now),
                ("BK-1003", "Rahul Krishnan", "rahul.k@example.com", "AC Diagnostics", 999.0, "FAILED", "TIMEOUT_504", "idem-1003", now),
            ],
        )

        cur.executemany(
            """
            INSERT INTO support_tickets (id, booking_id, title, description, severity, status, resolution_note, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "TCK-501",
                    "BK-1002",
                    "Money deducted via UPI, booking still shows Pending",
                    "Customer reports Rs. 1850 debited at gateway, but booking BK-1002 never transitioned to CONFIRMED.",
                    "HIGH",
                    "OPEN",
                    None,
                    now,
                ),
                (
                    "TCK-502",
                    "BK-1003",
                    "Gateway 504 timeout during checkout",
                    "Payment gateway timed out during confirmation callback for booking BK-1003.",
                    "MEDIUM",
                    "OPEN",
                    None,
                    now,
                ),
            ],
        )

        cur.execute(
            """
            INSERT INTO api_logs (timestamp, method, endpoint, status_code, latency_ms, fault_active, payload_summary, error_detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                "POST",
                "/api/payments/webhook",
                504,
                3012.4,
                "payment_timeout",
                '{"booking_id": "BK-1002", "event": "payment.captured"}',
                "Upstream payment webhook timed out before updating booking state",
            ),
        )

    conn.commit()
    conn.close()


def run_readonly_sql(query: str) -> dict:
    cleaned = query.strip().rstrip(";")
    if not cleaned.lower().startswith("select"):
        raise ValueError("Only read-only SELECT diagnostic queries are permitted in the Support Console.")

    forbidden = ["insert ", "update ", "delete ", "drop ", "alter ", "pragma ", "attach "]
    lowered = cleaned.lower()
    if any(word in lowered for word in forbidden):
        raise ValueError("Unsafe SQL keyword blocked by TraceGate read-only guard.")

    conn = get_connection()
    try:
        cur = conn.execute(cleaned)
        columns = [desc[0] for desc in cur.description] if cur.description else []
        rows = [dict(row) for row in cur.fetchall()]
        return {"columns": columns, "rows": rows, "row_count": len(rows)}
    finally:
        conn.close()
