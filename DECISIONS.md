# Design Decisions

### 1. Indexes and Query Optimization
I created five primary indexes at startup:
- **`employees`**: Unique `emp_code` enforces business identifier uniqueness and supports list sorting. Compound indexes `(department, joined_on)` and `(joined_on, department)` optimize department-filtered headcount and trend lookups.
- **`attendance_logs`**: Unique composite `(emp_code, date)` guarantees one record per employee-day, powers monthly lookups, and provides atomic race protection. `(emp_code, punch_in: -1)` enables $O(1)$ lookup for the latest open punch-in. Compound `(date: -1, emp_code: 1, status: 1)` optimizes list pagination and date-range filtering. Compound `(date: 1, late_minutes: 1, emp_code: 1)` provides an index scan (`IXSCAN`) for the late leaderboard.
I deliberately omitted a standalone low-cardinality `status` index to prevent index bloat.

### 2. Concurrency and Punch-In Protection
When two identical punch-ins arrive simultaneously for the same employee and date, both requests load the employee profile and compute identical IST attendance dates. Both then issue `insert_one`. Because the unique composite index `(emp_code, date)` is enforced at the database storage engine level, MongoDB commits exactly one insertion and rejects the second with `DuplicateKeyError` (code 11000). The service catches this exception and returns HTTP 409 Conflict. No vulnerable check-then-insert application logic is trusted.

### 3. Leaderboard Ranking and Cutoff Ties
The late leaderboard uses MongoDB's native `$setWindowFields` stage with the `$rank` operator to implement standard competition ranking (1, 2, 2, 4). The `rank <= limit` filter is applied **after** the rank calculation. Consequently, if multiple employees tie at the cutoff rank (e.g. two employees tied at rank 10 when limit is 10), both are returned, intentionally yielding 11 rows. Rows sharing the same rank and late minutes are deterministically sorted by `emp_code` ascending.

### 4. Department Summary Zero-Log Headcount
The department summary aggregation pipeline roots its query in the `employees` collection matching `joined_on <= month_end` (and department if specified). It then performs a `$lookup` into `attendance_logs` matching the month's date bounds. Employees with zero attendance logs remain in the pipeline stream with an empty `logs` array, contributing 1 to the department `headcount` before grouping. The `avg_work_hours` metric is calculated across flattened individual record durations, avoiding distorted "averages of averages".

### 5. Scaling for 100x Data
At 100x scale (~10 million records):
1. **Materialized Aggregations / Change Streams**: I would implement incremental rollups (daily and monthly summary tables) updated asynchronously via MongoDB Change Streams on punch-out and regularization, replacing repeated scans of raw logs.
2. **Horizontal Sharding**: I would shard `attendance_logs` on a compound shard key `{emp_code: "hashed", date: 1}` to distribute write throughput while keeping individual employee monthly queries routed to single shards.
3. **Partitioned Archival**: Historical records beyond 12 months would transition to colder tiered storage.
