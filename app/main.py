from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from app.database import engine, Base
from sqlalchemy import inspect, text
import os

# Import routers later
# from app.routes import dashboard, shipments, tracking, customers, tools

# Create database tables
from app import models
Base.metadata.create_all(bind=engine)

def ensure_lightweight_migrations():
    try:
        inspector = inspect(engine)
        if "tracking_events" in inspector.get_table_names():
            columns = {column["name"] for column in inspector.get_columns("tracking_events")}
            if "notes" not in columns:
                with engine.begin() as connection:
                    connection.execute(text("ALTER TABLE tracking_events ADD COLUMN notes VARCHAR"))
    except Exception:
        # Keep startup available even if a non-SQLite database handles migrations externally.
        pass

ensure_lightweight_migrations()

app = FastAPI(title="CourierBridge")

# Mount static files
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Templates setup
templates = Jinja2Templates(directory="app/templates")

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
