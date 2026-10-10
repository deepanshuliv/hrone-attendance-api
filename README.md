# Employee Attendance & Analytics API

My solution for the HROne assignment, built with FastAPI and MongoDB. All the code is in `app/main.py`.

- `REVIEW.md` lists the problems I found in the starter code and how I fixed each one.
- `DECISIONS.md` answers the five questions.

## How to run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI=mongodb://localhost:27017 MONGO_DB=attendance_db   # or put these two in a .env file
uvicorn app.main:app --port 8000
```

## Good to know
- The app needs `MONGO_URI` and `MONGO_DB`. It reads a `.env` file if there is one, but real environment variables
  always win.
- It needs MongoDB 6.0 or newer. I tested it on 7.0.
- Indexes are created when the app starts, so there's nothing to set up by hand.
- Everything in the contract is done. Nothing is missing.
- A few places where I had to make a call:
  - **Trend:** I read "more than 92 days" as more than 92 days in the range, so a full 92-day quarter is allowed.
  - **PATCH:** sending `null` for `status`, `punch_in` or `punch_out` gives a 422. To leave a field as it is, just
    don't send it.
  - **Punch-out** keeps the late minutes worked out at punch-in. PATCH works out every derived field again.
- I tested with about 100,000 attendance records. Every `/admin/explain` plan uses an index (`IXSCAN`), and none of
  them scan the whole collection (`COLLSCAN`).
