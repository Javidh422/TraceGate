import uuid
import pytest
from fastapi.testclient import TestClient


class TestSmokeAndHappyPath:
    def test_health_check(self, client: TestClient):
        response = client.get("/api/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["service"] == "TraceGate"
        assert "faults" in data

    def test_serve_dashboard(self, client: TestClient):
        response = client.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers.get("content-type", "")
        assert "TraceGate" in response.text

    def test_create_booking_success(self, client: TestClient):
        payload = {
            "customer_name": "Vikram Seth",
            "customer_email": "vikram.seth@example.com",
            "service_type": "Brake Inspection",
            "amount": 1250.0,
            "idempotency_key": f"idem-smoke-{uuid.uuid4().hex[:6]}",
        }
        response = client.post("/api/bookings", json=payload)
        assert response.status_code == 201
        data = response.json()
        assert data["id"].startswith("BK-")
        assert data["customer_name"] == payload["customer_name"]
        assert data["customer_email"] == payload["customer_email"]
        assert data["amount"] == 1250.0
        assert data["status"] == "PENDING_PAYMENT"
        assert data["payment_status"] == "AWAITING_WEBHOOK"

    def test_booking_and_webhook_confirmation_flow(self, client: TestClient):
        # 1. Create booking (201)
        booking_payload = {
            "customer_name": "Pooja Hegde",
            "customer_email": "pooja.h@example.com",
            "service_type": "Express Oil Change",
            "amount": 1800.0,
            "idempotency_key": f"idem-flow-{uuid.uuid4().hex[:6]}",
        }
        create_res = client.post("/api/bookings", json=booking_payload)
        assert create_res.status_code == 201
        booking = create_res.json()
        booking_id = booking["id"]

        # 2. Process payment captured webhook (200)
        webhook_payload = {
            "booking_id": booking_id,
            "event": "payment.captured",
            "transaction_id": f"txn_{uuid.uuid4().hex[:8]}",
        }
        webhook_res = client.post("/api/payments/webhook", json=webhook_payload)
        assert webhook_res.status_code == 200
        webhook_data = webhook_res.json()
        assert webhook_data["status"] == "processed"
        assert webhook_data["booking_id"] == booking_id
        assert webhook_data["new_state"] == "CONFIRMED"

        # 3. Verify via overview and SQL
        overview_res = client.get("/api/overview")
        assert overview_res.status_code == 200
        overview_data = overview_res.json()
        matching_bookings = [b for b in overview_data["bookings"] if b["id"] == booking_id]
        assert len(matching_bookings) == 1
        assert matching_bookings[0]["status"] == "CONFIRMED"
        assert matching_bookings[0]["payment_status"] == "PAID"


class TestIdempotency:
    def test_idempotency_duplicate_booking_prevention(self, client: TestClient):
        idempotency_key = f"idem-test-{uuid.uuid4().hex[:8]}"
        payload = {
            "customer_name": "Siddharth Roy",
            "customer_email": "siddharth.r@example.com",
            "service_type": "Wheel Alignment",
            "amount": 750.0,
            "idempotency_key": idempotency_key,
        }

        # First attempt: Created (201)
        res1 = client.post("/api/bookings", json=payload)
        assert res1.status_code == 201
        data1 = res1.json()
        first_booking_id = data1["id"]

        # Second attempt with identical idempotency key: Returns existing (200)
        res2 = client.post("/api/bookings", json=payload)
        assert res2.status_code == 200
        data2 = res2.json()
        assert data2["id"] == first_booking_id
        assert data2["idempotency_key"] == idempotency_key

        # Verify only one booking was created in the database
        sql_res = client.post(
            "/api/support/sql",
            json={"query": f"SELECT COUNT(*) as count FROM bookings WHERE idempotency_key = '{idempotency_key}'"},
        )
        assert sql_res.status_code == 200
        assert sql_res.json()["rows"][0]["count"] == 1


class TestValidationAndNegative:
    def test_invalid_email_format(self, client: TestClient):
        payload = {
            "customer_name": "Invalid Email User",
            "customer_email": "not-an-email",
            "service_type": "General Checkup",
            "amount": 500.0,
        }
        res = client.post("/api/bookings", json=payload)
        assert res.status_code == 422

    def test_negative_and_zero_amount(self, client: TestClient):
        # Negative amount
        res_neg = client.post(
            "/api/bookings",
            json={
                "customer_name": "Negative Amount User",
                "customer_email": "valid@example.com",
                "service_type": "Battery Replacement",
                "amount": -250.0,
            },
        )
        assert res_neg.status_code == 422

        # Zero amount
        res_zero = client.post(
            "/api/bookings",
            json={
                "customer_name": "Zero Amount User",
                "customer_email": "valid@example.com",
                "service_type": "Battery Replacement",
                "amount": 0.0,
            },
        )
        assert res_zero.status_code == 422

    def test_invalid_customer_name_length(self, client: TestClient):
        res = client.post(
            "/api/bookings",
            json={
                "customer_name": "A",  # min_length is 2
                "customer_email": "valid@example.com",
                "service_type": "Tire Rotation",
                "amount": 600.0,
            },
        )
        assert res.status_code == 422

    def test_webhook_non_existent_booking_404(self, client: TestClient):
        payload = {
            "booking_id": "BK-NONEXISTENT",
            "event": "payment.captured",
            "transaction_id": "txn_fake_999",
        }
        res = client.post("/api/payments/webhook", json=payload)
        assert res.status_code == 404
        assert "not found" in res.json()["detail"].lower()

    def test_webhook_unsupported_event_400(self, client: TestClient):
        # First create a valid booking
        create_res = client.post(
            "/api/bookings",
            json={
                "customer_name": "Deepak Verma",
                "customer_email": "deepak.v@example.com",
                "service_type": "Engine Tuneup",
                "amount": 4200.0,
            },
        )
        assert create_res.status_code == 201
        booking_id = create_res.json()["id"]

        # Send unsupported webhook event
        webhook_payload = {
            "booking_id": booking_id,
            "event": "payment.chargeback_initiated",
            "transaction_id": "txn_unsupported_123",
        }
        res = client.post("/api/payments/webhook", json=webhook_payload)
        assert res.status_code == 400
        assert "unsupported" in res.json()["detail"].lower()


class TestFaultInjection:
    def test_payment_timeout_fault_triggers_504(self, client: TestClient):
        # Enable payment_timeout fault
        fault_res = client.post("/api/faults", json={"payment_timeout": True})
        assert fault_res.status_code == 200
        assert fault_res.json()["payment_timeout"] is True

        # Create booking
        booking_res = client.post(
            "/api/bookings",
            json={
                "customer_name": "Kavita Rao",
                "customer_email": "kavita.r@example.com",
                "service_type": "Transmission Fluid Change",
                "amount": 3500.0,
            },
        )
        assert booking_res.status_code == 201
        booking_id = booking_res.json()["id"]

        # Send webhook - should trigger 504 Gateway Timeout
        webhook_res = client.post(
            "/api/payments/webhook",
            json={
                "booking_id": booking_id,
                "event": "payment.captured",
                "transaction_id": "txn_timeout_789",
            },
        )
        assert webhook_res.status_code == 504
        assert "gateway timeout" in webhook_res.json()["detail"].lower()

        # Check booking transitioned to FAILED / TIMEOUT_504
        overview = client.get("/api/overview").json()
        b = next(item for item in overview["bookings"] if item["id"] == booking_id)
        assert b["status"] == "FAILED"
        assert b["payment_status"] == "TIMEOUT_504"

    def test_drop_webhook_fault_and_reconciliation(self, client: TestClient):
        # Enable drop_webhook fault
        client.post("/api/faults", json={"drop_webhook": True})

        # Create booking
        booking_res = client.post(
            "/api/bookings",
            json={
                "customer_name": "Rohan Deshmukh",
                "customer_email": "rohan.d@example.com",
                "service_type": "Ceramic Coating",
                "amount": 8500.0,
            },
        )
        assert booking_res.status_code == 201
        booking_id = booking_res.json()["id"]

        # Send webhook - dropped in transit (returns 202 and creates ticket)
        webhook_res = client.post(
            "/api/payments/webhook",
            json={
                "booking_id": booking_id,
                "event": "payment.captured",
                "transaction_id": "txn_drop_456",
            },
        )
        assert webhook_res.status_code == 200  # FastAPI endpoint returns 200 with warning body
        data = webhook_res.json()
        assert data["status"] == "accepted_with_lag"
        assert "ticket_id" in data
        ticket_id = data["ticket_id"]

        # Verify state is PENDING_PAYMENT / DEDUCTED_WEBHOOK_MISSED
        overview = client.get("/api/overview").json()
        b = next(item for item in overview["bookings"] if item["id"] == booking_id)
        assert b["status"] == "PENDING_PAYMENT"
        assert b["payment_status"] == "DEDUCTED_WEBHOOK_MISSED"

        # Verify support ticket exists and is OPEN
        ticket = next(item for item in overview["tickets"] if item["id"] == ticket_id)
        assert ticket["booking_id"] == booking_id
        assert ticket["status"] == "OPEN"
        assert ticket["severity"] == "HIGH"

        # Reconcile booking via support triage endpoint
        reconcile_res = client.post(f"/api/support/reconcile/{booking_id}")
        assert reconcile_res.status_code == 200
        assert reconcile_res.json()["status"] == "reconciled"

        # Verify reconciled state
        updated_overview = client.get("/api/overview").json()
        updated_b = next(item for item in updated_overview["bookings"] if item["id"] == booking_id)
        assert updated_b["status"] == "CONFIRMED"
        assert updated_b["payment_status"] == "RECONCILED_PAID"

        updated_ticket = next(item for item in updated_overview["tickets"] if item["id"] == ticket_id)
        assert updated_ticket["status"] == "RESOLVED"
        assert "replayed idempotent webhook" in updated_ticket["resolution_note"].lower()

    def test_force_500_booking_fault(self, client: TestClient):
        # Enable force_500_booking
        client.post("/api/faults", json={"force_500_booking": True})

        res = client.post(
            "/api/bookings",
            json={
                "customer_name": "Ananya Sharma",
                "customer_email": "ananya.s@example.com",
                "service_type": "Windshield Replacement",
                "amount": 5400.0,
            },
        )
        assert res.status_code == 500
        assert "internal server error" in res.json()["detail"].lower()

    def test_strict_rate_limit_fault(self, client: TestClient):
        # Enable strict_rate_limit
        client.post("/api/faults", json={"strict_rate_limit": True})

        payload = {
            "customer_name": "Rate Limit Tester",
            "customer_email": "ratelimit@example.com",
            "service_type": "Quick Wash",
            "amount": 350.0,
        }

        # 3 allowed requests within window
        for i in range(3):
            res = client.post("/api/bookings", json={**payload, "idempotency_key": f"rl-key-{i}"})
            assert res.status_code == 201

        # 4th request triggers 429 Too Many Requests
        res_4 = client.post("/api/bookings", json={**payload, "idempotency_key": "rl-key-4"})
        assert res_4.status_code == 429
        assert "rate limit exceeded" in res_4.json()["detail"].lower()


class TestSqlSecurityGuard:
    def test_select_query_succeeds(self, client: TestClient):
        res = client.post(
            "/api/support/sql",
            json={"query": "SELECT id, customer_name, status FROM bookings LIMIT 5"},
        )
        assert res.status_code == 200
        data = res.json()
        assert "columns" in data
        assert "rows" in data
        assert "row_count" in data
        assert "customer_name" in data["columns"]

    @pytest.mark.parametrize(
        "query",
        [
            "DROP TABLE bookings",
            "DELETE FROM bookings WHERE id = 'BK-1001'",
            "UPDATE bookings SET status = 'HACKED'",
            "INSERT INTO bookings (id) VALUES ('HACK')",
            "ALTER TABLE bookings ADD COLUMN hacked TEXT",
            "PRAGMA table_info(bookings)",
            "ATTACH DATABASE 'test.db' AS test",
            "SELECT * FROM bookings; DROP TABLE bookings;",
            "SELECT * FROM bookings WHERE id = '1'; DELETE FROM bookings;",
            "SELECT * FROM bookings; UPDATE bookings SET status = 'HACKED';",
        ],
    )
    def test_unsafe_queries_are_blocked(self, client: TestClient, query: str):
        res = client.post("/api/support/sql", json={"query": query})
        assert res.status_code == 400
        detail = res.json()["detail"].lower()
        assert "blocked by tracegate" in detail or "only read-only select" in detail


class TestSupportAndAdmin:
    def test_overview_endpoint(self, client: TestClient):
        res = client.get("/api/overview")
        assert res.status_code == 200
        data = res.json()
        assert "bookings" in data
        assert "tickets" in data
        assert "logs" in data
        assert "faults" in data
        assert len(data["bookings"]) >= 3  # seeded default items
        assert len(data["tickets"]) >= 2

    def test_reconcile_404_for_non_existent_booking(self, client: TestClient):
        res = client.post("/api/support/reconcile/BK-NONEXISTENT")
        assert res.status_code == 404
        assert "not found" in res.json()["detail"].lower()

    def test_reset_sandbox(self, client: TestClient):
        # Change fault and insert custom booking
        client.post("/api/faults", json={"payment_timeout": True})
        client.post(
            "/api/bookings",
            json={
                "customer_name": "Reset Test User",
                "customer_email": "reset@example.com",
                "service_type": "Inspection",
                "amount": 500.0,
            },
        )

        # Call reset
        res = client.post("/api/reset")
        assert res.status_code == 200
        assert res.json()["status"] == "reset_complete"

        # Verify all faults reset to False
        faults_res = client.get("/api/faults")
        faults = faults_res.json()
        assert all(val is False for val in faults.values())

        # Verify seeded data restored
        overview = client.get("/api/overview").json()
        assert len(overview["bookings"]) == 3
        assert len(overview["tickets"]) == 2

    def test_qa_run_tests_endpoint(self, client: TestClient, monkeypatch: pytest.MonkeyPatch):
        import subprocess

        mock_completed = subprocess.CompletedProcess(
            args=["pytest"],
            returncode=0,
            stdout="================ 25 passed in 0.45s ================",
            stderr="",
        )
        monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: mock_completed)

        res = client.post("/api/qa/run-tests")
        assert res.status_code == 200
        data = res.json()
        assert data["passed"] is True
        assert data["exit_code"] == 0
        assert "25 passed" in data["output"]
