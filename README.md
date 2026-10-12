# Employee Attendance & Analytics API

A high-performance, robust REST API for employee attendance tracking, regularization, and multi-dimensional analytics built with **FastAPI**, **MongoDB**, **PyMongo**, and **Pydantic**.

Complies strictly with OpenAPI 3.0.3 contract and business rules R1–R10.

---

## Features

- **Employee Management**: Validated creation and department-filtered paginated listing. Uniqueness of `emp_code` enforced by unique database index.
- **Race-Safe Attendance**:
  - `POST /attendance/punch-in`: Derives IST attendance dates (including overnight shifts), calculates late minutes with 10-minute grace period (R2), and guarantees exactly one record per employee-day under concurrent requests via composite unique indexing.
  - `POST /attendance/punch-out`: Closes the latest open punch-in, computing work hours rounded half-up (R4), overtime (R3, 30-min threshold), and half-day status (R5) using atomic condition updates.
  - `PATCH /attendance/{emp_code}/{date}`: Regularizes records with optimistic concurrency control, recomputing derived fields and appending an immutable audit trail (`history`) with field-level deltas.
- **High-Performance MongoDB Aggregations**:
  - Monthly Employee Summary (`GET /analytics/employees/{emp_code}/monthly`): Computes working days (Mon–Fri), presence days (0.5 for half-days), leave count, late totals, and attendance percentage.
  - Department Summary (`GET /analytics/departments/summary`): Cross-collection aggregation including employees with zero logs, calculating department headcount, total presence days, and unweighted average work hours across records.
  - Late-Comers Leaderboard (`GET /analytics/leaderboard/late`): Standard competition ranking (`$rank`, e.g. 1, 2, 2, 4) retaining ties at the cutoff threshold.
  - Daily Department Trend (`GET /analytics/departments/{department}/trend`): Gap-filling for weekends and missing days with a 7-day moving window average computed directly in the database.
- **Explain Endpoint (`GET /admin/explain/{endpoint}`)**: Returns MongoDB `executionStats` plans demonstrating index scans (`IXSCAN`) and zero collection scans (`COLLSCAN`).

---

## Prerequisites

- **Python**: 3.11+ (Python 3.12 recommended)
- **MongoDB**: 6.0+ (Local Docker or MongoDB Atlas)

---

## Installation & Setup

1. **Clone the repository**:
   ```bash
   git clone <repository_url> employee-attendance-api
   cd employee-attendance-api
   ```

2. **Create and activate a virtual environment**:
   ```bash
   python -m venv .venv
   # On Linux/macOS:
   source .venv/bin/activate
   # On Windows (PowerShell):
   .\.venv\Scripts\Activate.ps1
   ```

3. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure Environment Variables**:
   Set `MONGO_URI` (or `MONGODB_URI`) and `MONGO_DB` via environment variables or a local `.env` file (never commit `.env`):
   ```bash
   # Example
   export MONGO_URI="mongodb://localhost:27017"
   export MONGO_DB="attendance_db"
   ```

5. **Start the API Server**:
   ```bash
   uvicorn app.main:app --port 8000 --reload
   ```

6. **Interactive Documentation**:
   - Swagger UI: [http://localhost:8000/docs](http://localhost:8000/docs)
   - ReDoc: [http://localhost:8000/redoc](http://localhost:8000/redoc)

---

## Running Automated Tests

Run the test suite using `pytest`:
```bash
pytest -v
```

Tests validate:
- Readiness probe and database health
- Employee creation, unique constraint enforcement, and pagination
- Race-safe punch-in and punch-out handling
- Overnight shift boundaries and grace period calculations
- Manual regularizations and audit trail preservation
- Complex monthly, department, leaderboard, and trend analytics
- Admin explain execution stats verification

---

## Example API Requests

### 1. Health Probe
```bash
curl -X GET "http://localhost:8000/health"
```
**Response (200 OK)**:
```json
{
  "status": "ok"
}
```

### 2. Create Employee
```bash
curl -X POST "http://localhost:8000/employees" \
  -H "Content-Type: application/json" \
  -d '{
    "emp_code": "EMP0101",
    "name": "Ananya Sharma",
    "email": "ananya@example.com",
    "department": "Engineering",
    "shift_start": "09:30",
    "shift_end": "18:30",
    "joined_on": "2026-07-01"
  }'
```

### 3. Record Punch-In
```bash
curl -X POST "http://localhost:8000/attendance/punch-in" \
  -H "Content-Type: application/json" \
  -d '{
    "emp_code": "EMP0101",
    "punched_at": 1783310400000,
    "status": "PRESENT"
  }'
```

### 4. Record Punch-Out
```bash
curl -X POST "http://localhost:8000/attendance/punch-out" \
  -H "Content-Type: application/json" \
  -d '{
    "emp_code": "EMP0101",
    "punched_at": 1783344600000
  }'
```

### 5. Regularize Attendance (Audit Trail)
```bash
curl -X PATCH "http://localhost:8000/attendance/EMP0101/2026-07-06" \
  -H "Content-Type: application/json" \
  -d '{
    "punch_in": 1783310280000,
    "reason": "Biometric machine scanner delayed punch",
    "regularized_by": "hr.lead"
  }'
```

### 6. Explain Query Plan
```bash
curl -X GET "http://localhost:8000/admin/explain/employee_monthly?emp_code=EMP0101&month=2026-07"
```

---

## Known Limitations & Design Notes

1. **Authentication**: The assignment specification specifies free-text `regularized_by` without auth tokens. In a production enterprise system, OAuth2/JWT tokens with RBAC would identify actors.
2. **Holiday Calendar**: Working days adhere strictly to rule R7 (Monday through Friday without a custom holiday calendar).
3. **MongoDB Atlas Shared Proxy**: On MongoDB Atlas M0 free tier, database-level pipeline explain (`{aggregate: 1}`) is intercepted by the Atlas driver proxy; the explain endpoint falls back to explaining the underlying indexed collection query.
