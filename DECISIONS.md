# DECISIONS.md

1. **Indexes.** `emp_code` is unique on employees, and `(emp_code, date)` is unique on attendance logs. They stop
   duplicates and make the race rules work. `(date desc, emp_code)` serves the attendance list and month lookups.
   `(status, date desc, emp_code)` is for the status filter. `(emp_code, punch_in desc)` lets punch-out find the latest
   record. On employees, `(department, emp_code)`, `(department, joined_on)` and `joined_on` help the employee
   list, the trend and the summary. I skipped a plain `emp_code` index on logs; the unique one already starts with it.

2. **Punch-in race.** I don't check first and then insert. Both requests just try to insert. The unique index lets one
   through, and the other gets a duplicate error, which I return as 409.

3. **Ties.** Tied people share a rank and the next one is skipped (1, 2, 2, 4). I keep everyone ranked within `limit`,
   so a tie at the cutoff can add extra rows. Ties are ordered by `emp_code`.

4. **Headcount.** I start from employees who joined by the month's end, then attach their logs. Someone with no logs
   gets an empty list but still counts.

5. **At 100x.** I'd keep daily totals per department, updated on every change, so reports read those instead of every log.
