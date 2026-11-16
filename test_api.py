"""Smoke tests for the Clinic Booking API. Run: pytest -q"""

import os
import tempfile
from datetime import datetime, timedelta, timezone

# Tests import the application against an isolated SQLite database.
tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
tmp_db.close()
os.environ["DATABASE_URL"] = f"sqlite:///{tmp_db.name}"
os.environ["API_KEY"] = "test-secret"

from fastapi.testclient import TestClient
import main
main.init_db()

client = TestClient(main.app)
HEADERS = {"X-API-Key": "test-secret"}


def future(value_minutes: int = 120) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=value_minutes)).isoformat().replace("+00:00", "Z")


def reset_with_slots(ids=("s1", "s2")):
    slots = []
    for index, slot_id in enumerate(ids):
        start = future(120 + index * 60)
        end = (datetime.fromisoformat(start.replace("Z", "+00:00")) + timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
        slots.append({"id": slot_id, "provider": "Dr Ade", "start": start, "end": end})
    response = client.post("/reset", headers=HEADERS, json={"slots": slots})
    assert response.status_code == 200


def test_auth_and_level1_booking_flow():
    assert client.get("/slots").status_code == 401
    reset_with_slots()
    slots = client.get("/slots", headers=HEADERS).json()["slots"]
    assert len(slots) == 2 and slots[0]["available"] is True
    created = client.post("/bookings", headers=HEADERS, json={"slot_id": "s1", "name": " Ada Obi ", "email": " ADA@EXAMPLE.COM "})
    assert created.status_code == 201
    booking = created.json()
    assert booking["name"] == "Ada Obi" and booking["email"] == "ada@example.com"
    assert client.get(f"/bookings/{booking['id']}", headers=HEADERS).json()["id"] == booking["id"]
    assert client.get("/slots", headers=HEADERS).json()["slots"][0]["available"] is False


def test_validation_conflict_idempotency_and_cancel():
    reset_with_slots(("s1",))
    bad = client.post("/bookings", headers=HEADERS, json={"slot_id": "s1", "name": "Ada", "email": "bad"})
    assert bad.status_code == 400
    headers = {**HEADERS, "Idempotency-Key": "once"}
    body = {"slot_id": "s1", "name": "Ada Obi", "email": "ada@example.com"}
    first = client.post("/bookings", headers=headers, json=body)
    second = client.post("/bookings", headers=headers, json=body)
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    mismatch = client.post("/bookings", headers=headers, json={**body, "name": "Different"})
    assert mismatch.status_code == 422
    conflict = client.post("/bookings", headers=HEADERS, json=body)
    assert conflict.status_code == 409
    booking_id = first.json()["id"]
    cancelled = client.delete(f"/bookings/{booking_id}", headers=HEADERS)
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    slots = client.get("/slots", headers=HEADERS).json()["slots"]
    assert slots[0]["available"] is True


def test_list_bookings_and_rate_limit():
    reset_with_slots(tuple(f"s{i}" for i in range(1, 8)))
    for index in range(5):
        response = client.post("/bookings", headers=HEADERS, json={"slot_id": f"s{index+1}", "name": f"Ada {index}", "email": "ada@example.com"})
        assert response.status_code == 201
    sixth = client.post("/bookings", headers=HEADERS, json={"slot_id": "s6", "name": "Ada 6", "email": "ada@example.com"})
    assert sixth.status_code == 429 and "Retry-After" in sixth.headers
    page = client.get("/bookings?email=ADA%40EXAMPLE.COM&limit=2", headers=HEADERS).json()
    assert len(page["bookings"]) == 2 and page["next_cursor"]
    page2 = client.get(f"/bookings?email=ada%40example.com&limit=2&cursor={page['next_cursor']}", headers=HEADERS).json()
    assert len(page2["bookings"]) == 2

def teardown_module(_module):
    try:
        client.close()
    except Exception:
        pass

    main.engine.dispose()
    os.unlink(tmp_db.name)

def test_simultaneous_requests_cannot_confirm_same_slot():
    from concurrent.futures import ThreadPoolExecutor

    reset_with_slots(("race-slot",))

    def attempt(index):
        return client.post(
            "/bookings",
            headers=HEADERS,
            json={
                "slot_id": "race-slot",
                "name": f"Patient {index}",
                "email": f"patient{index}@example.com",
            },
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(attempt, range(8)))

    statuses = [response.status_code for response in responses]
    assert statuses.count(201) == 1
    assert statuses.count(409) == 7
