# Clinic Booking API — Koya Cohort 4 Task 1

FastAPI backend plus a small browser UI. Implements the six paths in Koya's OpenAPI contract and levels 1–5.

## Features

- **Level 1:** `POST /reset`, `GET /slots`, `POST /bookings`, `GET /bookings/{id}`.
- **Level 2:** `X-API-Key` authentication on every API endpoint; booking input validation; past slots cannot be booked.
- **Level 3:** a database partial unique index prevents two confirmed bookings for one slot, including concurrent requests; `DELETE /bookings/{id}` cancels and frees a slot.
- **Level 4:** `Idempotency-Key` support and cursor pagination on `GET /bookings?email=...`.
- **Level 5:** database-backed sliding-window limit of five booking attempts per email per minute; failed validation attempts count; sixth request receives `429` and `Retry-After`.
- **Web page:** `/` lists slots, submits bookings, displays confirmations, lists a patient's bookings, and supports cancellation.

All API errors use `{"error":"..."}`. The only non-API route is `/`, which serves the browser page.

## Run locally (quickest path)

Requires Python 3.11+.

```bash
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# macOS/Linux:
# source .venv/bin/activate
pip install -r requirements.txt
```

Set an API key in the terminal before starting the app.

**Windows PowerShell:**

```powershell
$env:API_KEY="your-long-random-secret"
uvicorn main:app --reload
```

**macOS/Linux:**

```bash
export API_KEY="your-long-random-secret"
uvicorn main:app --reload
```

Open `http://127.0.0.1:8000`, enter the same API key, and use the page. API documentation is at `http://127.0.0.1:8000/docs`.

The default local database is SQLite (`clinic.db`). For production/concurrent deployment, set `DATABASE_URL` to PostgreSQL.

## Quick API test

Every API call needs `X-API-Key`.

```bash
curl -X POST http://127.0.0.1:8000/reset \
  -H "X-API-Key: your-long-random-secret" \
  -H "Content-Type: application/json" \
  -d '{"slots":[{"id":"s1","provider":"Dr Ade","start":"2027-01-15T09:00:00Z","end":"2027-01-15T09:30:00Z"}]}'

curl http://127.0.0.1:8000/slots -H "X-API-Key: your-long-random-secret"

curl -X POST http://127.0.0.1:8000/bookings \
  -H "X-API-Key: your-long-random-secret" \
  -H "Idempotency-Key: booking-request-001" \
  -H "Content-Type: application/json" \
  -d '{"slot_id":"s1","name":"Ada Obi","email":"ada@example.com"}'
```

## Deploy (Render + hosted PostgreSQL)

1. Push this project folder to a new GitHub repository.
2. Create a PostgreSQL database with a managed provider such as Neon. Copy its PostgreSQL connection string. Use the pooled/standard connection string recommended by the provider.
3. In Render, choose **New → Web Service**, connect the GitHub repository, and use:
   - Runtime: **Python 3**
   - Build command: `pip install -r requirements.txt`
   - Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
4. In Render's environment variables, set:
   - `API_KEY`: a long random secret you create. Keep it private.
   - `DATABASE_URL`: the PostgreSQL connection string from your database provider.
5. Deploy. Open the Render URL; the page is served at `/`. Enter your `API_KEY` in the page before calling the API.
6. Test the live API by loading `/` and then `/slots` from the page. You can also use `/docs` to inspect the routes.

Do not commit `.env`, database credentials, or your real API key to GitHub. Do not use SQLite for a multi-instance production deployment because its local file is not shared/persistent on ephemeral hosts.

## Contract notes

- `POST /reset` replaces the slot set and clears bookings and related test state.
- `GET /slots` returns `{ "slots": [...] }`, earliest first, with `available` per slot. `provider` is an exact-match optional filter.
- `POST /bookings` returns `201` and a booking object. Email is trimmed and lowercased; name/email are trimmed.
- `GET /bookings` requires `email`, accepts `limit` (default 10, range 1–50) and opaque `cursor`, and returns `{ "bookings": [...], "next_cursor": ... }` oldest first.
- `DELETE /bookings/{id}` changes status to `cancelled`; a cancelled slot becomes available again.

## Add the first appointment slots after deployment

A new database starts empty. After deploying, open `https://YOUR-SERVICE.onrender.com/docs`, click **Authorize**, enter the same `API_KEY`, and use `POST /reset` with a few future slots. Example body:

```json
{
  "slots": [
    {"id": "s1", "provider": "Dr Ade", "start": "2027-01-15T09:00:00Z", "end": "2027-01-15T09:30:00Z"},
    {"id": "s2", "provider": "Dr Ade", "start": "2027-01-15T10:00:00Z", "end": "2027-01-15T10:30:00Z"},
    {"id": "s3", "provider": "Dr Bello", "start": "2027-01-15T11:00:00Z", "end": "2027-01-15T11:30:00Z"}
  ]
}
```

Then open the service's home page (`/`), enter the API key, and press **Show available slots**. Note: `/reset` replaces all slots and clears current bookings, so use it only when you intend to reset the clinic schedule.
