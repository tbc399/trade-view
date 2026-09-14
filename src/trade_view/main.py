from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, select_autoescape

from trade_view.config import get_settings
from trade_view.tradier import TradierAPIError, TradierClient, TradierCredentialsMissing


BASE_DIR = Path(__file__).resolve().parent
template_env = Environment(
    loader=FileSystemLoader(str(BASE_DIR / "templates")),
    autoescape=select_autoescape(["html", "xml"]),
    cache_size=0,
)
templates = Jinja2Templates(env=template_env)

app = FastAPI(title="Trade View")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@dataclass
class AppPreferences:
    position_size_default_percent: Decimal


def initial_preferences() -> AppPreferences:
    try:
        position_size_default_percent = get_settings().position_size_default_percent
    except Exception:
        position_size_default_percent = Decimal("5")
    return AppPreferences(position_size_default_percent=position_size_default_percent)


app_preferences = initial_preferences()


NAV_ITEMS = [
    {
        "key": "overview",
        "label": "Overview",
        "path": "/",
        "partial_path": "/partials/overview",
        "icon": "M3 13h8V3H3v10Zm10 8h8V3h-8v18ZM3 21h8v-6H3v6Z",
    },
    {
        "key": "watchlist",
        "label": "Watchlist",
        "path": "/watchlist",
        "partial_path": "/partials/watchlist",
        "icon": "M3 12h3l2.25-6 4.5 12L15 12h6",
    },
    {
        "key": "positions",
        "label": "Positions",
        "path": "/positions",
        "partial_path": "/partials/positions",
        "icon": "M4 19V5m5 14V9m5 10V4m5 15v-7M3 19h18",
    },
    {
        "key": "settings",
        "label": "Settings",
        "path": "/settings",
        "partial_path": "/partials/settings",
        "icon": "M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Zm7.4-3.5a7.8 7.8 0 0 0-.1-1.1l2-1.5-2-3.5-2.4 1a7.8 7.8 0 0 0-1.9-1.1L14.7 3h-5.4L9 5.8a7.8 7.8 0 0 0-1.9 1.1l-2.4-1-2 3.5 2 1.5a7.8 7.8 0 0 0 0 2.2l-2 1.5 2 3.5 2.4-1a7.8 7.8 0 0 0 1.9 1.1l.3 2.8h5.4l.3-2.8a7.8 7.8 0 0 0 1.9-1.1l2.4 1 2-3.5-2-1.5c.1-.4.1-.7.1-1.1Z",
    },
]

SECTIONS = {
    "overview": {
        "title": "Overview",
        "template": "partials/overview.html",
        "path": "/",
    },
    "watchlist": {
        "title": "Watchlist",
        "template": "partials/watchlist.html",
        "path": "/watchlist",
    },
    "positions": {
        "title": "Positions",
        "template": "partials/positions.html",
        "path": "/positions",
    },
    "settings": {
        "title": "Settings",
        "template": "partials/settings.html",
        "path": "/settings",
    },
}

SUMMARY_STATS = [
    {"label": "Open Positions", "value": "5", "detail": "tracked holdings"},
    {"label": "Portfolio Drift", "value": "1.8%", "detail": "inside band"},
    {"label": "Risk Budget", "value": "68%", "detail": "moderate"},
]

WATCHLIST = [
    {"symbol": "SPY", "name": "S&P 500 ETF", "price": "$686.42", "change": "+0.42%"},
    {"symbol": "QQQ", "name": "Nasdaq 100 ETF", "price": "$611.08", "change": "+0.31%"},
    {"symbol": "IWM", "name": "Russell 2000 ETF", "price": "$244.19", "change": "-0.18%"},
    {"symbol": "TLT", "name": "20+ Year Treasury", "price": "$92.75", "change": "+0.09%"},
]

def page_context(
    request: Request,
    section_key: str,
    positions: list | None = None,
    positions_error: str = "",
    watchlists: list | None = None,
    watchlists_error: str = "",
    settings_message: str = "",
    settings_error: str = "",
) -> dict:
    section = SECTIONS[section_key]
    return {
        "request": request,
        "title": f"{section['title']} | Trade View",
        "nav_items": NAV_ITEMS,
        "active_nav": section_key,
        "section": section,
        "partial_template": section["template"],
        "summary_stats": SUMMARY_STATS,
        "watchlist": WATCHLIST,
        "watchlists": watchlists or [],
        "watchlists_error": watchlists_error,
        "positions": positions or [],
        "positions_error": positions_error,
        "preferences": app_preferences,
        "settings_message": settings_message,
        "settings_error": settings_error,
    }


async def positions_context(request: Request) -> dict:
    settings = get_settings()
    positions = []
    positions_error = ""

    try:
        positions = await TradierClient(settings).get_positions()
    except TradierCredentialsMissing:
        positions_error = "Add TRADIER_ACCOUNT_ID and TRADIER_API_TOKEN to your .env file to load live positions."
    except TradierAPIError as exc:
        positions_error = str(exc)
    except Exception:
        positions_error = "Unable to load Tradier positions right now."

    return page_context(
        request,
        "positions",
        positions=positions,
        positions_error=positions_error,
    )


async def watchlist_context(request: Request) -> dict:
    settings = get_settings()
    watchlists = []
    watchlists_error = ""

    try:
        watchlists = await TradierClient(settings).get_watchlists()
    except TradierCredentialsMissing:
        watchlists_error = "Add TRADIER_API_TOKEN to your .env file to load live watchlists."
    except TradierAPIError as exc:
        watchlists_error = str(exc)
    except Exception:
        watchlists_error = "Unable to load Tradier watchlists right now."

    return page_context(
        request,
        "watchlist",
        watchlists=watchlists,
        watchlists_error=watchlists_error,
    )


def settings_context(
    request: Request,
    settings_message: str = "",
    settings_error: str = "",
) -> dict:
    return page_context(
        request,
        "settings",
        settings_message=settings_message,
        settings_error=settings_error,
    )


def parse_position_size_percent(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Enter a valid percentage.") from exc

    if parsed <= 0:
        raise ValueError("Position size must be greater than 0%.")
    if parsed > 100:
        raise ValueError("Position size cannot be greater than 100%.")

    return parsed.quantize(Decimal("0.01"))


def get_section_key(section_key: str) -> str:
    if section_key not in SECTIONS:
        raise HTTPException(status_code=404, detail="Section not found")
    return section_key


@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse(request, "index.html", page_context(request, "overview"))


@app.get("/watchlist", response_class=HTMLResponse)
async def read_watchlist(request: Request):
    return templates.TemplateResponse(request, "index.html", await watchlist_context(request))


@app.get("/positions", response_class=HTMLResponse)
async def read_positions(request: Request):
    return templates.TemplateResponse(request, "index.html", await positions_context(request))


@app.get("/strategies", include_in_schema=False)
async def redirect_strategies():
    return RedirectResponse(url="/positions", status_code=308)


@app.get("/settings", response_class=HTMLResponse)
async def read_settings(request: Request):
    return templates.TemplateResponse(request, "index.html", settings_context(request))


@app.post("/settings/defaults", response_class=HTMLResponse)
async def update_defaults(request: Request):
    form = await request.form()
    try:
        app_preferences.position_size_default_percent = parse_position_size_percent(
            str(form.get("position_size_default_percent") or "")
        )
    except ValueError as exc:
        context = settings_context(request, settings_error=str(exc))
    else:
        context = settings_context(request, settings_message="Defaults updated.")

    return templates.TemplateResponse(request, context["partial_template"], context)


@app.get("/alerts", include_in_schema=False)
async def redirect_alerts():
    return RedirectResponse(url="/settings", status_code=308)


@app.get("/partials/{section_key}", response_class=HTMLResponse)
async def read_section_partial(request: Request, section_key: str):
    section_key = get_section_key(section_key)
    if section_key == "positions":
        context = await positions_context(request)
        return templates.TemplateResponse(request, context["partial_template"], context)
    if section_key == "watchlist":
        context = await watchlist_context(request)
        return templates.TemplateResponse(request, context["partial_template"], context)
    if section_key == "settings":
        context = settings_context(request)
        return templates.TemplateResponse(request, context["partial_template"], context)

    context = page_context(request, section_key)
    return templates.TemplateResponse(request, context["partial_template"], context)
