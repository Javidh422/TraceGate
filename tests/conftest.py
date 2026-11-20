import sys
from pathlib import Path

# Ensure root workspace directory is in sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import pytest
from fastapi.testclient import TestClient

from app.database import get_connection, init_db
from app.main import FAULT_STATE, REQUEST_TIMESTAMPS, app


@pytest.fixture(autouse=True)
def clean_sandbox_state():
    """Ensure a clean database and reset fault flags before and after every test."""
    REQUEST_TIMESTAMPS.clear()
    for key in FAULT_STATE:
        FAULT_STATE[key] = False

    init_db()

    conn = get_connection()
    conn.executescript(
        "DELETE FROM bookings; DELETE FROM support_tickets; DELETE FROM api_logs;"
    )
    conn.commit()
    conn.close()

    init_db()

    yield

    REQUEST_TIMESTAMPS.clear()
    for key in FAULT_STATE:
        FAULT_STATE[key] = False


@pytest.fixture
def client():
    """FastAPI TestClient fixture."""
    with TestClient(app) as test_client:
        yield test_client
