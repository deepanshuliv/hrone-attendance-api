# REVIEW.md

Defects found in the starter `app/main.py`. Line numbers refer to the **original starter** (the first commit in this repository).

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | module, L22-23 | `MONGO_URI` / `MONGO_DB` fall back to hard-coded values. | Unset the variables: the app quietly connects to `localhost/attendance_db` instead of failing. | Both are required; missing → clear startup error. `.env` is still loaded but never overrides real env vars. |
| 2 | module (whole file) | No indexes are created, not even the unique ones the race rules depend on. | `explain` on `GET /attendance` shows `COLLSCAN`; duplicate employees/records can be inserted. | `ensure_indexes()` in the FastAPI lifespan, idempotent, 8 named indexes (see DECISIONS.md). |
| 3 | `health`, L72-74 | Returns 200 without talking to MongoDB. | Stop Mongo: `/health` still says ok. | `ping`; any `PyMongoError` → 503. |
| 4 | `compute_late_minutes`, L35 | `punch_in.replace(hour, minute)` builds the shift start in the punch's own time zone (naive server-local/UTC), not IST. | Punch 09:45 IST (04:15 UTC) on a 09:30 shift → compared with 09:30 *UTC*, gives 0 instead of 15. | `shift_bounds()` builds the start from the attendance date and shift time in IST. |
| 5 | `compute_late_minutes`, L36-37 | The grace test is on whole minutes (`minutes > 10`), not on "strictly more than 10:00". | 09:40:30 → `minutes == 10` → 0, expected 10. | `seconds > 600`, then `seconds // 60`. |
| 6 | `compute_late_minutes` / all helpers | Instants are not truncated to whole seconds (R1); overnight shifts aren't handled (a 05:00 punch on a 22:00-06:00 shift compares with 22:00 the same day → negative). | 09:40:00.900 must count as 09:40:00; overnight 05:00 punch gives 0 instead of 420. | `ms_to_dt` truncates; `attendance_date` / `shift_bounds` implement R1. |
| 7 | `compute_work_hours`, L41 | `round()` is float banker's rounding, not half-up. | 16074 s = 4.465 h → `round` gives 4.46, expected 4.47 (about 1 in 70 durations is off by 0.01). | `Decimal(seconds) / 3600` quantized with `ROUND_HALF_UP`. |
| 8 | `compute_overtime`, L47 | No 30-minute minimum (R3) and the shift end of an overnight shift isn't moved to the next day. | Out 18:35 on an 18:30 shift → 5 (expected 0). Overnight out 06:40 → ~ -930 → 0 (expected 40). | `overtime_minutes()` with `>= 30`; `shift_bounds` adds a day for overnight shifts. |
| 9 | `EmployeeIn`, L53-60 | No validation: any emp_code, email, shift string, date; `shift_start == shift_end` accepted. | `{"emp_code": "x", "joined_on": "2026-02-30", "shift_start": "25:99"}` → 201. | Pydantic patterns/lengths from the contract, real-date check, model validator for equal shift times → 422. |
| 10 | `PunchInIn`, L63-66 | `status` accepts any string (`ABSENT`, `foo`); `punched_at` is a lax int, so `"1783312500000"`, `1783312500.0` and seconds values are accepted. | Punch-in with `status: "ABSENT"` → 201; `punched_at: 1783312500` (seconds) → a 1970 record. | `Literal` presence status; `EpochMillis` = strict int in `[1e11, 4102444800000]`. |
| 11 | `create_employee`, L79-83 | Check-then-insert: two parallel creates both pass `find_one` and both insert. | 10 parallel identical POSTs → several 201s. | Unique index on `emp_code`; plain insert; `DuplicateKeyError` → 409. |
| 12 | `create_employee`, L82/L85 | `datetime.now()` is naive server-local time (stored as a wrong instant off-UTC), and is returned as an ISO string instead of epoch ms. | Run with `TZ=Asia/Kolkata`: `created_at` is 5.5 h off; response has `"2026-…T…"`. | `datetime.now(UTC)` (ms precision); response through `employee_out` → epoch ms. |
| 13 | `list_employees`, L93 | `skip = page * page_size`: page 1 skips the first page. | `GET /employees?page=1` with 3 employees and `page_size=2` returns only the 3rd. | `(page - 1) * page_size`. |
| 14 | `list_employees`, L94 | `total` counts the whole collection, ignoring the department filter. | `?department=Sales` → `total` = all employees. | `count_documents(same filter)`. |
| 15 | `list_employees`, L95 | No sort; the contract requires `emp_code` ascending. | Insert EMP0002 before EMP0001: order follows insertion. | `.sort("emp_code", 1)` backed by `{department, emp_code}` / `{emp_code}` indexes. |
| 16 | `list_employees` / `list_attendance`, L89/L130 | `page` / `page_size` unbounded. `page=0` → negative skip → PyMongo error → 500; `page_size=0` → `limit(0)` = **no limit**. | `?page=0` → 500; `?page_size=0` → the whole collection. | `page >= 1`, `1 <= page_size <= 100` → 422 otherwise. |
| 17 | `punch_in`, L101/L113 | Unknown employee: `emp` is `None` → `TypeError` → 500. | Punch-in for `EMP9999` → 500 instead of 404. | `find_employee()` → 404. |
| 18 | `punch_in`, L102-103 | `fromtimestamp` uses the server's local time zone and `date` is that local date, not the IST date per R1 (and overnight shifts are ignored). | On a UTC server, a punch at 02:00 IST is filed under the previous day. | UTC-aware `ms_to_dt`, then `attendance_date()` (IST + overnight rule). |
| 19 | `punch_in`, L104-118 | Check-then-insert race on (emp_code, date). | 20 parallel identical punch-ins → several 201s and duplicate records. | Unique index `{emp_code, date}`; plain insert; `DuplicateKeyError` → 409. |
| 20 | `punch_in` / `list_attendance`, L119/L149 | Leaks MongoDB's `_id` as `id`, which the contract says never appears. | Response contains `"id": "66a…"`. | Whitelisted serializer `record_out`. |
| 21 | `punch_in` / `list_attendance` | Instants returned as datetimes (ISO strings), not epoch ms; history times not converted. | `punch_in: "2026-07-06T03:58:00"` in the response. | `dt_to_ms` everywhere, including `history[].at` and `changes.punch_in/out`. |
| 22 | `list_attendance`, L144-147 | Loads the **whole** filtered collection into Python, then sorts and slices. | 100k records → slow, memory-heavy; `explain` shows COLLSCAN. | Filter, sort, skip and limit in MongoDB; `count_documents` for `total`. |
| 23 | `list_attendance`, L145 | Sorted by `date` only; ties in `emp_code` come back in arbitrary order (unstable across pages). | Two employees on the same date can swap between pages. | Sort `{date: -1, emp_code: 1}` backed by an index. |
| 24 | `list_attendance`, L125-143 | No validation: bad dates, unknown status and `date_from > date_to` are silently accepted. | `?date_from=2026-08-05&date_to=2026-08-01` → 200 with nothing; `?status=LATE` → 200. | Date validator, `Status` enum, explicit 422 for an inverted range. |
| 25 | `list_attendance` / `punch_in` | Legacy records without `half_day` / `history` are returned with those fields missing. | Sample doc #9 → response has no `history` key. | Defaults in `record_out` (`[]` / `false`). |

## Looked at and decided were not defects
- **`load_dotenv()`** without `override=True`: correct, real environment variables keep priority over `.env`.
- **Storing `date` as a `YYYY-MM-DD` string** and filtering it with `$gte`/`$lte`: correct, because the format sorts
  lexicographically in date order and the data model requires strings.
- **Sync (`def`) endpoints with PyMongo**: fine. FastAPI runs them in a thread pool and `MongoClient` is thread-safe.
- **`status_code=201` on the two POSTs**: matches the contract.
- **`insert_one` adding `_id` to the dict**: harmless once responses are built from a whitelist.
- **The helper functions as an idea**: reusing shared rule helpers for punch-in, punch-out and PATCH is right; their
  bodies were wrong, so they were rewritten rather than reused as-is.
