# CourierBridge

A FastAPI, SQLite/Postgres, and Jinja2 app for managing courier shipments.

## Local Development

1. Copy `.env.example` to `.env`.
2. Keep local development on SQLite unless you intentionally want to touch production data:

```env
COURIERBRIDGE_DATABASE_URL=sqlite:///./courierbridge.db
COURIERBRIDGE_REQUIRE_AUTH=false
```

3. Run on Windows:

```bat
run.bat
```

4. Open `http://localhost:8001`.

## Production Deployment

Recommended setup:

- App: Render web service
- Database: Supabase Postgres
- URL: Render's generated `.onrender.com` URL

### Supabase

1. Create a Supabase project.
2. Open Project Settings > Database and copy the connection string.
3. Use SQLAlchemy's psycopg driver form on Render:

```env
COURIERBRIDGE_DATABASE_URL=postgresql+psycopg://USER:PASSWORD@HOST:5432/postgres?sslmode=require
```

The app also normalizes `postgres://` and `postgresql://` URLs to `postgresql+psycopg://` automatically.

### Render

This repo includes `render.yaml`, `Procfile`, and `runtime.txt`.

1. Push the `prod` branch to GitHub.
2. In Render, create a new Web Service from this GitHub repo.
3. Select the `prod` branch.
4. Use the Render YAML config if prompted, or use:

```bash
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

5. Set these environment variables in Render:

```env
COURIERBRIDGE_DATABASE_URL=postgresql+psycopg://...
COURIERBRIDGE_REQUIRE_AUTH=true
COURIERBRIDGE_ACCESS_PASSWORD=your-strong-login-password
COURIERBRIDGE_SECRET_KEY=generate-a-long-random-secret
```

Production auth fails closed: if `COURIERBRIDGE_REQUIRE_AUTH=true` but the password or secret key is missing, the app will not serve shipment/customer data. Only `/health`, `/static`, and `/login` are reachable.

Health check:

```text
/health
```

### Generate A Secret Key

Use PowerShell:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Use the output as `COURIERBRIDGE_SECRET_KEY`.

## Migrating Local SQLite Data To Supabase

After setting up Supabase, run this locally once:

```powershell
$env:TARGET_DATABASE_URL="postgresql+psycopg://USER:PASSWORD@HOST:5432/postgres?sslmode=require"
.\venv\Scripts\python.exe scripts\migrate_sqlite_to_database.py
```

By default, the script reads `./courierbridge.db` and refuses to import into non-empty target tables. This avoids accidental duplicate imports.

## Notes

- Local SQLite and production Supabase are separate databases unless you point local `.env` to Supabase.
- The app creates missing tables on startup with SQLAlchemy metadata.
- For serious long-term schema changes, add Alembic migrations later.

## Staff Attendance Phases 1-3

CourierBridge can prepare staff attendance suggestions from the owner's authorized Jibble organization. These phases import attendance, map Jibble people to local employees, configure effective-dated salary/attendance policies, and calculate daily attendance suggestions. Payroll finalization, advances, bonuses, forgiveness, leave requests, payslips, overrides, and facial recognition are intentionally not included yet.

### Required Environment Variables

```env
JIBBLE_SESSION_COOKIE=replace-with-current-owner-session-cookie
JIBBLE_TIMEZONE=Asia/Kolkata
```

The cookie is used only server-side for Jibble silent authentication. It is never stored in the database and is never returned by an API. If it is missing, startup logs only `JIBBLE_SESSION_COOKIE is missing`.

### Renewing The Jibble Cookie

1. Log in to Jibble in a browser as the authorized owner.
2. Copy the current Jibble session cookie value from browser dev tools.
3. Paste it into local `.env` or the Render environment variable `JIBBLE_SESSION_COOKIE`.
4. Restart the app so the new environment value is loaded.

Do not paste real cookies into commits, docs, issues, screenshots, or prompts.

### Monthly Sync

```bash
curl -X POST "https://your-render-app.onrender.com/admin/attendance/sync/jibble?month=2026-07" \
  -H "Cookie: courierbridge_auth=your-app-login-cookie"
```

The sync is idempotent for a month. Re-running the same month updates corrected Jibble days, counts identical days as unchanged, and never deletes previous attendance because a later sync fails.

### Integration Health

```bash
curl "https://your-render-app.onrender.com/admin/attendance/integrations/jibble/status" \
  -H "Cookie: courierbridge_auth=your-app-login-cookie"
```

States include `CONNECTED`, `REAUTH_REQUIRED`, `RATE_LIMITED`, `SCHEMA_CHANGED`, and `TEMPORARILY_UNAVAILABLE`. If authentication fails, existing attendance remains available and nobody is newly marked absent.

### Employees And Mapping

Create local employees first:

```bash
curl -X POST "https://your-render-app.onrender.com/admin/employees" \
  -H "Content-Type: application/json" \
  -H "Cookie: courierbridge_auth=your-app-login-cookie" \
  -d '{"employee_code":"EMP001","full_name":"Employee Name","active":true,"joining_date":"2026-07-01","default_timezone":"Asia/Kolkata"}'
```

Find unmapped Jibble people:

```bash
curl "https://your-render-app.onrender.com/admin/attendance/external-identities?source=JIBBLE&mapped=false" \
  -H "Cookie: courierbridge_auth=your-app-login-cookie"
```

Map by stable Jibble `personId`, not by name:

```bash
curl -X POST "https://your-render-app.onrender.com/admin/employees/1/external-mappings/jibble" \
  -H "Content-Type: application/json" \
  -H "Cookie: courierbridge_auth=your-app-login-cookie" \
  -d '{"external_person_id":"jibble-person-id"}'
```

One Jibble identity can map to only one employee. Mapping updates historical imported rows and recalculates affected attendance.

### Salary And Attendance Policies

Policies are effective-dated. Create a new policy when salary or shift timings change instead of overwriting history.

```bash
curl -X POST "https://your-render-app.onrender.com/admin/employees/1/attendance-policies" \
  -H "Content-Type: application/json" \
  -H "Cookie: courierbridge_auth=your-app-login-cookie" \
  -d '{"monthly_salary_paise":2500000,"salary_divisor_type":"FIXED_DAYS","fixed_salary_divisor":26,"shift_start_local":"10:00:00","shift_end_local":"19:00:00","grace_minutes":10,"full_day_required_minutes":480,"half_day_required_minutes":240,"missing_clock_out_buffer_minutes":60,"weekly_off_days":["SUNDAY"],"effective_from":"2026-07-01","active":true}'
```

Money is stored as integer paise. Durations are stored as integer seconds/minutes.

### Recalculate And Query

```bash
curl -X POST "https://your-render-app.onrender.com/admin/attendance/recalculate?month=2026-07" \
  -H "Cookie: courierbridge_auth=your-app-login-cookie"
```

```bash
curl "https://your-render-app.onrender.com/admin/attendance/daily?month=2026-07&needs_review=true" \
  -H "Cookie: courierbridge_auth=your-app-login-cookie"
```

### Attendance Statuses

`NOT_EVALUATED`, `NOT_APPLICABLE`, `IN_PROGRESS`, `PRESENT`, `LATE_PRESENT`, `HALF_DAY`, `ABSENT`, `PAID_LEAVE`, `UNPAID_LEAVE`, `WEEKLY_OFF`, `MISSING_CLOCK_OUT`, `MISSING_CLOCK_IN`, `POLICY_MISSING`, `UNMAPPED_EMPLOYEE`, and `SOURCE_ERROR`.

Calculated deductions are suggestions only. They do not finalize payroll and do not directly deduct salary in Phases 1-3.

### Private Jibble Flow Risk

This uses Jibble's private dashboard silent authentication flow, not an official payroll API. Jibble can change the response shape or authentication behavior. If that happens, CourierBridge marks the integration as `SCHEMA_CHANGED`, `REAUTH_REQUIRED`, or `TEMPORARILY_UNAVAILABLE`, preserves previous imports, and avoids marking employees absent because of sync failure.

A manual CSV import source should be added later as a fallback without changing the calculation engine.
