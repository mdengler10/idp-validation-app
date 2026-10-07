"""IDP Validation App - FastAPI entrypoint."""
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.db import get_db, init_db
from app.exception_handlers import (
    http_exception_handler,
    unhandled_exception_handler,
    validation_exception_handler,
)
from app.logging_setup import LOG_FILE, configure_logging
from app.middleware import RequestContextMiddleware
from app.routes import api_router, web_router
from app.service import resync_all_expected_from_ground_truth

configure_logging()

app = FastAPI(title="IDP Validation App")
app.add_middleware(RequestContextMiddleware)
app.add_exception_handler(Exception, unhandled_exception_handler)
app.add_exception_handler(StarletteHTTPException, http_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)

# Ensure data dir and DB exist
init_db()

# Fix stale expected_documents keys (fields.Master_Prompt.* -> flat keys)
try:
    from app.db import SessionLocal
    with SessionLocal() as db:
        resync_all_expected_from_ground_truth(db)
except Exception:
    pass

app.include_router(api_router, prefix="/api", tags=["api"])
app.include_router(web_router, tags=["web"])

static_dir = Path(__file__).resolve().parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

templates_dir = Path(__file__).resolve().parent / "templates"
if templates_dir.exists():
    templates = Jinja2Templates(directory=str(templates_dir))
else:
    templates = None


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    if templates is None:
        return HTMLResponse("<h1>IDP Validation App</h1><p>API only. Mount templates for UI.</p>")
    return templates.TemplateResponse("index.html", {"request": request})
