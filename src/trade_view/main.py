import asyncio
from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, select_autoescape

from trade_view.auth import (
    AUTH_COOKIE_NAME,
    SESSION_TTL_SECONDS,
    create_session_token,
    verify_password,
    verify_session_token,
)
from trade_view.config import get_settings
from trade_view.tradier import (
    DisplayAccountBalanceMetric,
    DisplayBracketOrder,
    DisplayHistoricalBalancePoint,
    DisplayHistoricalCandlePoint,
    DisplayHistoricalPricePoint,
    DisplayPendingGuardOrder,
    DisplayPosition,
    PlacedEquityOrder,
    PreviewedEquityOrder,
    TradierAPIError,
    TradierClient,
    TradierCredentialsMissing,
    TradierOrderSizingError,
    current_market_date,
    format_percent,
    format_signed_money,
    to_date,
)


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

AUTH_EXEMPT_PATHS = {"/health", "/login", "/favicon.ico"}


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
    {"label": "Open Positions", "value": "--", "detail": "Tradier positions"},
    {"label": "Watchlist Symbols", "value": "--", "detail": "Tradier watchlists"},
    {"label": "Position Default", "value": "5%", "detail": "per new entry"},
]

WATCHLIST = [
    {"symbol": "SPY", "name": "S&P 500 ETF", "price": "$686.42", "change": "+0.42%"},
    {"symbol": "QQQ", "name": "Nasdaq 100 ETF", "price": "$611.08", "change": "+0.31%"},
    {"symbol": "IWM", "name": "Russell 2000 ETF", "price": "$244.19", "change": "-0.18%"},
    {"symbol": "TLT", "name": "20+ Year Treasury", "price": "$92.75", "change": "+0.09%"},
]

HISTORICAL_BALANCE_PERIOD_OPTIONS = [
    {"value": "WEEK", "label": "1W"},
    {"value": "MONTH", "label": "1M"},
    {"value": "YTD", "label": "YTD"},
    {"value": "YEAR", "label": "1Y"},
    {"value": "YEAR_3", "label": "3Y"},
    {"value": "YEAR_5", "label": "5Y"},
    {"value": "ALL", "label": "All"},
]
DEFAULT_HISTORICAL_BALANCE_PERIOD = "MONTH"


def normalize_historical_balance_period(value: str) -> str:
    period = str(value).strip().upper()
    allowed_periods = {option["value"] for option in HISTORICAL_BALANCE_PERIOD_OPTIONS}
    if period in allowed_periods:
        return period
    return DEFAULT_HISTORICAL_BALANCE_PERIOD


def historical_balance_period_label(period: str) -> str:
    return next(
        (
            option["label"]
            for option in HISTORICAL_BALANCE_PERIOD_OPTIONS
            if option["value"] == period
        ),
        period,
    )


def account_return_summary(
    points: list[DisplayHistoricalBalancePoint],
    period: str,
) -> dict:
    label = historical_balance_period_label(period)
    if len(points) < 2:
        return {
            "label": label,
            "value": "--",
            "detail": "Need at least two balance points",
            "change": "--",
            "change_value": 0,
        }

    start_value = points[0].value
    end_value = points[-1].value
    change = end_value - start_value
    percent = (change / abs(start_value) * Decimal("100")) if start_value else None

    return {
        "label": label,
        "value": format_percent(percent),
        "detail": f"{format_signed_money(change)} over {label}",
        "change": format_signed_money(change),
        "change_value": float(change),
        "start_value": points[0].value_display,
        "end_value": points[-1].value_display,
    }


def account_balance_chart_data(
    points: list[DisplayHistoricalBalancePoint],
) -> list[dict[str, float | str]]:
    return [{"date": point.date, "value": float(point.value)} for point in points]


def candle_chart_data(
    points: list[DisplayHistoricalCandlePoint],
) -> list[dict[str, float | int | str | None]]:
    return [
        {
            "date": point.date,
            "open": float(point.open),
            "high": float(point.high),
            "low": float(point.low),
            "close": float(point.close),
            "volume": point.volume,
        }
        for point in points
    ]


def candle_chart_meta(
    position: DisplayPosition | None,
    points: list[DisplayHistoricalCandlePoint],
    pending_guard_orders: list[DisplayPendingGuardOrder] | None = None,
) -> dict:
    meta = {
        "pending_guard_orders": [
            {
                "label": order.label,
                "kind": order.kind,
                "price": order.price_value,
                "price_display": order.price,
                "quantity": order.quantity,
                "status": order.status,
                "order_id": order.order_id,
                "duration": order.duration,
            }
            for order in pending_guard_orders or []
        ],
    }

    if position is None or position.entry_price_value is None:
        return meta

    acquired_date = to_date(position.acquired_date_value)
    marker_date = None
    if acquired_date is not None:
        candle_dates = [
            (candle_date, point.date)
            for point in points
            if (candle_date := to_date(point.date)) is not None
        ]
        marker_date = next(
            (
                candle_date_text
                for candle_date, candle_date_text in sorted(candle_dates, key=lambda item: item[0])
                if candle_date >= acquired_date
            ),
            None,
        )

    meta.update(
        {
            "entry_price": position.entry_price_value,
            "entry_price_display": position.entry_price,
            "entry_date": position.acquired_date_value,
            "entry_marker_date": marker_date,
        }
    )
    return meta


def subtract_months(value: date, months: int) -> date:
    month_index = value.month - 1 - months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, monthrange(year, month)[1])
    return date(year, month, day)


def performance_chart_data(
    balance_points: list[DisplayHistoricalBalancePoint],
    benchmark_points: list[DisplayHistoricalPricePoint],
) -> list[dict[str, float | str]]:
    dated_prices = [
        (price_date, point.close)
        for point in benchmark_points
        if (price_date := to_date(point.date)) is not None and point.close > 0
    ]
    dated_prices.sort(key=lambda item: item[0])
    if not dated_prices:
        return []

    dated_balances = [
        (balance_date, point)
        for point in balance_points
        if (balance_date := to_date(point.date)) is not None and point.value > 0
    ]
    dated_balances.sort(key=lambda item: item[0])
    if not dated_balances:
        return []

    rows = []
    price_index = 0
    latest_benchmark_price = None
    base_account_value = None
    base_benchmark_price = None

    for balance_date, balance_point in dated_balances:
        while price_index < len(dated_prices) and dated_prices[price_index][0] <= balance_date:
            latest_benchmark_price = dated_prices[price_index][1]
            price_index += 1

        if latest_benchmark_price is None:
            continue

        if base_account_value is None or base_benchmark_price is None:
            base_account_value = balance_point.value
            base_benchmark_price = latest_benchmark_price

        rows.append(
            {
                "date": balance_point.date,
                "account": float((balance_point.value / base_account_value - 1) * Decimal("100")),
                "benchmark": float(
                    (latest_benchmark_price / base_benchmark_price - 1) * Decimal("100")
                ),
                "account_value": float(balance_point.value),
                "benchmark_price": float(latest_benchmark_price),
            }
        )

    return rows


def watchlist_symbols(watchlists: list) -> list[str]:
    return sorted(
        {
            item.symbol.strip().upper()
            for watchlist in watchlists
            for item in watchlist.items
            if item.symbol.strip()
        }
    )


def finviz_watchlist_url(watchlists: list) -> str:
    symbols = watchlist_symbols(watchlists)
    if not symbols:
        return ""

    return f"https://finviz.com/screener.ashx?v=211&t={quote(','.join(symbols), safe=',')}"


def find_position(positions: list[DisplayPosition], symbol: str) -> DisplayPosition | None:
    normalized_symbol = symbol.upper()
    return next(
        (position for position in positions if position.symbol.upper() == normalized_symbol),
        None,
    )


def find_pending_guard_order(
    orders: list[DisplayPendingGuardOrder],
    kind: str,
) -> DisplayPendingGuardOrder | None:
    return next((order for order in orders if order.kind == kind), None)


def page_context(
    request: Request,
    section_key: str,
    positions: list | None = None,
    positions_error: str = "",
    historical_balance_points: list[DisplayHistoricalBalancePoint] | None = None,
    historical_balance_error: str = "",
    historical_balance_period: str = DEFAULT_HISTORICAL_BALANCE_PERIOD,
    benchmark_symbol: str = "SPY",
    benchmark_error: str = "",
    performance_points: list[dict[str, float | str]] | None = None,
    account_metrics: list[DisplayAccountBalanceMetric] | None = None,
    account_balances_error: str = "",
    watchlists: list | None = None,
    watchlists_error: str = "",
    watchlists_message: str = "",
    settings_message: str = "",
    settings_error: str = "",
    order_preview: PreviewedEquityOrder | None = None,
    placed_order: PlacedEquityOrder | None = None,
    position_entry_error: str = "",
) -> dict:
    section = SECTIONS[section_key]
    historical_balance_points = historical_balance_points or []
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
        "watchlists_finviz_url": finviz_watchlist_url(watchlists or []),
        "watchlists_symbol_count": len(watchlist_symbols(watchlists or [])),
        "watchlists_error": watchlists_error,
        "watchlists_message": watchlists_message,
        "positions": positions or [],
        "positions_error": positions_error,
        "historical_balance_points": historical_balance_points,
        "historical_balance_chart_data": account_balance_chart_data(historical_balance_points),
        "historical_balance_error": historical_balance_error,
        "historical_balance_period": historical_balance_period,
        "historical_balance_period_label": historical_balance_period_label(historical_balance_period),
        "historical_balance_period_options": HISTORICAL_BALANCE_PERIOD_OPTIONS,
        "benchmark_symbol": benchmark_symbol,
        "benchmark_error": benchmark_error,
        "performance_chart_data": performance_points or [],
        "account_metrics": account_metrics or [],
        "account_balances_error": account_balances_error,
        "account_return": account_return_summary(
            historical_balance_points,
            historical_balance_period,
        ),
        "preferences": app_preferences,
        "settings_message": settings_message,
        "settings_error": settings_error,
        "order_preview": order_preview,
        "placed_order": placed_order,
        "position_entry_error": position_entry_error,
    }


def is_authenticated(request: Request) -> bool:
    settings = get_settings()
    if not settings.has_login_credentials:
        return False

    token = request.cookies.get(AUTH_COOKIE_NAME)
    if not token:
        return False

    return verify_session_token(
        token,
        expected_username=settings.username,
        signing_secret=settings.password_hash.get_secret_value(),
    )


def next_path_from_request(request: Request) -> str:
    next_path = request.url.path
    if request.url.query:
        next_path = f"{next_path}?{request.url.query}"
    return next_path


def safe_next_path(value: str) -> str:
    next_path = value.strip()
    if not next_path or not next_path.startswith("/") or next_path.startswith("//"):
        return "/"
    return next_path


def redirect_to_login(request: Request) -> Response:
    login_url = f"/login?next={quote(next_path_from_request(request), safe='')}"
    if request.headers.get("hx-request") == "true":
        response = Response(status_code=401)
        response.headers["HX-Redirect"] = login_url
        return response
    return RedirectResponse(url=login_url, status_code=303)


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if path in AUTH_EXEMPT_PATHS or path.startswith("/static/"):
        return await call_next(request)

    if not is_authenticated(request):
        return redirect_to_login(request)

    return await call_next(request)


@app.get("/health", include_in_schema=False)
async def health():
    return {"status": "ok"}


@app.get("/login", response_class=HTMLResponse)
async def read_login(request: Request, next: str = "/"):
    if is_authenticated(request):
        return RedirectResponse(url=safe_next_path(next), status_code=303)

    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "error": "",
            "next_path": safe_next_path(next),
            "username": "",
        },
    )


@app.post("/login", response_class=HTMLResponse)
async def create_login(request: Request):
    form = await request.form()
    username = str(form.get("username") or "")
    password = str(form.get("password") or "")
    next_path = safe_next_path(str(form.get("next") or "/"))
    settings = get_settings()
    password_hash = settings.password_hash.get_secret_value()

    if not settings.has_login_credentials:
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "error": "Login is not configured. Set USERNAME and PASSWORD in the environment.",
                "next_path": next_path,
                "username": username,
            },
            status_code=503,
        )

    if username != settings.username or not verify_password(password, password_hash):
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "error": "Invalid username or password.",
                "next_path": next_path,
                "username": username,
            },
            status_code=401,
        )

    response = RedirectResponse(url=next_path, status_code=303)
    response.set_cookie(
        AUTH_COOKIE_NAME,
        create_session_token(settings.username, password_hash),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
    )
    return response


@app.post("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(AUTH_COOKIE_NAME, samesite="lax")
    return response


async def overview_context(
    request: Request,
    historical_balance_period: str = DEFAULT_HISTORICAL_BALANCE_PERIOD,
) -> dict:
    positions = []
    positions_error = ""
    historical_balance_points = []
    historical_balance_error = ""
    historical_balance_period = normalize_historical_balance_period(historical_balance_period)
    settings = get_settings()
    benchmark_symbol = (settings.benchmark_symbol or "SPY").strip().upper()
    benchmark_error = ""
    performance_points = []
    account_metrics = []
    account_balances_error = ""
    watchlists = []
    watchlists_error = ""

    async def load_positions():
        return await TradierClient(settings).get_positions()

    async def load_account_metrics():
        return await TradierClient(settings).get_account_balance_metrics()

    async def load_watchlists():
        return await TradierClient(settings).get_watchlists()

    async def load_historical_balances():
        return await TradierClient(settings).get_historical_balances(historical_balance_period)

    (
        positions_result,
        account_metrics_result,
        watchlists_result,
        historical_balance_result,
    ) = await asyncio.gather(
        load_positions(),
        load_account_metrics(),
        load_watchlists(),
        load_historical_balances(),
        return_exceptions=True,
    )

    if isinstance(positions_result, TradierCredentialsMissing):
        positions_error = "Add TRADIER_ACCOUNT_ID and TRADIER_API_TOKEN to load live positions."
    elif isinstance(positions_result, TradierAPIError):
        positions_error = str(positions_result)
    elif isinstance(positions_result, Exception):
        positions_error = "Unable to load Tradier positions right now."
    else:
        positions = positions_result

    if isinstance(account_metrics_result, TradierCredentialsMissing):
        account_balances_error = (
            "Add TRADIER_ACCOUNT_ID and TRADIER_API_TOKEN to load account balances."
        )
    elif isinstance(account_metrics_result, TradierAPIError):
        account_balances_error = str(account_metrics_result)
    elif isinstance(account_metrics_result, Exception):
        account_balances_error = "Unable to load Tradier account balances right now."
    else:
        account_metrics = account_metrics_result

    if isinstance(watchlists_result, TradierCredentialsMissing):
        watchlists_error = "Add TRADIER_API_TOKEN to load live watchlists."
    elif isinstance(watchlists_result, TradierAPIError):
        watchlists_error = str(watchlists_result)
    elif isinstance(watchlists_result, Exception):
        watchlists_error = "Unable to load Tradier watchlists right now."
    else:
        watchlists = watchlists_result

    if isinstance(historical_balance_result, TradierCredentialsMissing):
        historical_balance_error = "Add TRADIER_ACCOUNT_ID and TRADIER_API_TOKEN to load account history."
    elif isinstance(historical_balance_result, TradierAPIError):
        historical_balance_error = str(historical_balance_result)
    elif isinstance(historical_balance_result, Exception):
        historical_balance_error = "Unable to load Tradier historical balances right now."
    else:
        historical_balance_points = historical_balance_result

    if not historical_balance_error and len(historical_balance_points) >= 2:
        historical_dates = [
            historical_date
            for point in historical_balance_points
            if (historical_date := to_date(point.date)) is not None
        ]
        if historical_dates:
            try:
                benchmark_points = await TradierClient(settings).get_historical_prices(
                    benchmark_symbol,
                    min(historical_dates),
                    max(historical_dates),
                )
                performance_points = performance_chart_data(
                    historical_balance_points,
                    benchmark_points,
                )
                if not performance_points:
                    benchmark_error = f"No {benchmark_symbol} benchmark prices were available for this period."
            except TradierCredentialsMissing:
                benchmark_error = f"Add TRADIER_API_TOKEN to compare performance against {benchmark_symbol}."
            except TradierAPIError as exc:
                benchmark_error = str(exc)
            except Exception:
                benchmark_error = f"Unable to load {benchmark_symbol} benchmark history right now."

    watched_symbols_count = sum(len(watchlist.items) for watchlist in watchlists)
    open_pnl = sum(Decimal(str(position.pnl_value)) for position in positions)
    account_return = account_return_summary(historical_balance_points, historical_balance_period)
    summary_stats = [
        {
            "label": "Open Positions",
            "value": str(len(positions)) if not positions_error else "--",
            "detail": "Tradier positions",
        },
        {
            "label": "Watchlist Symbols",
            "value": str(watched_symbols_count) if not watchlists_error else "--",
            "detail": f"{len(watchlists)} watchlists",
        },
        {
            "label": "Open P&L",
            "value": f"{'+' if open_pnl >= 0 else '-'}${abs(open_pnl):,.2f}" if positions else "--",
            "detail": "live quote estimate",
        },
        {
            "label": "Account Return",
            "value": account_return["value"] if not historical_balance_error else "--",
            "detail": (
                account_return["detail"]
                if not historical_balance_error
                else historical_balance_period_label(historical_balance_period)
            ),
        },
        {
            "label": "Position Default",
            "value": f"{app_preferences.position_size_default_percent}%",
            "detail": "per new entry",
        },
    ]

    context = page_context(
        request,
        "overview",
        positions=positions[:5],
        positions_error=positions_error,
        historical_balance_points=historical_balance_points,
        historical_balance_error=historical_balance_error,
        historical_balance_period=historical_balance_period,
        benchmark_symbol=benchmark_symbol,
        benchmark_error=benchmark_error,
        performance_points=performance_points,
        account_metrics=account_metrics,
        account_balances_error=account_balances_error,
        watchlists=watchlists[:3],
        watchlists_error=watchlists_error,
    )
    context["summary_stats"] = summary_stats
    return context


async def positions_context(
    request: Request,
    order_preview: PreviewedEquityOrder | None = None,
    placed_order: PlacedEquityOrder | None = None,
    position_entry_error: str = "",
) -> dict:
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
        order_preview=order_preview,
        placed_order=placed_order,
        position_entry_error=position_entry_error,
    )


async def position_detail_context(request: Request, symbol: str) -> dict:
    settings = get_settings()
    symbol = parse_symbol(symbol)
    positions = []
    position = None
    positions_error = ""
    candle_points = []
    candle_error = ""
    pending_guard_orders = []
    pending_guard_orders_error = ""
    end_date = current_market_date()
    start_date = subtract_months(end_date, 6)

    try:
        positions = await TradierClient(settings).get_positions()
        position = find_position(positions, symbol)
        if position is None:
            positions_error = f"{symbol} is not an open position."
    except TradierCredentialsMissing:
        positions_error = "Add TRADIER_ACCOUNT_ID and TRADIER_API_TOKEN to your .env file to load live positions."
    except TradierAPIError as exc:
        positions_error = str(exc)
    except Exception:
        positions_error = "Unable to load Tradier positions right now."

    try:
        candle_points = await TradierClient(settings).get_historical_candles(
            symbol,
            start_date,
            end_date,
        )
        if not candle_points:
            candle_error = f"No daily candles were available for {symbol}."
    except TradierCredentialsMissing:
        candle_error = "Add TRADIER_API_TOKEN to load daily candles."
    except TradierAPIError as exc:
        candle_error = str(exc)
    except Exception:
        candle_error = f"Unable to load {symbol} candle history right now."

    try:
        pending_guard_orders = await TradierClient(settings).get_pending_guard_orders(symbol)
    except TradierCredentialsMissing:
        pending_guard_orders_error = "Add TRADIER_ACCOUNT_ID and TRADIER_API_TOKEN to load pending guard orders."
    except TradierAPIError as exc:
        pending_guard_orders_error = str(exc)
    except Exception:
        pending_guard_orders_error = f"Unable to load pending {symbol} guard orders right now."

    context = page_context(
        request,
        "positions",
        positions=positions,
        positions_error=positions_error,
    )
    context.update(
        {
            "title": f"{symbol} | Positions | Trade View",
            "partial_template": "partials/position-detail.html",
            "position": position,
            "position_symbol": symbol,
            "candle_chart_data": candle_chart_data(candle_points),
            "candle_chart_meta": candle_chart_meta(position, candle_points, pending_guard_orders),
            "candle_error": candle_error,
            "candle_start_date": start_date.isoformat(),
            "candle_end_date": end_date.isoformat(),
            "pending_guard_orders": pending_guard_orders,
            "pending_guard_orders_error": pending_guard_orders_error,
            "pending_target_order": find_pending_guard_order(pending_guard_orders, "target"),
            "pending_stop_order": find_pending_guard_order(pending_guard_orders, "stop"),
        }
    )
    return context


async def watchlist_context(
    request: Request,
    watchlists_message: str = "",
    watchlists_error: str = "",
) -> dict:
    settings = get_settings()
    watchlists = []
    initial_error = watchlists_error

    try:
        watchlists = await TradierClient(settings).get_watchlists()
    except TradierCredentialsMissing:
        watchlists_error = initial_error or "Add TRADIER_API_TOKEN to your .env file to load live watchlists."
    except TradierAPIError as exc:
        watchlists_error = initial_error or str(exc)
    except Exception:
        watchlists_error = initial_error or "Unable to load Tradier watchlists right now."

    return page_context(
        request,
        "watchlist",
        watchlists=watchlists,
        watchlists_error=watchlists_error,
        watchlists_message=watchlists_message,
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


def parse_position_quantity(value: str) -> Decimal:
    quantity = to_decimal_or_error(value, "Enter a valid share quantity.")
    if quantity == 0:
        raise ValueError("Cannot exit a position with zero shares.")
    return quantity


def parse_order_quantity(value: str) -> Decimal:
    quantity = to_decimal_or_error(value, "Enter a valid share quantity.")
    if quantity <= 0:
        raise ValueError("Order quantity must be greater than zero.")
    return quantity


def parse_order_price(value: str, label: str) -> Decimal:
    normalized_value = value.strip().replace("$", "").replace(",", "")
    price = to_decimal_or_error(normalized_value, f"Enter a valid {label.lower()} price.")
    if price <= 0:
        raise ValueError(f"{label} must be greater than zero.")
    return price.quantize(Decimal("0.01"))


def parse_order_duration(value: str) -> str:
    duration = value.strip().lower()
    if duration not in {"day", "gtc"}:
        raise ValueError("Choose day or GTC duration.")
    return duration


def parse_order_id(value: str, label: str) -> str:
    order_id = value.strip()
    if not order_id.isdigit():
        raise ValueError(f"Missing {label.lower()} order ID.")
    return order_id


def to_decimal_or_error(value: str, message: str) -> Decimal:
    try:
        return Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(message) from exc


def parse_symbol(value: str) -> str:
    symbol = value.strip().upper()
    if not symbol:
        raise ValueError("Enter a symbol.")
    if len(symbol) > 12:
        raise ValueError("Symbol must be 12 characters or fewer.")
    if not all(character.isalnum() or character in {".", "-", "/"} for character in symbol):
        raise ValueError("Symbol can only include letters, numbers, dots, dashes, or slashes.")
    return symbol


def parse_watchlist_id(value: str) -> str:
    watchlist_id = value.strip()
    if not watchlist_id:
        raise ValueError("Choose a watchlist.")
    return watchlist_id


def get_section_key(section_key: str) -> str:
    if section_key not in SECTIONS:
        raise HTTPException(status_code=404, detail="Section not found")
    return section_key


@app.get("/", response_class=HTMLResponse)
async def read_root(
    request: Request,
    period: str = DEFAULT_HISTORICAL_BALANCE_PERIOD,
):
    return templates.TemplateResponse(request, "index.html", await overview_context(request, period))


@app.get("/watchlist", response_class=HTMLResponse)
async def read_watchlist(request: Request):
    return templates.TemplateResponse(request, "index.html", await watchlist_context(request))


@app.post("/watchlist/symbols", response_class=HTMLResponse)
async def add_watchlist_symbol(request: Request):
    form = await request.form()
    watchlists_message = ""
    watchlists_error = ""

    try:
        watchlist_id = parse_watchlist_id(str(form.get("watchlist_id") or ""))
        symbol = parse_symbol(str(form.get("symbol") or ""))
        await TradierClient(get_settings()).add_symbols_to_watchlist(watchlist_id, [symbol])
    except ValueError as exc:
        watchlists_error = str(exc)
    except TradierCredentialsMissing:
        watchlists_error = "Add TRADIER_API_TOKEN to your .env file before updating watchlists."
    except TradierAPIError as exc:
        watchlists_error = str(exc)
    except Exception:
        watchlists_error = "Unable to update the Tradier watchlist right now."
    else:
        watchlists_message = f"Added {symbol} to the watchlist."

    context = await watchlist_context(
        request,
        watchlists_message=watchlists_message,
        watchlists_error=watchlists_error,
    )
    return templates.TemplateResponse(request, context["partial_template"], context)


@app.post("/watchlist/symbols/remove", response_class=HTMLResponse)
async def remove_watchlist_symbol(request: Request):
    form = await request.form()
    watchlists_message = ""
    watchlists_error = ""

    try:
        watchlist_id = parse_watchlist_id(str(form.get("watchlist_id") or ""))
        symbol = parse_symbol(str(form.get("symbol") or ""))
        await TradierClient(get_settings()).remove_symbol_from_watchlist(watchlist_id, symbol)
    except ValueError as exc:
        watchlists_error = str(exc)
    except TradierCredentialsMissing:
        watchlists_error = "Add TRADIER_API_TOKEN to your .env file before updating watchlists."
    except TradierAPIError as exc:
        watchlists_error = str(exc)
    except Exception:
        watchlists_error = "Unable to update the Tradier watchlist right now."
    else:
        watchlists_message = f"Removed {symbol} from the watchlist."

    context = await watchlist_context(
        request,
        watchlists_message=watchlists_message,
        watchlists_error=watchlists_error,
    )
    return templates.TemplateResponse(request, context["partial_template"], context)


@app.get("/positions", response_class=HTMLResponse)
async def read_positions(request: Request):
    return templates.TemplateResponse(request, "index.html", await positions_context(request))


@app.get("/positions/{symbol}", response_class=HTMLResponse)
async def read_position_detail(request: Request, symbol: str):
    try:
        context = await position_detail_context(request, symbol)
    except ValueError:
        raise HTTPException(status_code=404, detail="Position not found") from None
    return templates.TemplateResponse(request, "index.html", context)


@app.post("/positions/{symbol}/bracket/preview", response_class=HTMLResponse)
async def preview_position_bracket(request: Request, symbol: str):
    form = await request.form()
    bracket_preview: DisplayBracketOrder | None = None
    bracket_order_error = ""

    try:
        symbol = parse_symbol(symbol)
        quantity = parse_position_quantity(str(form.get("quantity") or ""))
        take_profit_price = parse_order_price(
            str(form.get("take_profit_price") or ""),
            "Take profit",
        )
        stop_loss_price = parse_order_price(str(form.get("stop_loss_price") or ""), "Stop loss")
        duration = parse_order_duration(str(form.get("duration") or "gtc"))
        bracket_preview = await TradierClient(get_settings()).preview_position_bracket_order(
            symbol,
            quantity,
            take_profit_price,
            stop_loss_price,
            duration,
        )
    except ValueError as exc:
        bracket_order_error = str(exc)
    except TradierCredentialsMissing:
        bracket_order_error = "Add TRADIER_ACCOUNT_ID and TRADIER_API_TOKEN to your .env file before placing orders."
    except TradierOrderSizingError as exc:
        bracket_order_error = str(exc)
    except TradierAPIError as exc:
        bracket_order_error = str(exc)
    except Exception:
        bracket_order_error = "Unable to preview the Tradier bracket order right now."

    return templates.TemplateResponse(
        request,
        "partials/position-bracket-result.html",
        {
            "bracket_order_error": bracket_order_error,
            "bracket_preview": bracket_preview,
            "bracket_order": None,
            "bracket_update": None,
        },
    )


@app.post("/positions/{symbol}/bracket/submit", response_class=HTMLResponse)
async def submit_position_bracket(request: Request, symbol: str):
    form = await request.form()
    bracket_order: DisplayBracketOrder | None = None
    bracket_order_error = ""

    try:
        symbol = parse_symbol(symbol)
        quantity = parse_position_quantity(str(form.get("quantity") or ""))
        take_profit_price = parse_order_price(
            str(form.get("take_profit_price") or ""),
            "Take profit",
        )
        stop_loss_price = parse_order_price(str(form.get("stop_loss_price") or ""), "Stop loss")
        duration = parse_order_duration(str(form.get("duration") or "gtc"))
        bracket_order = await TradierClient(get_settings()).submit_position_bracket_order(
            symbol,
            quantity,
            take_profit_price,
            stop_loss_price,
            duration,
        )
    except ValueError as exc:
        bracket_order_error = str(exc)
    except TradierCredentialsMissing:
        bracket_order_error = "Add TRADIER_ACCOUNT_ID and TRADIER_API_TOKEN to your .env file before placing orders."
    except TradierOrderSizingError as exc:
        bracket_order_error = str(exc)
    except TradierAPIError as exc:
        bracket_order_error = str(exc)
    except Exception:
        bracket_order_error = "Unable to submit the Tradier bracket order right now."

    return templates.TemplateResponse(
        request,
        "partials/position-bracket-result.html",
        {
            "bracket_order_error": bracket_order_error,
            "bracket_preview": None,
            "bracket_order": bracket_order,
            "bracket_update": None,
        },
    )


@app.post("/positions/{symbol}/bracket/update", response_class=HTMLResponse)
async def update_position_bracket(request: Request, symbol: str):
    form = await request.form()
    bracket_update: DisplayBracketOrder | None = None
    bracket_order_error = ""

    try:
        symbol = parse_symbol(symbol)
        quantity = parse_position_quantity(str(form.get("quantity") or ""))
        target_order_id = parse_order_id(str(form.get("target_order_id") or ""), "Target")
        stop_order_id = parse_order_id(str(form.get("stop_order_id") or ""), "Stop")
        take_profit_price = parse_order_price(
            str(form.get("take_profit_price") or ""),
            "Take profit",
        )
        stop_loss_price = parse_order_price(str(form.get("stop_loss_price") or ""), "Stop loss")
        duration = parse_order_duration(str(form.get("duration") or "gtc"))
        bracket_update = await TradierClient(get_settings()).update_position_guard_orders(
            symbol,
            quantity,
            target_order_id,
            stop_order_id,
            take_profit_price,
            stop_loss_price,
            duration,
        )
    except ValueError as exc:
        bracket_order_error = str(exc)
    except TradierCredentialsMissing:
        bracket_order_error = "Add TRADIER_ACCOUNT_ID and TRADIER_API_TOKEN to your .env file before updating orders."
    except TradierOrderSizingError as exc:
        bracket_order_error = str(exc)
    except TradierAPIError as exc:
        bracket_order_error = str(exc)
    except Exception:
        bracket_order_error = "Unable to update the Tradier bracket order right now."

    return templates.TemplateResponse(
        request,
        "partials/position-bracket-result.html",
        {
            "bracket_order_error": bracket_order_error,
            "bracket_preview": None,
            "bracket_order": None,
            "bracket_update": bracket_update,
        },
    )


@app.get("/positions/{symbol}/bracket/cancel", response_class=HTMLResponse)
async def cancel_position_bracket(request: Request, symbol: str):
    return templates.TemplateResponse(
        request,
        "partials/position-bracket-result.html",
        {
            "bracket_order_error": "",
            "bracket_preview": None,
            "bracket_order": None,
            "bracket_update": None,
        },
    )


@app.post("/positions/entry", response_class=HTMLResponse)
async def create_position_entry(request: Request):
    form = await request.form()
    order_preview = None
    position_entry_error = ""

    try:
        symbol = parse_symbol(str(form.get("symbol") or ""))
        position_size_percent = parse_position_size_percent(
            str(form.get("position_size_percent") or "")
        )
        order_preview = await TradierClient(get_settings()).preview_long_equity_position(
            symbol,
            position_size_percent,
        )
    except ValueError as exc:
        position_entry_error = str(exc)
    except TradierCredentialsMissing:
        position_entry_error = "Add TRADIER_ACCOUNT_ID and TRADIER_API_TOKEN to your .env file before placing orders."
    except TradierOrderSizingError as exc:
        position_entry_error = str(exc)
    except TradierAPIError as exc:
        position_entry_error = str(exc)
    except Exception:
        position_entry_error = "Unable to place the Tradier order right now."

    context = page_context(
        request,
        "positions",
        order_preview=order_preview,
        position_entry_error=position_entry_error,
    )
    return templates.TemplateResponse(request, "partials/position-entry-result.html", context)


@app.post("/positions/entry/submit", response_class=HTMLResponse)
async def submit_position_entry(request: Request):
    form = await request.form()
    placed_order = None
    position_entry_error = ""

    try:
        symbol = parse_symbol(str(form.get("symbol") or ""))
        quantity = parse_order_quantity(str(form.get("quantity") or ""))
        position_size_percent = parse_position_size_percent(
            str(form.get("position_size_percent") or "")
        )
        placed_order = await TradierClient(get_settings()).submit_long_equity_position(
            symbol,
            quantity,
            position_size_percent,
        )
    except ValueError as exc:
        position_entry_error = str(exc)
    except TradierCredentialsMissing:
        position_entry_error = "Add TRADIER_ACCOUNT_ID and TRADIER_API_TOKEN to your .env file before placing orders."
    except TradierOrderSizingError as exc:
        position_entry_error = str(exc)
    except TradierAPIError as exc:
        position_entry_error = str(exc)
    except Exception:
        position_entry_error = "Unable to place the Tradier order right now."

    context = page_context(
        request,
        "positions",
        placed_order=placed_order,
        position_entry_error=position_entry_error,
    )
    return templates.TemplateResponse(request, "partials/position-entry-result.html", context)


@app.get("/positions/entry/cancel", response_class=HTMLResponse)
async def cancel_position_entry(request: Request):
    context = page_context(request, "positions")
    return templates.TemplateResponse(request, "partials/position-entry-result.html", context)


@app.post("/positions/exit", response_class=HTMLResponse)
async def exit_position(request: Request):
    form = await request.form()
    placed_order = None
    position_entry_error = ""

    try:
        symbol = parse_symbol(str(form.get("symbol") or ""))
        quantity = parse_position_quantity(str(form.get("quantity") or ""))
        placed_order = await TradierClient(get_settings()).place_exit_equity_position(symbol, quantity)
    except ValueError as exc:
        position_entry_error = str(exc)
    except TradierCredentialsMissing:
        position_entry_error = "Add TRADIER_ACCOUNT_ID and TRADIER_API_TOKEN to your .env file before placing orders."
    except TradierOrderSizingError as exc:
        position_entry_error = str(exc)
    except TradierAPIError as exc:
        position_entry_error = str(exc)
    except Exception:
        position_entry_error = "Unable to place the Tradier exit order right now."

    context = await positions_context(
        request,
        placed_order=placed_order,
        position_entry_error=position_entry_error,
    )
    return templates.TemplateResponse(request, context["partial_template"], context)


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


@app.get("/partials/positions/{symbol}", response_class=HTMLResponse)
async def read_position_detail_partial(request: Request, symbol: str):
    try:
        context = await position_detail_context(request, symbol)
    except ValueError:
        raise HTTPException(status_code=404, detail="Position not found") from None
    return templates.TemplateResponse(request, context["partial_template"], context)


@app.get("/partials/{section_key}", response_class=HTMLResponse)
async def read_section_partial(
    request: Request,
    section_key: str,
    period: str = DEFAULT_HISTORICAL_BALANCE_PERIOD,
):
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

    context = await overview_context(request, period)
    return templates.TemplateResponse(request, context["partial_template"], context)
