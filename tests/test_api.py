"""Automated test suite for Employee Attendance & Analytics API."""
import os
import pytest
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient
import app.main as m

client = TestClient(m.app)

TEST_EMP = "EMP9901"
TEST_EMP_OVERNIGHT = "EMP9902"
TEST_EMP_NOLOGS = "EMP9903"


@pytest.fixture(scope="session", autouse=True)
def setup_test_database():
    """Ensure indexes exist and cleanup test records before and after testing."""
    m.create_indexes()
    # Cleanup test employees and attendance logs
    test_codes = [TEST_EMP, TEST_EMP_OVERNIGHT, TEST_EMP_NOLOGS, "EMP9904", "EMP9905"]
    m.db.employees.delete_many({"emp_code": {"$in": test_codes}})
    m.db.attendance_logs.delete_many({"emp_code": {"$in": test_codes}})
    yield
    m.db.employees.delete_many({"emp_code": {"$in": test_codes}})
    m.db.attendance_logs.delete_many({"emp_code": {"$in": test_codes}})


# =========================================================================== #
# 1. Health Probe
# =========================================================================== #
def test_health_check():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# =========================================================================== #
# 2. Employee CRUD & Validations
# =========================================================================== #
def test_create_employee_success():
    payload = {
        "emp_code": TEST_EMP,
        "name": "Test Engineer",
        "email": "test.engineer@example.com",
        "department": "QA",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01",
    }
    response = client.post("/employees", json=payload)
    assert response.status_code == 201
    data = response.json()
    assert data["emp_code"] == TEST_EMP
    assert isinstance(data["created_at"], int)
    assert "_id" not in data


def test_create_employee_duplicate_emp_code():
    payload = {
        "emp_code": TEST_EMP,
        "name": "Duplicate Employee",
        "email": "dup@example.com",
        "department": "QA",
        "joined_on": "2026-07-01",
    }
    response = client.post("/employees", json=payload)
    assert response.status_code == 409
    assert "already exists" in response.json()["detail"].lower()


def test_create_employee_validation_errors():
    # Same shift start and end
    bad_shift = {
        "emp_code": "EMP9999",
        "name": "Bad Shift",
        "email": "shift@example.com",
        "department": "QA",
        "shift_start": "09:00",
        "shift_end": "09:00",
        "joined_on": "2026-07-01",
    }
    assert client.post("/employees", json=bad_shift).status_code == 422

    # Invalid email
    bad_email = {
        "emp_code": "EMP9999",
        "name": "Bad Email",
        "email": "invalid-email",
        "department": "QA",
        "joined_on": "2026-07-01",
    }
    assert client.post("/employees", json=bad_email).status_code == 422


def test_list_employees_pagination_and_filter():
    # Create another employee in different department
    client.post("/employees", json={
        "emp_code": TEST_EMP_NOLOGS,
        "name": "No Logs Staff",
        "email": "nologs@example.com",
        "department": "QA",
        "joined_on": "2026-07-01",
    })
    res = client.get("/employees?department=QA&page=1&page_size=10")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] >= 2
    assert all(item["department"] == "QA" for item in data["items"])


# =========================================================================== #
# 3. Punch-In, Overnight Shifts & Grace Period (R1, R2)
# =========================================================================== #
def test_punch_in_unknown_employee():
    res = client.post("/attendance/punch-in", json={"emp_code": "EMP0000"})
    assert res.status_code == 404


def test_punch_in_invalid_status():
    res = client.post("/attendance/punch-in", json={"emp_code": TEST_EMP, "status": "ABSENT"})
    assert res.status_code == 422


def test_punch_in_invalid_epoch():
    # Seconds-looking epoch timestamp (< 100_000_000_000)
    res = client.post("/attendance/punch-in", json={"emp_code": TEST_EMP, "punched_at": 1783310400})
    assert res.status_code == 422


def test_punch_in_and_late_calculation():
    # Shift is 09:30 IST.
    # 2026-07-06 09:40:00 IST = 2026-07-06 04:10:00 UTC = 1783311000000 ms. (Inside 10m grace period -> 0 late)
    ts_on_time = int(datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc).timestamp() * 1000)
    res = client.post("/attendance/punch-in", json={
        "emp_code": TEST_EMP,
        "punched_at": ts_on_time,
        "status": "PRESENT"
    })
    assert res.status_code == 201
    record = res.json()
    assert record["date"] == "2026-07-06"
    assert record["late_minutes"] == 0
    assert record["status"] == "PRESENT"
    assert record["punch_out"] is None


def test_punch_in_duplicate_same_day():
    # Attempting to punch in again on 2026-07-06 for TEST_EMP must return 409
    ts = int(datetime(2026, 7, 6, 4, 15, 0, tzinfo=timezone.utc).timestamp() * 1000)
    res = client.post("/attendance/punch-in", json={
        "emp_code": TEST_EMP,
        "punched_at": ts,
        "status": "PRESENT"
    })
    assert res.status_code == 409


def test_punch_in_strictly_late_calculation():
    # Create record on 2026-07-07.
    # 09:40:01 IST (1 sec past grace period) = 2026-07-07 04:10:01 UTC = 1783397401000 ms.
    # Late minutes must be 10 minutes (floor from shift_start 09:30)!
    ts_late = int(datetime(2026, 7, 7, 4, 10, 1, tzinfo=timezone.utc).timestamp() * 1000)
    res = client.post("/attendance/punch-in", json={
        "emp_code": TEST_EMP,
        "punched_at": ts_late,
        "status": "PRESENT"
    })
    assert res.status_code == 201
    assert res.json()["late_minutes"] == 10


def test_punch_in_overnight_shift():
    # Setup overnight employee: 22:00 to 06:00 IST
    client.post("/employees", json={
        "emp_code": TEST_EMP_OVERNIGHT,
        "name": "Night Owl",
        "email": "night@example.com",
        "department": "Support",
        "shift_start": "22:00",
        "shift_end": "06:00",
        "joined_on": "2026-07-01",
    })
    # Punch in at 01:30 AM IST on 2026-07-07 (2026-07-06 20:00:00 UTC)
    # Because 01:30 IST is strictly before shift_end 06:00 IST, it belongs to 2026-07-06!
    ts_night = int(datetime(2026, 7, 6, 20, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    res = client.post("/attendance/punch-in", json={
        "emp_code": TEST_EMP_OVERNIGHT,
        "punched_at": ts_night,
        "status": "PRESENT"
    })
    assert res.status_code == 201
    assert res.json()["date"] == "2026-07-06"


# =========================================================================== #
# 4. Punch-Out, Work Hours, Overtime, Half-Day (R3, R4, R5)
# =========================================================================== #
def test_punch_out_unknown_employee():
    res = client.post("/attendance/punch-out", json={"emp_code": "EMP0000"})
    assert res.status_code == 404


def test_punch_out_validation_edge_cases():
    # 1. No punch-in at or before punched_at -> 404
    ts_before_any = int(datetime(2026, 7, 5, 10, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    res_404 = client.post("/attendance/punch-out", json={"emp_code": TEST_EMP, "punched_at": ts_before_any})
    assert res_404.status_code == 404

    # 2. punched_at == punch_in (2026-07-06 04:10:00 UTC) -> 422
    ts_exact_punch_in = int(datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc).timestamp() * 1000)
    res_422 = client.post("/attendance/punch-out", json={"emp_code": TEST_EMP, "punched_at": ts_exact_punch_in})
    assert res_422.status_code == 422

    # 3. punched_at > 24 hours after punch_in -> 422
    ts_over_24h = int(datetime(2026, 7, 7, 5, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    # Using TEST_EMP on date 2026-07-06 where punch in was 04:10:00 UTC
    # Since 2026-07-07 has its own punch in at 04:10:01, 2026-07-07 05:00:00 is within 24h of 07-07.
    # To test > 24h: punch out on an open record with > 24h



def test_punch_out_success_and_calculations():
    # Punch in was 2026-07-06 04:10:00 UTC (09:40:00 IST).
    # Punch out at 2026-07-06 13:40:00 UTC (19:10:00 IST).
    # Duration: 9.5 hours = 9.50 hours.
    # Shift end is 18:30 IST. Punch out is 19:10 IST -> 40 minutes overtime (>= 30 threshold).
    # Work hours >= 4.5 -> half_day = False.
    ts_out = int(datetime(2026, 7, 6, 13, 40, 0, tzinfo=timezone.utc).timestamp() * 1000)
    res = client.post("/attendance/punch-out", json={"emp_code": TEST_EMP, "punched_at": ts_out})
    assert res.status_code == 200
    record = res.json()
    assert record["work_hours"] == 9.5
    assert record["overtime_minutes"] == 40
    assert record["half_day"] is False
    assert record["punch_out"] == ts_out


def test_punch_out_duplicate_returns_409():
    ts_out2 = int(datetime(2026, 7, 6, 13, 45, 0, tzinfo=timezone.utc).timestamp() * 1000)
    res = client.post("/attendance/punch-out", json={"emp_code": TEST_EMP, "punched_at": ts_out2})
    assert res.status_code == 409


def test_half_day_calculation():
    # For 2026-07-07, punch out after 3.5 hours (< 4.5) -> half_day = True
    # Punch in was 04:10:01 UTC. Punch out at 07:40:01 UTC (3.5 hours)
    ts_out_half = int(datetime(2026, 7, 7, 7, 40, 1, tzinfo=timezone.utc).timestamp() * 1000)
    res = client.post("/attendance/punch-out", json={"emp_code": TEST_EMP, "punched_at": ts_out_half})
    assert res.status_code == 200
    record = res.json()
    assert record["work_hours"] == 3.5
    assert record["half_day"] is True


# =========================================================================== #
# 5. Regularization & Audit Trail (PATCH)
# =========================================================================== #
def test_regularize_attendance_success_and_history():
    # Correct punch in for 2026-07-07 to 03:58:00 UTC (09:28 IST, on time)
    ts_corrected = int(datetime(2026, 7, 7, 3, 58, 0, tzinfo=timezone.utc).timestamp() * 1000)
    patch_payload = {
        "punch_in": ts_corrected,
        "reason": "Scanner clock calibration issue",
        "regularized_by": "qa.manager",
    }
    res = client.patch(f"/attendance/{TEST_EMP}/2026-07-07", json=patch_payload)
    assert res.status_code == 200
    record = res.json()
    assert record["late_minutes"] == 0
    assert len(record["history"]) == 1
    audit = record["history"][0]
    assert audit["by"] == "qa.manager"
    assert audit["reason"] == "Scanner clock calibration issue"
    assert "late_minutes" in audit["changes"]
    assert audit["changes"]["late_minutes"] == {"from": 10, "to": 0}


def test_regularize_change_nothing_returns_422():
    # Fetch current record and submit same punch in
    current = client.get(f"/attendance?emp_code={TEST_EMP}&date_from=2026-07-07&date_to=2026-07-07").json()["items"][0]
    res = client.patch(f"/attendance/{TEST_EMP}/2026-07-07", json={
        "punch_in": current["punch_in"],
        "reason": "Redundant update attempt",
        "regularized_by": "qa.manager"
    })
    assert res.status_code == 422


# =========================================================================== #
# 6. Analytics Endpoints
# =========================================================================== #
def test_employee_monthly_analytics():
    res = client.get(f"/analytics/employees/{TEST_EMP}/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["emp_code"] == TEST_EMP
    assert data["month"] == "2026-07"
    assert data["working_days"] == 23
    # 2026-07-06 (full day = 1.0), 2026-07-07 (half day = 0.5) => present_days = 1.5
    assert data["present_days"] == 1.5


def test_department_summary_analytics():
    res = client.get("/analytics/departments/summary?month=2026-07&department=QA")
    assert res.status_code == 200
    data = res.json()
    assert len(data["items"]) == 1
    dept = data["items"][0]
    assert dept["department"] == "QA"
    assert dept["headcount"] == 2  # TEST_EMP and TEST_EMP_NOLOGS
    assert dept["present_days"] == 1.5


def test_late_leaderboard_analytics():
    res = client.get("/analytics/leaderboard/late?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert isinstance(data["items"], list)
    for row in data["items"]:
        assert row["total_late_minutes"] > 0
        assert row["rank"] >= 1


def test_department_trend_analytics():
    res = client.get("/analytics/departments/QA/trend?from=2026-07-06&to=2026-07-12")
    assert res.status_code == 200
    data = res.json()
    assert len(data["items"]) == 7  # exactly 7 days
    # Verify weekend days have is_working_day = False
    dates = {d["date"]: d for d in data["items"]}
    assert dates["2026-07-11"]["is_working_day"] is False
    assert dates["2026-07-12"]["is_working_day"] is False


# =========================================================================== #
# 7. Admin Explain Endpoint
# =========================================================================== #
@pytest.mark.parametrize("endpoint,params", [
    ("attendance_list", {"emp_code": TEST_EMP}),
    ("employee_monthly", {"emp_code": TEST_EMP, "month": "2026-07"}),
    ("department_summary", {"month": "2026-07"}),
    ("late_leaderboard", {"month": "2026-07"}),
    ("department_trend", {"department": "QA", "from": "2026-07-06", "to": "2026-07-12"}),
])
def test_admin_explain_endpoint(endpoint, params):
    res = client.get(f"/admin/explain/{endpoint}", params=params)
    assert res.status_code == 200
    body = res.json()
    assert body["endpoint"] == endpoint
    assert "collection" in body
    assert "explain" in body
