# TraceGate — QA, API Reliability & Support Triage Sandbox

TraceGate is a hands-on, production-grade sandbox designed to simulate distributed transaction lifecycle failures, asynchronous webhook edge cases, and L1/L2 support triage workflows. Built with **FastAPI**, **SQLite**, and a dark-mode **Single-Page Command Center UI**, it demonstrates core competencies in:

- **Manual QA Testing & Edge Case Simulation**: Interactive fault injection and webhook lifecycle manipulation.
- **Automated QA Engineering**: Comprehensive test coverage using `pytest` and `fastapi.testclient.TestClient`.
- **API Security & Guardrails**: Read-only SQL diagnostic sandboxing with AST/keyword injection protection.
- **Application Support & L1/L2 Triage**: Auto-ticketing, log correlation, state reconciliation, and root-cause analysis for ghost payments and dropped callbacks.

---

## Architecture Overview

```
+----------------------------------------------------------------------------------+
|                     TraceGate Command Center UI (static/index.html)             |
|   +--------------------+  +--------------------+  +--------------------------+   |
|   | Chaos Toggles      |  | Simulator Studio   |  | Support Triage & Console |   |
|   | - payment_timeout  |  | - Create Booking   |  | - Open Tickets Table     |   |
|   | - drop_webhook     |  | - Webhook Callback |  | - 1-Click Reconcile      |   |
|   | - force_500_booking|  | - Live JSON Viewer |  | - Read-Only SQL Console  |   |
|   | - strict_rate_limit|  +--------------------+  +--------------------------+   |
+----------------------------------------------------------------------------------+
                                        │
                                        ▼ REST API
+──────────────────────────────────────────────────────────────────────────────────+
|                        FastAPI Core Backend (app/main.py)                        |
|                                                                                  |
|   [POST /api/faults]                -> Updates runtime FAULT_STATE               |
|   [POST /api/bookings]              -> Idempotency check, schema validation      |
|   [POST /api/payments/webhook]      -> Processes gateway callbacks / drops       |
|   [POST /api/support/reconcile/{id}]-> Idempotent booking & ticket resolution    |
|   [POST /api/support/sql]           -> Read-only SQL parser & security guard     |
|   [POST /api/qa/run-tests]          -> In-process subprocess pytest execution    |
|   [GET  /api/overview]              -> Telemetry, active logs, tickets, bookings |
+──────────────────────────────────────────────────────────────────────────────────+
                                        │
                                        ▼ SQLite WAL
+──────────────────────────────────────────────────────────────────────────────────+
|                        Persistence Layer (tracegate.db)                          |
|   - bookings: Idempotency keys, lifecycle states (PENDING -> CONFIRMED / FAILED) |
|   - api_logs: Microsecond telemetry, error details, active fault tags, latency   |
|   - support_tickets: Auto-generated triage records for dropped callbacks         |
+──────────────────────────────────────────────────────────────────────────────────+
```

---

## Quickstart

### Prerequisites
- Python 3.9+ (or active virtual environment `.venv`)

### 1. Setup & Installation
```bash
# Clone the repository and enter the directory
cd tracegate

# Activate virtual environment
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 2. Run the Local Development Server
```bash
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Open your browser at **[http://localhost:8000](http://localhost:8000)** to access the **TraceGate Command Center**.

---

## Chaos & Fault Scenarios

TraceGate allows engineering and support teams to test failure modes on demand via the UI toggles or `POST /api/faults`:

| Fault Flag | HTTP Result | Simulated Failure Mode | Recovery / Triage Flow |
|---|---|---|---|
| `payment_timeout` | `504 Gateway Timeout` | Upstream payment gateway hangs and times out during callback. | Booking transitions to `FAILED` / `TIMEOUT_504`. Investigated via API trace logs. |
| `drop_webhook` | `200` with warning (`accepted_with_lag`) | Webhook message dropped in transit. Money deducted from user, but booking stays in `PENDING_PAYMENT`. | Auto-generates high-severity Support Ticket (`TCK-xxx`). Resolved via **1-click Reconcile** button or SQL diagnostics. |
| `force_500_booking` | `500 Internal Error` | Database connection or persistence layer failure during booking creation. | Booking is rejected before DB commit; logged to `api_logs` with stack details. |
| `strict_rate_limit` | `429 Too Many Requests` | Rapid burst traffic exceeding rate threshold (>3 requests / 10s). | Validates client-side backoff, retry headers, and rate-limiting responses. |

---

## Key Pillars Demonstrated

### 1. Manual QA & Exploratory Testing
- **Interactive Simulator**: Craft custom booking payloads with custom idempotency keys and amounts.
- **Webhook Dispatcher**: Manually trigger payment lifecycle callbacks (`payment.captured`, unsupported events, or non-existent booking IDs).
- **Live Response Inspector**: Real-time HTTP status codes, latency in milliseconds, and formatted JSON payloads.

### 2. Automated QA Engineering
Comprehensive automated test suite located in `tests/test_tracegate.py` executed with `pytest`:
- **Smoke & Happy Path**: Validates booking creation (201) followed by payment confirmation (200).
- **Idempotency Verification**: Validates that replaying duplicate booking requests with identical `idempotency_key` returns the existing record (200) without creating duplicate rows.
- **Negative & Boundary Validation**: Invalid email schemas, negative/zero amounts, string length limits, non-existent booking IDs (404), and unsupported webhook events (400).
- **Chaos Fault Verification**: Programmatically asserts 504 timeouts, 500 errors, 429 rate-limiting, and auto-ticketing upon dropped webhooks.

Run the test suite directly from your terminal:
```bash
pytest -v
```
Or execute it straight from the Command Center UI via the **"Run pytest Suite"** button.

### 3. API Security & Guardrails
- **Read-Only SQL Diagnostic Sandbox (`/api/support/sql`)**:
  - Requires all queries to strictly begin with `SELECT`.
  - Blocks dangerous DDL / DML keywords (`DROP`, `DELETE`, `UPDATE`, `INSERT`, `ALTER`, `PRAGMA`, `ATTACH`).
  - Returns structured table metadata (`columns`, `rows`, `row_count`) while rejecting malicious multi-statement queries.

### 4. Application Support & L1/L2 Triage
- **Ghost Payment Deduction Triage**:
  When `drop_webhook` is triggered, the customer's bank debits the funds, but the booking remains `PENDING_PAYMENT (DEDUCTED_WEBHOOK_MISSED)`.
- **Auto-Ticketing**: TraceGate automatically creates a `HIGH` severity support ticket with customer details and booking reference.
- **Diagnostic SQL Presets**: One-click preset queries to quickly isolate unconfirmed bookings with missing callbacks.
- **1-Click Reconciliation (`POST /api/support/reconcile/{booking_id}`)**:
  Atomically updates booking status to `CONFIRMED (RECONCILED_PAID)` and closes the support ticket with an audit resolution note.

---

## REST API Reference

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/` | Serves the TraceGate Command Center single-page app |
| `GET` | `/api/health` | Healthcheck and active fault status |
| `GET` | `/api/faults` | Returns current chaos fault flags |
| `POST` | `/api/faults` | Update one or more chaos fault flags |
| `POST` | `/api/bookings` | Create a new service booking (supports idempotency) |
| `POST` | `/api/payments/webhook` | Ingest gateway payment webhook callbacks |
| `POST` | `/api/support/reconcile/{booking_id}` | Reconcile stuck booking and resolve related support ticket |
| `GET` | `/api/overview` | Aggregated telemetry (bookings, tickets, logs, faults) |
| `POST` | `/api/support/sql` | Execute safe, read-only SQL diagnostic query |
| `POST` | `/api/qa/run-tests` | Run automated pytest suite and capture terminal output |
| `POST` | `/api/reset` | Reset sandbox database and fault states to initial defaults |

---

## Continuous Integration (CI)

A GitHub Actions workflow is provided at `.github/workflows/ci.yml`. It triggers on push and pull requests to run:
```yaml
pytest -v --tb=short
```
Ensuring zero regressions across all core features and fault injection scenarios.
