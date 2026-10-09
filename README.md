# Employee Attendance & Analytics API

FastAPI + MongoDB service for the HROne engineering assignment. All application code is in `app/main.py`.
The review of the starter is in `REVIEW.md` (Part A); design answers are in `DECISIONS.md` (Part C).

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI=mongodb://localhost:27017 MONGO_DB=attendance_db   # or put them in a .env file
uvicorn app.main:app --port 8000
```
MongoDB 6.0+ is required (`$densify`, `$documents` and `$setWindowFields` are used). Indexes are created at startup.

## Things to know
- `MONGO_URI` and `MONGO_DB` are required; real environment variables win over a `.env` file.
- Every endpoint in the contract is implemented; nothing is missing.
- Trend: "range exceeds 92 days" is read as more than 92 rows, so a full 92-day quarter is accepted.
- `PATCH`: an explicit `null` for `status`, `punch_in` or `punch_out` is a 422 (omit a field to leave it unchanged).
- Punch-out keeps the stored `late_minutes`; `PATCH` recomputes every derived field.
- Tested against MongoDB 7.0 with ~100k attendance records: every `/admin/explain` plan uses `IXSCAN`, never `COLLSCAN`.
