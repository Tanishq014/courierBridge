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
