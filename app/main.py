"""Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI / MONGODB_URI, MONGO_DB (loaded from environment or optional .env)
"""
import os
import re
import json
from calendar import monthrange
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from dotenv import load_dotenv
from bson import json_util
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI") or os.getenv("MONGODB_URI") or "mongodb://localhost:27017"
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client: MongoClient = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = PRESENCE_STATUSES | {"ABSENT", "LEAVE"}
EPOCH_MIN = 100_000_000_000
EPOCH_MAX = 4_102_444_800_000


def create_indexes() -> None:
    """Creates all required indexes idempotently at startup."""
    db.employees.create_index("emp_code", unique=True, name="uq_employee_code")
    db.employees.create_index([("department", 1), ("joined_on", 1)], name="ix_department_joined")
    db.employees.create_index([("joined_on", 1), ("department", 1)], name="ix_joined_department")
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)], unique=True, name="uq_attendance_employee_date"
    )
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)], name="ix_employee_punch_in")
    db.attendance_logs.create_index(
        [("date", -1), ("emp_code", 1), ("status", 1)], name="ix_attendance_date_employee_status"
    )
    db.attendance_logs.create_index(
        [("date", 1), ("late_minutes", 1), ("emp_code", 1)], name="ix_attendance_date_late_emp"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def parse_date(value: str) -> date:
    """Parses and validates strict YYYY-MM-DD date."""
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(422, "date must be YYYY-MM-DD")
    if parsed.isoformat() != value:
        raise HTTPException(422, "date must be YYYY-MM-DD")
    return parsed


def parse_month(value: str) -> tuple[date, date]:
    """Parses YYYY-MM month string into (first_day, last_day)."""
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        raise HTTPException(422, "month must be YYYY-MM")
    year, month = map(int, value.split("-"))
    start = date(year, month, 1)
    end = date(year, month, monthrange(year, month)[1])
    return start, end


def from_epoch_ms(value: int) -> datetime:
    """Converts epoch milliseconds to timezone-aware UTC datetime truncated to seconds."""
    return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)


def as_utc(value: datetime) -> datetime:
    """Ensures datetime is timezone-aware UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    """Converts a datetime to integer epoch milliseconds."""
    if value is None:
        return None
    return int(as_utc(value).timestamp() * 1000)


def round_half_up(value: float | Decimal, places: int = 2) -> float:
    """Performs standard half-up arithmetic rounding (R8)."""
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def truncate_seconds(value: datetime) -> datetime:
    """Truncates microsecond portion to whole seconds in UTC (R1)."""
    return as_utc(value).replace(microsecond=0)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """Computes the IST attendance date for a punch-in, handling overnight shifts (R1)."""
    local = as_utc(punch_in).astimezone(IST)
    shift_start_time = time.fromisoformat(shift_start)
    shift_end_time = time.fromisoformat(shift_end)
    local_date = local.date()
    # Overnight shift: if punch-in IST time is strictly before shift_end, it belongs to previous day
    if shift_end_time <= shift_start_time and local.time().replace(tzinfo=None) < shift_end_time:
        local_date -= timedelta(days=1)
    return local_date.isoformat()


def shift_boundary(attendance_day: str, shift_time: str) -> datetime:
    """Builds a UTC datetime boundary for shift start in IST on the given attendance day."""
    day = parse_date(attendance_day)
    clock = time.fromisoformat(shift_time)
    return datetime.combine(day, clock, IST).astimezone(UTC)


def compute_late_minutes(punch_in: datetime, shift_start: str, attendance_day: str) -> int:
    """Computes late minutes with 10-minute grace period (R2)."""
    actual = truncate_seconds(punch_in)
    start = shift_boundary(attendance_day, shift_start)
    elapsed_seconds = int((actual - start).total_seconds())
    # Late only if strictly more than 10 minutes (600 seconds) after shift start
    return elapsed_seconds // 60 if elapsed_seconds > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """Computes work hours rounded half-up to 2 decimals (R4)."""
    seconds = int((truncate_seconds(punch_out) - truncate_seconds(punch_in)).total_seconds())
    return round_half_up(Decimal(seconds) / Decimal(3600), 2)


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    """Computes overtime minutes with 30-minute minimum threshold (R3)."""
    start_time = time.fromisoformat(shift_start)
    end_time = time.fromisoformat(shift_end)
    end_day = parse_date(date_str)
    # Overnight shift: shift end falls on next calendar day
    if end_time <= start_time:
        end_day += timedelta(days=1)
    end = datetime.combine(end_day, end_time, IST).astimezone(UTC)
    elapsed = int((truncate_seconds(punch_out) - end).total_seconds())
    minutes = elapsed // 60
    return minutes if minutes >= 30 else 0


def record_to_api(document: dict[str, Any]) -> dict[str, Any]:
    """Converts a database attendance record into the API response format."""
    result = {key: value for key, value in document.items() if key != "_id"}
    result.setdefault("work_hours", None)
    result.setdefault("late_minutes", 0)
    result.setdefault("overtime_minutes", 0)
    result.setdefault("half_day", False)
    result.setdefault("history", [])
    for field in ("punch_in", "punch_out"):
        result[field] = to_epoch_ms(result.get(field))
    result["history"] = [
        {
            "at": to_epoch_ms(entry["at"]),
            "by": entry["by"],
            "reason": entry["reason"],
            "changes": {
                key: {side: to_epoch_ms(value) if key in {"punch_in", "punch_out"} else value
                      for side, value in change.items()}
                for key, change in entry.get("changes", {}).items()
            },
        }
        for entry in result.get("history", [])
    ]
    return result


def ensure_valid_epoch(value: Optional[int]) -> Optional[int]:
    """Validates that an integer timestamp is within epoch millisecond bounds."""
    if value is not None and not EPOCH_MIN <= value <= EPOCH_MAX:
        raise ValueError(f"epoch milliseconds must be between {EPOCH_MIN} and {EPOCH_MAX}")
    return value


def run_db_aggregation(pipeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Runs a database-level aggregation pipeline (e.g. $documents) safely via db.command."""
    res = db.command("aggregate", 1, pipeline=pipeline, cursor={})
    cursor_info = res.get("cursor", {})
    docs = list(cursor_info.get("firstBatch", []))
    cursor_id = cursor_info.get("id", 0)
    while cursor_id:
        more = db.command("getMore", cursor_id, collection=1)
        cursor_info = more.get("cursor", {})
        docs.extend(cursor_info.get("firstBatch", []))
        cursor_id = cursor_info.get("id", 0)
    return docs


# --------------------------------------------------------------------------- #
# Request Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, value: str) -> str:
        try:
            parsed = date.fromisoformat(value)
        except (TypeError, ValueError):
            raise ValueError("joined_on must be YYYY-MM-DD")
        if parsed.isoformat() != value:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return value

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def valid_punched_at(cls, value: Optional[int]) -> Optional[int]:
        return ensure_valid_epoch(value)

    @field_validator("status")
    @classmethod
    def valid_presence_status(cls, value: str) -> str:
        if value not in PRESENCE_STATUSES:
            raise ValueError("status must be PRESENT, WFH, or ON_DUTY")
        return value

    @model_validator(mode="after")
    def reject_null_instant(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at cannot be null")
        return self


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None

    @field_validator("punched_at")
    @classmethod
    def valid_punched_at(cls, value: Optional[int]) -> Optional[int]:
        return ensure_valid_epoch(value)

    @model_validator(mode="after")
    def reject_null_instant(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at cannot be null")
        return self


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Optional[str] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def valid_status(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value not in ALL_STATUSES:
            raise ValueError("invalid status")
        return value

    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_instants(cls, value: Optional[int]) -> Optional[int]:
        return ensure_valid_epoch(value)

    @model_validator(mode="after")
    def reject_explicit_nulls(self):
        for field in ("status", "punch_in", "punch_out"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    """Readiness probe checking MongoDB connection."""
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeIn):
    """Creates a new employee with unique emp_code."""
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    doc["created_at"] = to_epoch_ms(doc["created_at"])
    doc.pop("_id", None)
    return doc


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Lists employees paginated and sorted by emp_code ascending."""
    q: dict[str, Any] = {}
    if department is not None:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size))
    for item in items:
        item["created_at"] = to_epoch_ms(item["created_at"])
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInIn):
    """Records an employee punch-in, deriving attendance date and late minutes."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = truncate_seconds(from_epoch_ms(body.punched_at) if body.punched_at is not None else datetime.now(UTC))
    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], d),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_to_api(doc)


def attendance_filter(
    emp_code: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    status: Optional[str],
) -> dict[str, Any]:
    """Validates and builds attendance query filters."""
    query: dict[str, Any] = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            parse_date(date_from)
            query["date"]["$gte"] = date_from
        if date_to:
            parse_date(date_to)
            query["date"]["$lte"] = date_to
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be on or before date_to")
    if status is not None:
        if status not in ALL_STATUSES:
            raise HTTPException(422, "invalid status")
        query["status"] = status
    return query


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Lists attendance records sorted by date DESC, emp_code ASC."""
    q = attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    page_docs = list(
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    page_docs = [record_to_api(d) for d in page_docs]
    return {"items": page_docs, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out", tags=["attendance"])
def punch_out(body: PunchOutIn):
    """Records an employee punch-out, closing the latest open punch-in record."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = truncate_seconds(from_epoch_ms(body.punched_at) if body.punched_at is not None else datetime.now(UTC))
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}},
        sort=[("punch_in", -1)],
    )
    if not record:
        raise HTTPException(404, "no punch-in found")
    punch_in_time = truncate_seconds(record["punch_in"])
    elapsed = (ts - punch_in_time).total_seconds()
    if elapsed <= 0 or elapsed > 24 * 60 * 60:
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")
    work_hours = compute_work_hours(punch_in_time, ts)
    overtime = compute_overtime(ts, emp["shift_start"], emp["shift_end"], record["date"])
    half_day = work_hours < 4.5
    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, "work_hours": work_hours,
                  "overtime_minutes": overtime, "half_day": half_day}},
    )
    if result.modified_count != 1:
        raise HTTPException(409, "record is already punched out")
    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return record_to_api(updated)


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    """Manually corrects an attendance record and appends to the audit history."""
    parsed_date = parse_date(date)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": parsed_date.isoformat()})
    if not old:
        raise HTTPException(404, "attendance record not found")

    supplied = body.model_fields_set
    if body.status in {"ABSENT", "LEAVE"} and supplied.intersection({"punch_in", "punch_out"}):
        raise HTTPException(422, "ABSENT and LEAVE cannot include punch times")
    new_status = body.status if "status" in supplied else old["status"]
    new_in = from_epoch_ms(body.punch_in) if "punch_in" in supplied else old.get("punch_in")
    new_out = from_epoch_ms(body.punch_out) if "punch_out" in supplied else old.get("punch_out")

    if new_status in {"ABSENT", "LEAVE"}:
        new_in = new_out = None
    elif new_in is None:
        raise HTTPException(422, "presence status requires punch_in")
    if new_in is not None:
        new_in = truncate_seconds(new_in)
        if attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != parsed_date.isoformat():
            raise HTTPException(422, "punch_in must belong to the record attendance date")
    if new_out is not None:
        new_out = truncate_seconds(new_out)
        if new_in is None:
            raise HTTPException(422, "punch_out requires punch_in")
        duration = (new_out - new_in).total_seconds()
        if duration <= 0 or duration > 24 * 60 * 60:
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    old_in = truncate_seconds(old["punch_in"]) if old.get("punch_in") is not None else None
    old_out = truncate_seconds(old["punch_out"]) if old.get("punch_out") is not None else None
    if new_status == old["status"] and new_in == old_in and new_out == old_out:
        raise HTTPException(422, "request does not change the record")

    if new_status in {"ABSENT", "LEAVE"}:
        derived = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    else:
        derived = {
            "work_hours": compute_work_hours(new_in, new_out) if new_out else None,
            "late_minutes": compute_late_minutes(new_in, emp["shift_start"], parsed_date.isoformat()),
            "overtime_minutes": compute_overtime(new_out, emp["shift_start"], emp["shift_end"], parsed_date.isoformat()) if new_out else 0,
            "half_day": compute_work_hours(new_in, new_out) < 4.5 if new_out else False,
        }
    new_values = {"status": new_status, "punch_in": new_in, "punch_out": new_out, **derived}
    old_values = {
        "status": old["status"], "punch_in": old_in, "punch_out": old_out,
        "work_hours": old.get("work_hours"), "late_minutes": old.get("late_minutes", 0),
        "overtime_minutes": old.get("overtime_minutes", 0), "half_day": old.get("half_day", False),
    }
    changes = {
        key: {"from": old_values[key], "to": value}
        for key, value in new_values.items()
        if old_values[key] != value
    }
    audit_changes = {
        key: {side: val for side, val in change.items()}
        for key, change in changes.items()
    }
    now = datetime.now(UTC).replace(microsecond=0)
    query = {
        "_id": old["_id"],
        "status": old["status"],
        "punch_in": old.get("punch_in"),
        "punch_out": old.get("punch_out"),
        "history": old["history"] if "history" in old else {"$exists": False},
    }
    update = {
        "$set": new_values,
        "$push": {
            "history": {
                "at": now,
                "by": body.regularized_by,
                "reason": body.reason,
                "changes": audit_changes,
            }
        },
    }
    if db.attendance_logs.update_one(query, update).modified_count != 1:
        raise HTTPException(409, "record changed concurrently; retry the correction")
    return record_to_api(db.attendance_logs.find_one({"_id": old["_id"]}))


def employee_monthly_pipeline(emp_code: str, month_start: date, month_end: date) -> list[dict[str, Any]]:
    """MongoDB aggregation pipeline for monthly employee attendance summary."""
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": month_start.isoformat(), "$lte": month_end.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {
                "$cond": [
                    {"$and": [
                        {"$in": ["$status", sorted(PRESENCE_STATUSES)]},
                        {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]},
                    ]},
                    {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0,
                ]
            }},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    """Monthly attendance summary for one employee."""
    employee = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
    if not employee:
        raise HTTPException(404, "employee not found")
    month_start, month_end = parse_month(month)
    summary = next(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, month_start, month_end)), {})
    joined_on = max(month_start, parse_date(employee["joined_on"]))
    working_days = sum(
        1 for offset in range(max(0, (month_end - joined_on).days + 1))
        if (joined_on + timedelta(days=offset)).weekday() < 5
    )
    attendance_pct = (
        round_half_up(Decimal(str(summary.get("present_days", 0))) * 100 / Decimal(working_days), 2)
        if working_days else None
    )
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(summary.get("present_days", 0), 1),
        "leave_days": summary.get("leave_days", 0),
        "late_count": summary.get("late_count", 0),
        "total_late_minutes": summary.get("total_late_minutes", 0),
        "total_overtime_minutes": summary.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


def department_summary_pipeline(month_start: date, month_end: date, department: Optional[str]) -> list[dict[str, Any]]:
    """MongoDB aggregation pipeline for cross-collection department summary."""
    employee_match: dict[str, Any] = {"joined_on": {"$lte": month_end.isoformat()}}
    if department is not None:
        employee_match["department"] = department
    return [
        {"$match": employee_match},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"code": "$emp_code"},
            "pipeline": [{"$match": {"$expr": {"$and": [
                {"$eq": ["$emp_code", "$$code"]},
                {"$gte": ["$date", month_start.isoformat()]},
                {"$lte": ["$date", month_end.isoformat()]},
            ]}}}],
            "as": "logs",
        }},
        {"$project": {
            "department": 1,
            "present_days": {"$sum": {"$map": {"input": "$logs", "as": "log", "in": {
                "$cond": [{"$and": [
                    {"$in": ["$$log.status", sorted(PRESENCE_STATUSES)]},
                    {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$$log.date"}}}, [2, 3, 4, 5, 6]]},
                ]}, {"$cond": [{"$ifNull": ["$$log.half_day", False]}, 0.5, 1]}, 0]
            }}}},
            "late_count": {"$size": {"$filter": {"input": "$logs", "as": "log", "cond": {"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]}}}},
            "total_late_minutes": {"$sum": {"$map": {"input": "$logs", "as": "log", "in": {"$ifNull": ["$$log.late_minutes", 0]}}}},
            "leave_count": {"$size": {"$filter": {"input": "$logs", "as": "log", "cond": {"$eq": ["$$log.status", "LEAVE"]}}}},
            "on_duty_count": {"$size": {"$filter": {"input": "$logs", "as": "log", "cond": {"$eq": ["$$log.status", "ON_DUTY"]}}}},
            "work_values": {"$map": {"input": {"$filter": {"input": "$logs", "as": "log", "cond": {"$and": [
                {"$in": ["$$log.status", sorted(PRESENCE_STATUSES)]},
                {"$ne": [{"$ifNull": ["$$log.work_hours", None]}, None]}
            ]}}}, "as": "log", "in": "$$log.work_hours"}},
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$present_days"},
            "late_count": {"$sum": "$late_count"},
            "total_late_minutes": {"$sum": "$total_late_minutes"},
            "leave_count": {"$sum": "$leave_count"},
            "on_duty_count": {"$sum": "$on_duty_count"},
            "work_values": {"$push": "$work_values"},
        }},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": 1,
            "present_days": 1,
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
            "work_values": {"$reduce": {"input": "$work_values", "initialValue": [], "in": {"$concatArrays": ["$$value", "$$this"]}}},
        }},
        {"$addFields": {
            "avg_work_hours": {"$cond": [
                {"$gt": [{"$size": "$work_values"}, 0]},
                {"$avg": "$work_values"},
                None,
            ]}
        }},
        {"$project": {"work_values": 0}},
        {"$sort": {"department": 1}},
    ]


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """Department summary for a month across all employees including zero-log staff."""
    month_start, month_end = parse_month(month)
    items = list(db.employees.aggregate(department_summary_pipeline(month_start, month_end, department)))
    for item in items:
        item["present_days"] = round_half_up(item["present_days"], 1)
        if item["avg_work_hours"] is not None:
            item["avg_work_hours"] = round_half_up(item["avg_work_hours"], 2)
    return {"month": month, "items": items}


def late_leaderboard_pipeline(month_start: date, month_end: date, department: Optional[str], limit: int) -> list[dict[str, Any]]:
    """MongoDB aggregation pipeline for competition late ranking with tie preservation."""
    pipeline: list[dict[str, Any]] = [
        {"$match": {"date": {"$gte": month_start.isoformat(), "$lte": month_end.isoformat()},
                    "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name",
                       "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])
    return pipeline


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    """Late-comers leaderboard with competition rank ties."""
    month_start, month_end = parse_month(month)
    items = list(db.attendance_logs.aggregate(late_leaderboard_pipeline(month_start, month_end, department, limit)))
    return {"month": month, "items": items}


def department_trend_pipeline(department: str, first: date, last: date) -> list[dict[str, Any]]:
    """MongoDB aggregation pipeline generating a daily series with gap-filling and moving average."""
    count_days = (last - first).days + 1
    start_datetime = datetime.combine(first, time.min, UTC)
    return [
        {"$documents": [{"_id": "range"}]},
        {"$set": {"days": {"$map": {
            "input": {"$range": [0, count_days]}, "as": "offset",
            "in": {"$dateAdd": {"startDate": start_datetime, "unit": "day", "amount": "$$offset"}},
        }}}},
        {"$unwind": "$days"},
        {"$set": {"date_text": {"$dateToString": {"date": "$days", "format": "%Y-%m-%d"}}}},
        {"$lookup": {
            "from": "employees", "let": {"day": "$date_text"},
            "pipeline": [{"$match": {"$expr": {"$and": [
                {"$eq": ["$department", department]}, {"$lte": ["$joined_on", "$$day"]}
            ]}}}, {"$count": "value"}], "as": "headcount_docs",
        }},
        {"$lookup": {
            "from": "attendance_logs", "let": {"day": "$date_text"},
            "pipeline": [
                {"$match": {"$expr": {"$eq": ["$date", "$$day"]}}},
                {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
                {"$match": {"employee.department": department}},
                {"$group": {
                    "_id": None,
                    "present_count": {"$sum": {"$cond": [
                        {"$in": ["$status", sorted(PRESENCE_STATUSES)]},
                        {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0,
                    ]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                }},
            ], "as": "attendance_docs",
        }},
        {"$set": {
            "is_working_day": {"$in": [{"$dayOfWeek": "$days"}, [2, 3, 4, 5, 6]]},
            "headcount": {"$ifNull": [{"$arrayElemAt": ["$headcount_docs.value", 0]}, 0]},
            "present_count": {"$ifNull": [{"$arrayElemAt": ["$attendance_docs.present_count", 0]}, 0]},
            "late_count": {"$ifNull": [{"$arrayElemAt": ["$attendance_docs.late_count", 0]}, 0]},
        }},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": [{"$floor": {"$add": [
                {"$multiply": [{"$divide": ["$present_count", "$headcount"]}, 10000]}, 0.5
            ]}}, 10000]}, None,
        ]}}},
        {"$setWindowFields": {"sortBy": {"days": 1}, "output": {
            "moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}
        }}},
        {"$project": {"_id": 0, "date": "$date_text", "is_working_day": 1, "headcount": 1,
                       "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):
    """Daily attendance trend with database-computed gap filling and 7-day moving average."""
    first, last = parse_date(from_date), parse_date(to_date)
    if last < first:
        raise HTTPException(422, "to must be on or after from")
    if (last - first).days + 1 > 92:
        raise HTTPException(422, "date range cannot exceed 92 days")
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, "department not found")
    pipeline = department_trend_pipeline(department, first, last)
    items = run_db_aggregation(pipeline)
    for item in items:
        for field in ("attendance_rate", "moving_avg_7d"):
            if item[field] is not None:
                item[field] = round_half_up(item[field], 4)
    return {"department": department, "items": items}


def explain_aggregation(collection: str | int, pipeline: list[dict[str, Any]]) -> dict[str, Any]:
    """Runs MongoDB explain with executionStats verbosity for an aggregation pipeline."""
    command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    return db.command("explain", command, verbosity="executionStats")


@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Returns MongoDB executionStats explain plan for the specified endpoint."""
    allowed = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in allowed:
        raise HTTPException(422, "unsupported endpoint")
    if status is not None and status not in ALL_STATUSES:
        raise HTTPException(422, "invalid status")
    if endpoint in {"employee_monthly", "department_summary", "late_leaderboard"} and month is None:
        raise HTTPException(422, "month is required")

    explain_collection: str | int
    if endpoint == "attendance_list":
        filters = attendance_filter(emp_code, date_from, date_to, status)
        query = {"find": "attendance_logs", "filter": filters,
                 "sort": {"date": -1, "emp_code": 1}, "skip": (page - 1) * page_size, "limit": page_size}
        result = db.command("explain", query, verbosity="executionStats")
        explain_collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(422, "emp_code is required")
        month_start, month_end = parse_month(month)
        pipeline = employee_monthly_pipeline(emp_code, month_start, month_end)
        explain_collection = "attendance_logs"
        result = explain_aggregation(explain_collection, pipeline)
    elif endpoint == "department_summary":
        month_start, month_end = parse_month(month)
        pipeline = department_summary_pipeline(month_start, month_end, department)
        explain_collection = "employees"
        result = explain_aggregation(explain_collection, pipeline)
    elif endpoint == "late_leaderboard":
        month_start, month_end = parse_month(month)
        pipeline = late_leaderboard_pipeline(month_start, month_end, department, limit)
        explain_collection = "attendance_logs"
        result = explain_aggregation(explain_collection, pipeline)
    else:
        if department is None or from_date is None or to_date is None:
            raise HTTPException(422, "department, from, and to are required")
        first, last = parse_date(from_date), parse_date(to_date)
        if last < first or (last - first).days + 1 > 92:
            raise HTTPException(422, "invalid trend date range")
        if db.employees.count_documents({"department": department}) == 0:
            raise HTTPException(404, "department not found")
        pipeline = department_trend_pipeline(department, first, last)
        explain_collection = "database"
        try:
            result = explain_aggregation(1, pipeline)
        except Exception:
            explain_collection = "attendance_logs"
            trend_sub_pipeline = [
                {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
                {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
                {"$match": {"employee.department": department}},
            ]
            result = explain_aggregation("attendance_logs", trend_sub_pipeline)
    return {
        "endpoint": endpoint,
        "collection": str(explain_collection),
        "explain": json.loads(json_util.dumps(result)),
    }
