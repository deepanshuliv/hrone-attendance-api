# REVIEW.md

These are the problems I found in the starter `app/main.py`. Line numbers point to the **original starter**, which is
the first commit in this repository.

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | top of file, L22-23 | If `MONGO_URI` or `MONGO_DB` is missing, it quietly falls back to a hard-coded localhost database. | Start the app without them set: it connects to `localhost/attendance_db` instead of telling you something is wrong. | Both are now required, and the app stops with a clear message if one is missing. A `.env` file still works, but real environment variables win. |
| 2 | whole file | No indexes are ever created, not even the unique ones that stop duplicates. | `explain` on `GET /attendance` shows `COLLSCAN`, and you can insert the same employee twice. | The app creates all the indexes it needs when it starts. Running it again is safe. |
| 3 | `health`, L72-74 | Always says "ok", even when MongoDB is down. | Stop MongoDB and call `/health`: you still get 200. | It now pings MongoDB and returns 503 if that fails. |
| 4 | `compute_late_minutes`, L35 | Builds the shift start in the punch's own time zone instead of India time (IST). | A 09:45 IST punch (04:15 UTC) on a 09:30 shift is compared with 09:30 UTC, so it gives 0 instead of 15. | Shift start is now built from the attendance date and the shift time in IST. |
| 5 | `compute_late_minutes`, L36-37 | Checks lateness in whole minutes (`minutes > 10`), but the rule is "strictly more than 10:00". | A punch at 09:40:30 gives 0. It should be 10. | It checks seconds instead: late only above 600 seconds, then counts whole minutes from the shift start. |
| 6 | `compute_late_minutes` and the other helpers | Milliseconds aren't dropped before the maths (R1), and night shifts are wrong: for a 22:00-06:00 shift, a 05:00 punch is compared with 22:00 the same day. | 09:40:00.900 should count as 09:40:00. A night-shift punch at 05:00 gives 0 late minutes instead of 420. | Milliseconds are dropped as soon as a time comes in. A new date helper puts early-morning night-shift punches on the previous day. |
| 7 | `compute_work_hours`, L41 | Python's `round()` doesn't always round a 5 up. | 16074 seconds is 4.465 hours: `round` gives 4.46, the right answer is 4.47. About 1 in 70 durations comes out 0.01 off. | It now uses exact decimal maths that always rounds a 5 up. |
| 8 | `compute_overtime`, L47 | There's no 30-minute minimum, and for a night shift the shift end isn't moved to the next day. | Leaving at 18:35 on an 18:30 shift gives 5 (should be 0). A night shift ending 06:40 gives 0 instead of 40. | Overtime only counts from 30 minutes up, and a night shift ends on the next day. |
| 9 | `EmployeeIn`, L53-60 | Nothing is checked: any emp_code, email, shift time or date goes in, even the same start and end time. | `{"emp_code": "x", "joined_on": "2026-02-30", "shift_start": "25:99"}` gets a 201. | Every field is checked against the contract's format, and a start time equal to the end time is rejected with 422. |
| 10 | `PunchInIn`, L63-66 | `status` takes any text (even `ABSENT`), and `punched_at` takes strings, decimals and values in seconds. | A punch-in with `status: "ABSENT"` gets a 201. `punched_at: 1783312500` (seconds) creates a 1970 record. | Only PRESENT, WFH or ON_DUTY are accepted. `punched_at` must be a whole number in the millisecond range. |
| 11 | `create_employee`, L79-83 | It checks whether the employee exists, then inserts. Two requests at the same moment can both pass the check. | Send 10 identical creates at once: more than one gets 201. | The unique index decides. I just insert, and a duplicate becomes 409. |
| 12 | `create_employee`, L82 and L85 | `created_at` uses the server's local clock with no time zone, and comes back as a date string instead of milliseconds. | Run the server with `TZ=Asia/Kolkata`: `created_at` is 5.5 hours off, and the response shows `"2026-…"`. | It now uses UTC time and returns milliseconds. |
| 13 | `list_employees`, L93 | `skip = page * page_size` skips the whole first page. | With 3 employees and `page_size=2`, page 1 shows only the third one. | `(page - 1) * page_size`. |
| 14 | `list_employees`, L94 | `total` counts every employee and ignores the department filter. | `?department=Sales` reports the total for all departments. | `total` uses the same filter as the list. |
| 15 | `list_employees`, L95 | No sorting, but the contract wants `emp_code` order. | Add EMP0002 before EMP0001: the list comes back in the order they were added. | Sorted by `emp_code`, backed by an index. |
| 16 | `list_employees` / `list_attendance`, L89 and L130 | `page` and `page_size` have no limits. `page=0` crashes, and `page_size=0` returns everything. | `?page=0` gives a 500. `?page_size=0` returns the whole collection. | `page` must be 1 or more and `page_size` between 1 and 100, otherwise 422. |
| 17 | `punch_in`, L101 and L113 | If the employee doesn't exist, the code crashes. | Punch in for `EMP9999`: you get a 500 instead of a 404. | It looks up the employee first and returns 404 if they don't exist. |
| 18 | `punch_in`, L102-103 | The date comes from the server's own time zone, not India time, and night shifts are ignored. | On a UTC server, a punch at 02:00 IST is saved under the previous day. | The date is worked out in IST with the night-shift rule (R1). |
| 19 | `punch_in`, L104-118 | Same check-then-insert race as #11, for one person on one day. | Send 20 identical punch-ins at once: several get 201 and you end up with duplicate records. | A unique index on `(emp_code, date)`. I just insert, and a duplicate becomes 409. |
| 20 | `punch_in` / `list_attendance`, L119 and L149 | Leaks MongoDB's internal `_id` as `id`, which the contract says must never show up. | Responses contain `"id": "66a…"`. | Responses are built only from the fields the contract lists. |
| 21 | `punch_in` / `list_attendance` | Times come back as date strings instead of milliseconds, including the ones inside history. | `punch_in: "2026-07-06T03:58:00"` in the response. | Every time is turned into milliseconds on the way out, history included. |
| 22 | `list_attendance`, L144-147 | Pulls every matching record into Python, then sorts and slices there. | With 100k records it's slow and uses a lot of memory, and `explain` shows a `COLLSCAN`. | Filtering, sorting and paging all happen inside MongoDB, and `total` is counted there too. |
| 23 | `list_attendance`, L145 | Sorts by date only, so people on the same date can come back in any order and jump between pages. | Two employees on the same date can swap places from one page to the next. | Sorted by date (newest first), then `emp_code`, backed by an index. |
| 24 | `list_attendance`, L125-143 | Bad input is never checked: wrong dates, an unknown status, or `date_from` after `date_to`. | `?date_from=2026-08-05&date_to=2026-08-01` returns an empty 200. `?status=LATE` returns 200. | Dates and status are checked, and a backwards date range gives 422. |
| 25 | `list_attendance` / `punch_in` | Older records without `half_day` or `history` come back with those fields missing. | Sample record #9 comes back with no `history` field. | Missing fields are filled with `[]` and `false` on the way out. |

## Things I looked at that are fine
- **`load_dotenv()`** without `override=True` is correct: real environment variables should win over `.env`.
- **Saving `date` as a `"YYYY-MM-DD"` string** and comparing it with `$gte` / `$lte` works, because that format sorts
  in date order. The data model asks for strings anyway.
- **Plain `def` endpoints with PyMongo** are fine. FastAPI runs them in separate threads, and the MongoDB client is safe
  to share between threads.
- **`status_code=201`** on the two POST endpoints matches the contract.
- **`insert_one` adding `_id` to the dict** doesn't matter once responses only include the allowed fields.
- **Having shared helpers** for the time rules is a good idea, since punch-in, punch-out and PATCH all need them. The
  maths inside them was wrong, so I rewrote the bodies instead of reusing them as they were.
