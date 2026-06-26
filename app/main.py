from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from app.database import engine, Base
from sqlalchemy import inspect, text
import hmac
import os
import time


ACCESS_PASSWORD = os.environ.get("COURIERBRIDGE_ACCESS_PASSWORD", "").strip()
AUTH_SECRET_KEY = os.environ.get("COURIERBRIDGE_SECRET_KEY", "").strip()
REQUIRE_AUTH = os.environ.get("COURIERBRIDGE_REQUIRE_AUTH", "").strip().lower() in {"1", "true", "yes", "on"}
AUTH_COOKIE_NAME = "courierbridge_auth"
AUTH_COOKIE_MAX_AGE = 60 * 60 * 12


LOGIN_PAGE = """
<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>CourierBridge Login</title>
    <style>
        body { margin: 0; min-height: 100vh; display: grid; place-items: center; font-family: Arial, sans-serif; background: #f3f5f1; color: #17211b; }
        form { width: min(360px, calc(100vw - 32px)); display: grid; gap: 12px; padding: 24px; background: #fff; border: 1px solid #dde4de; border-radius: 8px; box-shadow: 0 12px 32px rgba(23, 33, 27, 0.08); }
        h1 { margin: 0 0 4px; font-size: 20px; }
        label { font-size: 12px; font-weight: 700; color: #415048; }
        input { min-height: 42px; padding: 0 12px; border: 1px solid #cbd5cd; border-radius: 7px; font: inherit; }
        button { min-height: 42px; border: 0; border-radius: 7px; background: #174c3c; color: #fff; font-weight: 800; cursor: pointer; }
        .error { margin: 0; color: #a33a3a; font-size: 13px; font-weight: 700; }
    </style>
</head>
<body>
    <form method="post" action="/login" autocomplete="off">
        <h1>CourierBridge</h1>
        <label for="access-password">Password</label>
        <input id="access-password" type="password" name="password" autocomplete="current-password" autofocus required>
        {error}
        <button type="submit">Login</button>
    </form>
</body>
</html>
"""


CONFIG_ERROR_PAGE = """
<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>CourierBridge Locked</title></head>
<body style="font-family: Arial, sans-serif; padding: 32px;">
    <h1>CourierBridge is locked</h1>
    <p>Production auth is required, but the password or secret key is not configured.</p>
</body>
</html>
"""


def auth_required() -> bool:
    return REQUIRE_AUTH or bool(ACCESS_PASSWORD)


def auth_configured() -> bool:
    if not auth_required():
        return True
    return bool(ACCESS_PASSWORD and AUTH_SECRET_KEY)


def sign_auth_value(issued_at: int) -> str:
    payload = str(issued_at)
    signature = hmac.new(AUTH_SECRET_KEY.encode("utf-8"), payload.encode("utf-8"), "sha256").hexdigest()
    return f"{payload}.{signature}"


def valid_auth_cookie(cookie_value: str | None) -> bool:
    if not cookie_value or not auth_configured():
        return False
    issued_at_text, separator, signature = cookie_value.partition(".")
    if not separator or not issued_at_text.isdigit():
        return False
    issued_at = int(issued_at_text)
    if issued_at < int(time.time()) - AUTH_COOKIE_MAX_AGE:
        return False
    expected = sign_auth_value(issued_at).partition(".")[2]
    return hmac.compare_digest(signature, expected)


def password_is_valid(password: str) -> bool:
    return bool(ACCESS_PASSWORD) and hmac.compare_digest(password, ACCESS_PASSWORD)


def cookie_secure(request: Request) -> bool:
    forwarded_proto = request.headers.get("x-forwarded-proto", "").lower()
    return forwarded_proto == "https" or request.url.scheme == "https"


# Import routers later
# from app.routes import dashboard, shipments, tracking, customers, tools

# Create database tables
from app import models
Base.metadata.create_all(bind=engine)


def ensure_lightweight_migrations():
    try:
        with engine.begin() as connection:
            if engine.dialect.name == "sqlite":
                connection.execute(text("PRAGMA journal_mode=OFF"))
            inspector = inspect(connection)
            table_names = inspector.get_table_names()
            if "tracking_events" in table_names:
                columns = {column["name"] for column in inspector.get_columns("tracking_events")}
                if "notes" not in columns:
                    connection.execute(text("ALTER TABLE tracking_events ADD COLUMN notes VARCHAR"))
            if "shipments" in table_names:
                columns = {column["name"] for column in inspector.get_columns("shipments")}
                if "row_color" not in columns:
                    connection.execute(text("ALTER TABLE shipments ADD COLUMN row_color VARCHAR"))
    except Exception:
        # Keep startup available even if a non-SQLite database handles migrations externally.
        pass


ensure_lightweight_migrations()

app = FastAPI(title="CourierBridge")

# Mount static files
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Templates setup
templates = Jinja2Templates(directory="app/templates")


@app.middleware("http")
async def require_password_auth(request: Request, call_next):
    path = request.url.path
    if path == "/health" or path.startswith("/static") or path == "/login":
        return await call_next(request)
    if not auth_required():
        return await call_next(request)
    if not auth_configured():
        return HTMLResponse(CONFIG_ERROR_PAGE, status_code=503)
    if valid_auth_cookie(request.cookies.get(AUTH_COOKIE_NAME)):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response
    return RedirectResponse(url="/login", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, error: str = ""):
    if auth_required() and not auth_configured():
        return HTMLResponse(CONFIG_ERROR_PAGE, status_code=503)
    if auth_required() and valid_auth_cookie(request.cookies.get(AUTH_COOKIE_NAME)):
        return RedirectResponse(url="/", status_code=303)
    error_markup = '<p class="error">Wrong password</p>' if error else ""
    return HTMLResponse(LOGIN_PAGE.replace("{error}", error_markup))


@app.post("/login")
def login(request: Request, password: str = Form("")):
    if auth_required() and not auth_configured():
        return HTMLResponse(CONFIG_ERROR_PAGE, status_code=503)
    if not auth_required() or password_is_valid(password):
        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie(
            AUTH_COOKIE_NAME,
            sign_auth_value(int(time.time())) if auth_required() else "dev",
            max_age=AUTH_COOKIE_MAX_AGE,
            httponly=True,
            secure=cookie_secure(request),
            samesite="lax",
        )
        return response
    return RedirectResponse(url="/login?error=1", status_code=303)


@app.post("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(AUTH_COOKIE_NAME)
    return response


# Include routers
from app.routes import dashboard, shipments, tracking, customers, tools
app.include_router(dashboard.router)
app.include_router(shipments.router)
app.include_router(tracking.router)
app.include_router(customers.router)
app.include_router(tools.router)


@app.get("/health")
def health_check():
    return {"status": "ok"}
