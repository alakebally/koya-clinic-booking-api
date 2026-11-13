from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Security
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import APIKeyHeader
from fastapi.exceptions import RequestValidationError
from sqlalchemy import (
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    create_engine,
    delete,
    or_,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


# ---------------------------------------------------------------------------
# Configuration and database
# ---------------------------------------------------------------------------
API_KEY = os.getenv("API_KEY", "local-dev-key-change-me")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./clinic.db")

# Neon and other providers commonly supply postgresql:// URLs. This project
# uses psycopg 3, so make the SQLAlchemy driver explicit.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

engine_options: dict[str, Any] = {"pool_pre_ping": True}
if DATABASE_URL.startswith("sqlite:"):
    engine_options["connect_args"] = {"check_same_thread": False, "timeout": 30}

engine = create_engine(DATABASE_URL, **engine_options)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Slot(Base):
    __tablename__ = "slots"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    start: Mapped[str] = mapped_column(Text, nullable=False)
    end: Mapped[str] = mapped_column(Text, nullable=False)


class Booking(Base):
    __tablename__ = "bookings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    slot_id: Mapped[str] = mapped_column(ForeignKey("slots.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)


# The database, not application-level timing, enforces one confirmed booking
# per slot. This protects against simultaneous requests across workers.
Index(
    "uq_one_confirmed_booking_per_slot",
    Booking.slot_id,
    unique=True,
    sqlite_where=(Booking.status == "confirmed"),
    postgresql_where=(Booking.status == "confirmed"),
)


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    response_json: Mapped[str] = mapped_column(Text, nullable=False)


class RateLimitState(Base):
    __tablename__ = "rate_limit_states"

    email: Mapped[str] = mapped_column(Text, primary_key=True)
    timestamps_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")


def init_db() -> None:
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# API errors, authentication, and helpers
# ---------------------------------------------------------------------------
class APIError(Exception):
    def __init__(self, status_code: int, message: str, headers: dict[str, str] | None = None):
        self.status_code = status_code
        self.message = message
        self.headers = headers or {}


@contextlib.asynccontextmanager
async def lifespan(_app):
    init_db()
    yield


app = FastAPI(title="Clinic Booking API", version="1.0.0", lifespan=lifespan)


@app.exception_handler(APIError)
async def api_error_handler(_request: Request, exc: APIError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.message},
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_request: Request, _exc: RequestValidationError):
    return JSONResponse(status_code=400, content={"error": "invalid request body or query parameter"})


@app.exception_handler(HTTPException)
async def http_error_handler(_request: Request, exc: HTTPException):
    message = exc.detail if isinstance(exc.detail, str) else "request failed"
    return JSONResponse(status_code=exc.status_code, content={"error": message}, headers=exc.headers)


api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(x_api_key: str | None = Security(api_key_header)) -> None:
    if not x_api_key or not secrets.compare_digest(x_api_key, API_KEY):
        raise APIError(401, "missing or invalid API key")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def datetime_to_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_datetime(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("must be an ISO 8601 date-time")
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def slot_to_dict(slot: Slot, available: bool) -> dict[str, Any]:
    return {
        "id": slot.id,
        "provider": slot.provider,
        "start": slot.start,
        "end": slot.end,
        "available": available,
    }


def booking_to_dict(booking: Booking) -> dict[str, Any]:
    return {
        "id": booking.id,
        "slot_id": booking.slot_id,
        "name": booking.name,
        "email": booking.email,
        "status": booking.status,
        "created_at": booking.created_at,
    }


def normalize_email(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    return value or None


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def canonical_request_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def cursor_encode(created_at: str, booking_id: str) -> str:
    raw = json.dumps([created_at, booking_id], separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def cursor_decode(cursor: str) -> tuple[str, str]:
    try:
        padded = cursor + ("=" * (-len(cursor) % 4))
        created_at, booking_id = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        if not isinstance(created_at, str) or not isinstance(booking_id, str):
            raise ValueError
        return created_at, booking_id
    except Exception as exc:
        raise APIError(400, "invalid cursor") from exc


# ---------------------------------------------------------------------------
# Level 5: database-backed sliding-window limiter
# ---------------------------------------------------------------------------
def register_booking_attempt(db: Session, email: str | None) -> None:
    # With no usable email, there is no per-email bucket to apply. A supplied
    # malformed email is still normalized and counted before validation.
    if not email:
        return

    dialect = db.bind.dialect.name if db.bind is not None else "sqlite"
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    else:
        from sqlalchemy.dialects.sqlite import insert as dialect_insert

    now = time.time()
    cutoff = now - 60.0

    # Create the bucket if absent, then lock it on Postgres so concurrent
    # booking attempts for the same email cannot exceed the limit.
    insert_stmt = dialect_insert(RateLimitState).values(email=email, timestamps_json="[]")
    insert_stmt = insert_stmt.on_conflict_do_nothing(index_elements=["email"])
    db.execute(insert_stmt)
    state = db.execute(
        select(RateLimitState).where(RateLimitState.email == email).with_for_update()
    ).scalar_one()

    try:
        timestamps = [float(value) for value in json.loads(state.timestamps_json)]
    except (TypeError, ValueError, json.JSONDecodeError):
        timestamps = []

    timestamps = [stamp for stamp in timestamps if stamp > cutoff]
    if len(timestamps) >= 5:
        retry_after = max(1, int(60 - (now - min(timestamps)) + 0.999))
        state.timestamps_json = json.dumps(timestamps)
        db.commit()
        raise APIError(
            429,
            "too many booking attempts for this email; try again later",
            headers={"Retry-After": str(retry_after)},
        )

    timestamps.append(now)
    state.timestamps_json = json.dumps(timestamps)
    db.commit()


# ---------------------------------------------------------------------------
# Startup and web page
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def homepage():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "index.html"))


# ---------------------------------------------------------------------------
# Level 1: reset slots and list slots
# ---------------------------------------------------------------------------
@app.post("/reset", dependencies=[Depends(require_api_key)])
def reset_slots(payload: dict[str, Any], db: Session = Depends(get_db)):
    incoming_slots = payload.get("slots")
    if not isinstance(incoming_slots, list):
        raise APIError(400, "slots must be an array")

    prepared: list[Slot] = []
    seen_ids: set[str] = set()
    try:
        for item in incoming_slots:
            if not isinstance(item, dict):
                raise ValueError("each slot must be an object")
            slot_id = item.get("id")
            provider = item.get("provider")
            start = parse_datetime(item.get("start"))
            end = parse_datetime(item.get("end"))
            if not isinstance(slot_id, str) or not slot_id.strip():
                raise ValueError("slot id is required")
            if not isinstance(provider, str) or not provider.strip():
                raise ValueError("slot provider is required")
            slot_id = slot_id.strip()
            if slot_id in seen_ids:
                raise ValueError("slot ids must be unique")
            if end <= start:
                raise ValueError("slot end must be after slot start")
            seen_ids.add(slot_id)
            prepared.append(
                Slot(
                    id=slot_id,
                    provider=provider.strip(),
                    start=datetime_to_text(start),
                    end=datetime_to_text(end),
                )
            )
    except (ValueError, TypeError) as exc:
        raise APIError(400, str(exc)) from exc

    # Reset is intentionally destructive: it replaces every slot and clears
    # bookings and related test state, as described in the assessment.
    db.execute(delete(Booking))
    db.execute(delete(IdempotencyRecord))
    db.execute(delete(RateLimitState))
    db.execute(delete(Slot))
    db.add_all(prepared)
    db.commit()
    return {"message": "slots replaced", "count": len(prepared)}


@app.get("/slots", dependencies=[Depends(require_api_key)])
def list_slots(provider: str | None = Query(default=None), db: Session = Depends(get_db)):
    slots = db.scalars(select(Slot)).all()
    if provider is not None:
        slots = [slot for slot in slots if slot.provider == provider]
    slots.sort(key=lambda slot: parse_datetime(slot.start))

    result = []
    for slot in slots:
        confirmed = db.scalar(
            select(Booking.id).where(
                Booking.slot_id == slot.id,
                Booking.status == "confirmed",
            ).limit(1)
        )
        result.append(slot_to_dict(slot, available=confirmed is None))
    return {"slots": result}


# ---------------------------------------------------------------------------
# Levels 1-5: create a booking
# ---------------------------------------------------------------------------
@app.post("/bookings", status_code=201, dependencies=[Depends(require_api_key)])
def create_booking(
    payload: dict[str, Any],
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
):
    if not isinstance(payload, dict):
        raise APIError(400, "request body must be an object")

    request_hash = canonical_request_hash(payload)

    # Count every booking request before validation or idempotency checks:
    # failed requests and replays are attempts too.
    attempt_email = normalize_email(payload.get("email"))
    register_booking_attempt(db, attempt_email)

    if idempotency_key:
        prior = db.get(IdempotencyRecord, idempotency_key)
        if prior is not None:
            if prior.request_hash != request_hash:
                raise APIError(422, "Idempotency-Key was already used with a different request body")
            return JSONResponse(status_code=prior.status_code, content=json.loads(prior.response_json))

    slot_id = payload.get("slot_id")
    name = payload.get("name")
    email = payload.get("email")

    if not isinstance(slot_id, str) or not slot_id.strip():
        raise APIError(400, "slot_id is required")
    if not isinstance(name, str) or not name.strip():
        raise APIError(400, "name is required")
    if not isinstance(email, str) or not email.strip():
        raise APIError(400, "email is required")

    slot_id = slot_id.strip()
    name = name.strip()
    email = email.strip().lower()
    if not EMAIL_RE.match(email):
        raise APIError(400, "email is invalid")

    slot = db.get(Slot, slot_id)
    if slot is None:
        raise APIError(404, "slot not found")

    try:
        slot_start = parse_datetime(slot.start)
    except ValueError as exc:
        raise APIError(400, "slot has an invalid start time") from exc
    if slot_start <= utc_now():
        raise APIError(400, "slot has already started")

    booking = Booking(
        id=str(uuid.uuid4()),
        slot_id=slot_id,
        name=name,
        email=email,
        status="confirmed",
        created_at=datetime_to_text(utc_now()),
    )
    db.add(booking)
    response_body = booking_to_dict(booking)
    if idempotency_key:
        db.add(
            IdempotencyRecord(
                key=idempotency_key,
                request_hash=request_hash,
                status_code=201,
                response_json=json.dumps(response_body),
            )
        )

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # A concurrent request may have committed this idempotency key first.
        if idempotency_key:
            prior = db.get(IdempotencyRecord, idempotency_key)
            if prior is not None:
                if prior.request_hash != request_hash:
                    raise APIError(422, "Idempotency-Key was already used with a different request body")
                return JSONResponse(status_code=prior.status_code, content=json.loads(prior.response_json))
        # The partial unique index is the final concurrency-safe guard.
        existing = db.scalar(
            select(Booking.id).where(
                Booking.slot_id == slot_id,
                Booking.status == "confirmed",
            ).limit(1)
        )
        if existing is not None:
            raise APIError(409, "slot is already booked")
        raise APIError(409, "booking could not be created")

    return JSONResponse(status_code=201, content=response_body)


# ---------------------------------------------------------------------------
# Level 4: list a person's bookings with cursor pagination
# ---------------------------------------------------------------------------
@app.get("/bookings", dependencies=[Depends(require_api_key)])
def list_person_bookings(
    email: str = Query(...),
    limit: int = Query(default=10, ge=1, le=50),
    cursor: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    if not email.strip():
        raise APIError(400, "email is required")
    page_size = limit

    normalized_email = email.strip().lower()
    statement = select(Booking).where(Booking.email == normalized_email)
    if cursor:
        last_created_at, last_id = cursor_decode(cursor)
        statement = statement.where(
            or_(
                Booking.created_at > last_created_at,
                (Booking.created_at == last_created_at) & (Booking.id > last_id),
            )
        )
    statement = statement.order_by(Booking.created_at.asc(), Booking.id.asc()).limit(page_size + 1)
    rows = db.scalars(statement).all()

    has_more = len(rows) > page_size
    page = rows[:page_size]
    next_cursor = cursor_encode(page[-1].created_at, page[-1].id) if has_more and page else None
    return {
        "bookings": [booking_to_dict(row) for row in page],
        "next_cursor": next_cursor,
    }


# ---------------------------------------------------------------------------
# Level 1: retrieve a booking
# ---------------------------------------------------------------------------
@app.get("/bookings/{id}", dependencies=[Depends(require_api_key)])
def get_booking(id: str, db: Session = Depends(get_db)):
    booking = db.get(Booking, id)
    if booking is None:
        raise APIError(404, "booking not found")
    return booking_to_dict(booking)


# ---------------------------------------------------------------------------
# Level 3: cancel a booking and release its slot
# ---------------------------------------------------------------------------
@app.delete("/bookings/{id}", dependencies=[Depends(require_api_key)])
def cancel_booking(id: str, db: Session = Depends(get_db)):
    booking = db.get(Booking, id)
    if booking is None:
        raise APIError(404, "booking not found")
    if booking.status == "cancelled":
        raise APIError(409, "booking is already cancelled")
    booking.status = "cancelled"
    db.commit()
    db.refresh(booking)
    return booking_to_dict(booking)
