# DECISIONS.md

1. **Indexes.** Unique `employees.emp_code` and unique `attendance_logs.(emp_code, date)` enforce identity and decide
   races. `(date desc, emp_code)` serves the attendance list's sort and the leaderboard's month range;
   `(status, date desc, emp_code)` the status filter; `(emp_code, punch_in desc)` punch-out's "latest open record";
   `(department, emp_code)`, `(department, joined_on)` and `(joined_on)` the employee list, trend headcount and summary.
   I rejected a separate `(emp_code)` index on logs: the unique compound index already has it as its prefix.

2. **Punch-in race.** Both requests compute the same `(emp_code, date)` and call `insert_one`. MongoDB's unique index
   lets exactly one through; the other raises `DuplicateKeyError`, which becomes 409. There's no read-before-write.

3. **Ties.** `$rank` gives tied employees the same rank and skips the next one. I keep every row with
   `rank <= limit`, so a tie at the cutoff returns all tied people, sorted by `emp_code`.

4. **Headcount.** The pipeline starts from `employees` filtered by `joined_on <= month end`, then `$lookup`s logs.
   Someone with no logs gets an empty array but still counts once.

5. **At 100x.** Precompute daily per-department aggregates when records change, so the trend and summary read small
   rollups instead of raw logs.
