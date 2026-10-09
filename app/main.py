"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (required; real environment variables win over a local .env)

Layout: config -> constants -> time rules (R1-R5) -> validation types -> serializers -> indexes
        -> query/pipeline builders (shared with /admin/explain) -> endpoints.
"""
import calendar
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from bson import Decimal128, json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from pydantic import AfterValidator, BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
load_dotenv()  # override=False: real environment variables take priority


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"environment variable {name} is required")
    return value


client = MongoClient(_required_env("MONGO_URI"), tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[_required_env("MONGO_DB")]
employees = db.employees
logs = db.attendance_logs

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
GRACE_SECONDS = 10 * 60
MIN_OVERTIME_MINUTES = 30
HALF_DAY_HOURS = 4.5
MAX_SHIFT = timedelta(hours=24)
MAX_TREND_DAYS = 92
TRACKED_FIELDS = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")
TIME_FIELDS = ("punch_in", "punch_out")

# --------------------------------------------------------------------------- #
# Time rules (R1 - R5)
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime, truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:  # defensive: BSON dates are always UTC
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def truncate_seconds(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)


def now_utc() -> datetime:
    return truncate_seconds(datetime.now(UTC))


def now_bson() -> datetime:
    """Server time at the precision BSON stores (ms), so what we return equals what we store."""
    now = datetime.now(UTC)
    return now.replace(microsecond=now.microsecond // 1000 * 1000)


def parse_hhmm(value: str) -> time:
    hours, minutes = map(int, value.split(":"))
    return time(hours, minutes)


def is_overnight(emp: dict) -> bool:
    return emp["shift_end"] <= emp["shift_start"]


def attendance_date(punch_in: datetime, emp: dict) -> str:
    """R1: IST date of the punch-in; early-morning punches of an overnight shift belong to the previous day."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(emp) and local.time() < parse_hhmm(emp["shift_end"]):
        day -= timedelta(days=1)
    return day.isoformat()


def shift_bounds(date_str: str, emp: dict) -> tuple[datetime, datetime]:
    day = date.fromisoformat(date_str)
    start = datetime.combine(day, parse_hhmm(emp["shift_start"]), IST)
    end = datetime.combine(day, parse_hhmm(emp["shift_end"]), IST)
    if is_overnight(emp):
        end += timedelta(days=1)
    return start, end


def late_minutes(punch_in: datetime, shift_start: datetime) -> int:
    """R2: late only when strictly more than 10:00 after shift start; minutes counted from shift start."""
    seconds = (punch_in - shift_start) // timedelta(seconds=1)
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def overtime_minutes(punch_out: datetime, shift_end: datetime) -> int:
    """R3: whole minutes after shift end, counted only from 30 upwards."""
    minutes = (punch_out - shift_end) // timedelta(minutes=1)
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def quantize_half_up(value: Decimal, places: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def round_half_up(value, places: int) -> Optional[float]:
    """R8. Python's round() and Mongo's $round both round half to even, so neither is used."""
    if value is None:
        return None
    if isinstance(value, Decimal128):
        value = value.to_decimal()
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return float(quantize_half_up(value, places))


def work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: elapsed seconds / 3600, 2 decimals half-up."""
    seconds = (punch_out - punch_in) // timedelta(seconds=1)
    return float(quantize_half_up(Decimal(seconds) / 3600, 2))


def derive(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime], date_str: str, emp: dict) -> dict:
    """All derived fields of a record, per the table in DATA_MODEL.md section 3."""
    if status not in PRESENCE:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    start, end = shift_bounds(date_str, emp)
    punch_in = truncate_seconds(punch_in)
    derived = {"work_hours": None, "late_minutes": late_minutes(punch_in, start), "overtime_minutes": 0, "half_day": False}
    if punch_out is not None:
        punch_out = truncate_seconds(punch_out)
        hours = work_hours(punch_in, punch_out)
        derived.update(work_hours=hours, overtime_minutes=overtime_minutes(punch_out, end), half_day=hours < HALF_DAY_HOURS)
    return derived


# --------------------------------------------------------------------------- #
# Validation types
# --------------------------------------------------------------------------- #
def _check_date(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("must be a date in YYYY-MM-DD format")
    date.fromisoformat(value)  # rejects impossible dates such as 2026-02-30
    return value


EpochMillis = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
DateStr = Annotated[str, AfterValidator(_check_date)]
OptDate = Annotated[Optional[str], AfterValidator(_check_date)]
HHMM = Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]
Limit = Annotated[int, Query(ge=1, le=50)]


def validation_error(loc: tuple, msg: str) -> RequestValidationError:
    """A 422 with FastAPI's default body shape, for checks that span several fields."""
    return RequestValidationError([{"type": "value_error", "loc": loc, "msg": msg, "input": None}])


class EmployeeIn(BaseModel):
    emp_code: Annotated[str, Field(pattern=r"^EMP\d{4,6}$")]
    name: Annotated[str, Field(min_length=1, max_length=100)]
    email: Annotated[str, Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, Field(min_length=1, max_length=50)]
    shift_start: HHMM = "09:30"
    shift_end: HHMM = "18:30"
    joined_on: DateStr

    @model_validator(mode="after")
    def _shift_has_length(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: Annotated[str, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[str, Field(min_length=1, max_length=50)]

    @field_validator("status", "punch_in", "punch_out", mode="before")
    @classmethod
    def _omit_rather_than_null(cls, value):
        if value is None:
            raise ValueError("omit the field to leave it unchanged; null is not allowed")
        return value


# --------------------------------------------------------------------------- #
# Serializers (whitelists: _id never leaves the service, instants leave as epoch ms)
# --------------------------------------------------------------------------- #
def employee_out(doc: dict) -> dict:
    out = {k: doc.get(k) for k in ("emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on")}
    out["created_at"] = dt_to_ms(doc.get("created_at"))
    return out


def history_out(entry: dict) -> dict:
    changes = {}
    for field, change in (entry.get("changes") or {}).items():
        if field in TIME_FIELDS:
            change = {"from": dt_to_ms(change.get("from")), "to": dt_to_ms(change.get("to"))}
        changes[field] = change
    return {"at": dt_to_ms(entry.get("at")), "by": entry.get("by"), "reason": entry.get("reason"), "changes": changes}


def normalized(doc: dict) -> dict:
    """The tracked fields of a stored record, with the defaults for legacy/missing fields applied."""
    return {
        "status": doc["status"],
        "punch_in": doc.get("punch_in"),
        "punch_out": doc.get("punch_out"),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day") or False),
    }


def record_out(doc: dict) -> dict:
    fields = normalized(doc)
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        **fields,
        "punch_in": dt_to_ms(fields["punch_in"]),
        "punch_out": dt_to_ms(fields["punch_out"]),
        "history": [history_out(h) for h in doc.get("history") or []],
    }


# --------------------------------------------------------------------------- #
# Indexes (DECISIONS.md lists what each one serves)
# --------------------------------------------------------------------------- #
INDEXES = {
    "employees": [
        ([("emp_code", 1)], {"name": "emp_code_unique", "unique": True}),
        ([("department", 1), ("emp_code", 1)], {"name": "department_emp_code"}),
        ([("department", 1), ("joined_on", 1)], {"name": "department_joined_on"}),
        ([("joined_on", 1)], {"name": "joined_on"}),
    ],
    "attendance_logs": [
        ([("emp_code", 1), ("date", 1)], {"name": "emp_code_date_unique", "unique": True}),
        ([("date", -1), ("emp_code", 1)], {"name": "date_desc_emp_code"}),
        ([("status", 1), ("date", -1), ("emp_code", 1)], {"name": "status_date_emp_code"}),
        ([("emp_code", 1), ("punch_in", -1)], {"name": "emp_code_punch_in"}),
    ],
}


def ensure_indexes() -> None:
    for collection, specs in INDEXES.items():
        for keys, options in specs:
            db[collection].create_index(keys, **options)  # no-op when it already exists


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Query / pipeline builders (each one is used by its endpoint AND by /admin/explain)
# --------------------------------------------------------------------------- #
def month_range(month: str) -> tuple[str, str]:
    year, mon = map(int, month.split("-"))
    return f"{month}-01", f"{month}-{calendar.monthrange(year, mon)[1]:02d}"


def weekday_of(date_expr) -> dict:
    """ISO weekday (1 = Monday ... 7 = Sunday) of a YYYY-MM-DD string."""
    return {"$isoDayOfWeek": {"$dateFromString": {"dateString": date_expr, "format": "%Y-%m-%d"}}}


def presence_value(status, half_day) -> dict:
    """1 for a presence record, 0.5 for a half day, 0 otherwise (R5, R6)."""
    return {"$cond": [{"$in": [status, PRESENCE]}, {"$cond": [{"$eq": [{"$ifNull": [half_day, False]}, True]}, 0.5, 1]}, 0]}


def weekday_presence_value(status, half_day, date_expr) -> dict:
    """presence_value, but only for Monday - Friday (R7)."""
    return {"$cond": [{"$lte": [weekday_of(date_expr), 5]}, presence_value(status, half_day), 0]}


def is_late(late) -> dict:
    return {"$gt": [{"$ifNull": [late, 0]}, 0]}


def build_attendance_query(emp_code, date_from, date_to, status, page, page_size) -> dict:
    if date_from and date_to and date_from > date_to:
        raise validation_error(("query", "date_from"), "date_from must be on or before date_to")
    flt = {}
    if emp_code is not None:
        flt["emp_code"] = emp_code
    if date_from or date_to:
        flt["date"] = {}
        if date_from:
            flt["date"]["$gte"] = date_from
        if date_to:
            flt["date"]["$lte"] = date_to
    if status is not None:
        flt["status"] = status
    return {"filter": flt, "sort": {"date": -1, "emp_code": 1}, "skip": (page - 1) * page_size, "limit": page_size}


def build_employee_monthly_pipeline(emp_code: str, month: str) -> list:
    first, last = month_range(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": weekday_presence_value("$status", "$half_day", "$date")},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [is_late("$late_minutes"), 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def build_department_summary_pipeline(month: str, department: Optional[str]) -> list:
    first, last = month_range(month)
    match = {"joined_on": {"$lte": last}}  # R9: joined on or before the end of the month
    if department is not None:
        match["department"] = department

    def count_where(cond):
        return {"$size": {"$filter": {"input": "$logs", "as": "l", "cond": cond}}}

    timed_presence = {"$filter": {"input": "$logs", "as": "l", "cond": {"$and": [
        {"$in": ["$$l.status", PRESENCE]},
        {"$ne": [{"$ifNull": ["$$l.work_hours", None]}, None]},
    ]}}}
    return [
        {"$match": match},
        # Starting from employees keeps people with zero logs: they get an empty `logs` array.
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first, "$lte": last}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "work_hours": 1, "late_minutes": 1}},
            ],
            "as": "logs",
        }},
        {"$project": {
            "department": 1,
            "present_days": {"$sum": {"$map": {"input": "$logs", "as": "l", "in":
                weekday_presence_value("$$l.status", "$$l.half_day", "$$l.date")}}},
            # Sum and count are carried separately so the average is over records, not over employees.
            "wh_sum": {"$sum": {"$map": {"input": timed_presence, "as": "l", "in": {"$toDecimal": "$$l.work_hours"}}}},
            "wh_count": {"$size": timed_presence},
            "late_count": count_where(is_late("$$l.late_minutes")),
            "total_late_minutes": {"$sum": {"$map": {"input": "$logs", "as": "l", "in": {"$ifNull": ["$$l.late_minutes", 0]}}}},
            "leave_count": count_where({"$eq": ["$$l.status", "LEAVE"]}),
            "on_duty_count": count_where({"$eq": ["$$l.status", "ON_DUTY"]}),
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$present_days"},
            "wh_sum": {"$sum": "$wh_sum"},
            "wh_count": {"$sum": "$wh_count"},
            "late_count": {"$sum": "$late_count"},
            "total_late_minutes": {"$sum": "$total_late_minutes"},
            "leave_count": {"$sum": "$leave_count"},
            "on_duty_count": {"$sum": "$on_duty_count"},
        }},
        {"$sort": {"_id": 1}},
    ]


def build_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    first, last = month_range(month)
    pipeline = [
        {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "pipeline": [{"$project": {"_id": 0, "name": 1, "department": 1}}],
            "as": "emp",
        }},
        {"$unwind": "$emp"},  # drops logs whose employee does not exist
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipeline += [
        # $rank is standard competition ranking: ties share a rank, the next rank is skipped (1, 2, 2, 4).
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies after ranking, so ties at the cutoff stay
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0, "rank": "$rank", "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department",
            "total_late_minutes": "$total_late_minutes", "late_count": "$late_count",
        }},
    ]
    return pipeline


def mongo_half_up(expr, places: int) -> dict:
    """Half-up rounding of a non-negative Decimal128 expression ($round would round half to even)."""
    factor = 10 ** places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expr, factor]}, Decimal128("0.5")]}}, factor]}


def trend_range(from_: str, to: str) -> None:
    days = (date.fromisoformat(to) - date.fromisoformat(from_)).days + 1
    if days < 1:
        raise validation_error(("query", "to"), "to must be on or after from")
    if days > MAX_TREND_DAYS:
        raise validation_error(("query", "to"), f"the range may span at most {MAX_TREND_DAYS} days")


def department_codes(department: str) -> list[str]:
    codes = employees.distinct("emp_code", {"department": department})
    if not codes:
        raise HTTPException(404, "unknown department")
    return codes


def build_trend_pipeline(department: str, codes: list[str], from_: str, to: str) -> list:
    start = datetime.combine(date.fromisoformat(from_), time(), UTC)
    end_exclusive = datetime.combine(date.fromisoformat(to), time(), UTC) + timedelta(days=1)
    return [
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": from_, "$lte": to}}},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": presence_value("$status", "$half_day")},
            "late_count": {"$sum": {"$cond": [is_late("$late_minutes"), 1, 0]}},
        }},
        # A seed row for the first day, so $densify has something to fill from even when there are no logs.
        {"$unionWith": {"pipeline": [{"$documents": [{"_id": from_, "present_count": 0, "late_count": 0}]}]}},
        {"$group": {"_id": "$_id", "present_count": {"$sum": "$present_count"}, "late_count": {"$sum": "$late_count"}}},
        {"$addFields": {"day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [start, end_exclusive]}}},
        {"$addFields": {
            "date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]},
        }},
        {"$lookup": {  # R9 headcount for each day
            "from": "employees",
            "let": {"day": "$date"},
            "pipeline": [
                {"$match": {"department": department}},
                {"$match": {"$expr": {"$lte": ["$joined_on", "$$day"]}}},
                {"$count": "n"},
            ],
            "as": "headcount",
        }},
        {"$addFields": {"headcount": {"$ifNull": [{"$first": "$headcount.n"}, 0]}}},
        {"$addFields": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            mongo_half_up({"$divide": [{"$toDecimal": "$present_count"}, {"$toDecimal": "$headcount"}]}, 4),
            None,
        ]}}},
        # Only rows inside the range are in the pipeline, so the first rows naturally get a shorter window.
        # $avg ignores nulls and returns null when the window holds none.
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {
            "moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}},
        }}},
        {"$sort": {"day": 1}},
        {"$project": {
            "_id": 0, "date": "$date", "is_working_day": "$is_working_day", "headcount": "$headcount",
            "present_count": "$present_count", "late_count": "$late_count", "attendance_rate": "$attendance_rate",
            "moving_avg_7d": "$moving_avg_7d",
        }},
    ]


# --------------------------------------------------------------------------- #
# Endpoints: system and employees
# --------------------------------------------------------------------------- #
def find_employee(emp_code: str) -> dict:
    emp = employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    return emp


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_bson()
    try:
        employees.insert_one(doc)  # the unique index decides races, not a prior find
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    flt = {} if department is None else {"department": department}
    total = employees.count_documents(flt)
    cursor = employees.find(flt).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Endpoints: attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = find_employee(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date(ts, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive(body.status, ts, None, day, emp),
        "history": [],
    }
    try:
        logs.insert_one(doc)  # unique (emp_code, date): exactly one concurrent insert wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = find_employee(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    # Search by instant, not by date, so an overnight shift is found after midnight.
    rec = logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", -1)])
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    started = truncate_seconds(rec["punch_in"])
    if ts <= started or ts - started > MAX_SHIFT:
        raise validation_error(("body", "punched_at"), "punch-out must be after punch-in and within 24 hours")
    derived = derive(rec["status"], rec["punch_in"], ts, rec["date"], emp)
    derived.pop("late_minutes")  # set at punch-in; punching out does not change it
    updated = logs.find_one_and_update(
        # Only one concurrent punch-out can match; a concurrent PATCH of punch_in/status also makes this miss.
        {"_id": rec["_id"], "punch_out": None, "punch_in": rec["punch_in"], "status": rec["status"]},
        {"$set": {"punch_out": ts, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: OptDate = None,
    date_to: OptDate = None,
    status: Optional[Status] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    q = build_attendance_query(emp_code, date_from, date_to, status, page, page_size)
    total = logs.count_documents(q["filter"])
    cursor = logs.find(q["filter"]).sort(list(q["sort"].items())).skip(q["skip"]).limit(q["limit"])
    return {"items": [record_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


def _same(a, b) -> bool:
    if isinstance(a, datetime) or isinstance(b, datetime):
        return dt_to_ms(a) == dt_to_ms(b)
    return a == b


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: DateStr, body: RegularizeIn):
    emp = find_employee(emp_code)
    rec = logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")
    old = normalized(rec)
    status = body.status or old["status"]

    if status in PRESENCE:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else old["punch_in"]
        if new_in is None:
            raise validation_error(("body", "punch_in"), "a presence status requires punch_in")
        if body.punch_in is not None and attendance_date(new_in, emp) != date:
            raise validation_error(("body", "punch_in"), "punch_in must stay on the record's attendance date")
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else old["punch_out"]
        if new_out is not None:
            gap = truncate_seconds(new_out) - truncate_seconds(new_in)
            if gap <= timedelta(0) or gap > MAX_SHIFT:
                raise validation_error(("body", "punch_out"), "punch_out must be after punch_in and within 24 hours")
    else:
        if body.punch_in is not None or body.punch_out is not None:
            raise validation_error(("body", "status"), f"{status} clears punch times; do not send punch_in/punch_out")
        new_in = new_out = None

    new = {"status": status, "punch_in": new_in, "punch_out": new_out, **derive(status, new_in, new_out, date, emp)}
    changes = {f: {"from": old[f], "to": new[f]} for f in TRACKED_FIELDS if not _same(old[f], new[f])}
    if not changes:
        raise validation_error(("body",), "the request changes nothing")

    entry = {"at": now_bson(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    seen = len(rec.get("history") or [])
    # Optimistic concurrency: write only if the record still looks exactly as we read it.
    guard = {
        "_id": rec["_id"],
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        f"history.{seen}": {"$exists": False},
    }
    if seen:
        guard[f"history.{seen - 1}"] = {"$exists": True}
    updated = logs.find_one_and_update(
        guard, {"$set": new, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER
    )
    if updated is None:
        raise HTTPException(409, "the record was changed by another request; retry")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# Endpoints: analytics
# --------------------------------------------------------------------------- #
def working_days(month: str, joined_on: str) -> int:
    """R7: Monday - Friday from max(first of month, joined_on) to the end of the month."""
    first, last = (date.fromisoformat(d) for d in month_range(month))
    day = max(first, date.fromisoformat(joined_on))
    count = 0
    while day <= last:
        count += day.weekday() < 5
        day += timedelta(days=1)
    return count


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: Annotated[str, Query(pattern=MONTH_PATTERN)]):
    emp = find_employee(emp_code)
    row = next(logs.aggregate(build_employee_monthly_pipeline(emp_code, month)), {})
    days = working_days(month, emp["joined_on"])
    present = row.get("present_days", 0)
    pct = None if days == 0 else round_half_up(Decimal(str(present)) * 100 / days, 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": days,
        "present_days": round_half_up(present, 2),
        "leave_days": int(row.get("leave_days", 0)),
        "late_count": int(row.get("late_count", 0)),
        "total_late_minutes": int(row.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(row.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: Annotated[str, Query(pattern=MONTH_PATTERN)], department: Optional[str] = None):
    items = []
    for row in employees.aggregate(build_department_summary_pipeline(month, department)):
        wh_sum = row["wh_sum"].to_decimal() if isinstance(row["wh_sum"], Decimal128) else Decimal(row["wh_sum"])
        items.append({
            "department": row["_id"],
            "headcount": row["headcount"],
            "present_days": round_half_up(row["present_days"], 2),
            "avg_work_hours": round_half_up(wh_sum / row["wh_count"], 2) if row["wh_count"] else None,
            "late_count": row["late_count"],
            "total_late_minutes": row["total_late_minutes"],
            "leave_count": row["leave_count"],
            "on_duty_count": row["on_duty_count"],
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: Annotated[str, Query(pattern=MONTH_PATTERN)], limit: Limit = 10, department: Optional[str] = None
):
    return {"month": month, "items": list(logs.aggregate(build_leaderboard_pipeline(month, limit, department)))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: Annotated[DateStr, Query(alias="from")],
    to: Annotated[DateStr, Query()],
):
    trend_range(from_, to)
    codes = department_codes(department)
    items = []
    for row in logs.aggregate(build_trend_pipeline(department, codes, from_, to)):
        row["attendance_rate"] = round_half_up(row["attendance_rate"], 4)
        row["moving_avg_7d"] = round_half_up(row["moving_avg_7d"], 4)
        items.append(row)
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Endpoints: admin
# --------------------------------------------------------------------------- #
ExplainTarget = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


def _require(name: str, value):
    if value is None:
        raise validation_error(("query", name), "Field required")
    return value


@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Annotated[Optional[str], Query(pattern=MONTH_PATTERN)] = None,
    department: Optional[str] = None,
    limit: Limit = 10,
    date_from: OptDate = None,
    date_to: OptDate = None,
    status: Optional[Status] = None,
    from_: Annotated[OptDate, Query(alias="from")] = None,
    to: OptDate = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    if endpoint == "attendance_list":
        collection = "attendance_logs"
        q = build_attendance_query(emp_code, date_from, date_to, status, page, page_size)
        command = {"find": collection, **q}
    else:
        if endpoint == "employee_monthly":
            collection = "attendance_logs"
            pipeline = build_employee_monthly_pipeline(_require("emp_code", emp_code), _require("month", month))
        elif endpoint == "department_summary":
            collection = "employees"
            pipeline = build_department_summary_pipeline(_require("month", month), department)
        elif endpoint == "late_leaderboard":
            collection = "attendance_logs"
            pipeline = build_leaderboard_pipeline(_require("month", month), limit, department)
        else:
            collection = "attendance_logs"
            dept, start, end = _require("department", department), _require("from", from_), _require("to", to)
            trend_range(start, end)
            pipeline = build_trend_pipeline(dept, department_codes(dept), start, end)
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    plan = db.command("explain", command, verbosity="executionStats")
    plan = json.loads(json_util.dumps(plan, json_options=json_util.RELAXED_JSON_OPTIONS))
    return {"endpoint": endpoint, "collection": collection, "explain": plan}
