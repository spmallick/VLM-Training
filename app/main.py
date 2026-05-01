from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .agent import ExpenseAutomationAgent
from .agent_routes import create_agent_router
from .config import get_settings
from .policy import PolicyReviewService
from .portal_routes import create_portal_router
from .store import SessionStore
from .tools import BlurDetector
from .vision import ReceiptVisionService


APP_DIR = Path(__file__).resolve().parent

settings = get_settings()
store = SessionStore(settings.database_path)
vision_service = ReceiptVisionService(settings)
policy_service = PolicyReviewService(settings)
agent = ExpenseAutomationAgent(settings, store, policy_service)
blur_detector = BlurDetector()

# The agent UI and the sandbox portal UI live in separate template trees. The
# agent opens the portal through the browser, so portal templates are target
# website code, not agent-side knowledge.
agent_templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
portal_templates = Jinja2Templates(directory=str(APP_DIR / "portal_site" / "templates"))

app = FastAPI(title=settings.app_name)
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")


def asset_version() -> str:
    """Return a cache-busting version for static assets used by templates."""
    paths = [APP_DIR / "static" / "styles.css", APP_DIR / "static" / "app.js"]
    latest = max(int(path.stat().st_mtime) for path in paths if path.exists())
    return str(latest)


@app.on_event("startup")
async def startup() -> None:
    store.init_db()
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)


app.include_router(
    create_agent_router(
        settings=settings,
        store=store,
        vision_service=vision_service,
        agent=agent,
        blur_detector=blur_detector,
        agent_templates=agent_templates,
        asset_version=asset_version,
    )
)
app.include_router(create_portal_router(portal_templates, asset_version))
