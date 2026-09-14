from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from trade_view.config import Settings


class TradierCredentialsMissing(RuntimeError):
    pass


class TradierAPIError(RuntimeError):
    pass


class TradierOrderSizingError(RuntimeError):
    pass


@dataclass(frozen=True)
class DisplayPosition:
    symbol: str
    name: str
    cost_basis: str
    shares: str
    held_market_days: str
    exit_quantity: str
    pnl_dollars: str
    pnl_percent: str
    pnl_value: float


@dataclass(frozen=True)
class DisplayWatchlistItem:
    symbol: str
    name: str
    price: str
    change: str
    change_value: float


@dataclass(frozen=True)
class DisplayWatchlist:
    id: str
    name: str
    public_id: str
    items: list[DisplayWatchlistItem]


@dataclass(frozen=True)
class DisplayHistoricalBalancePoint:
    date: str
    value: Decimal
    value_display: str


@dataclass(frozen=True)
class PlacedEquityOrder:
    symbol: str
    quantity: str
    side: str
    last_price: str
    account_value: str
    target_notional: str
    position_size_percent: str
    order_id: str
    status: str


@dataclass(frozen=True)
class PreviewedEquityOrder:
    symbol: str
    quantity: str
    side: str
    last_price: str
    account_value: str
    target_notional: str
    position_size_percent: str
    preview_id: str
    status: str


def normalize_list(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def to_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def format_money(value: Decimal | None) -> str:
    if value is None:
        return "--"
    sign = "-" if value < 0 else ""
    absolute = abs(value)
    return f"{sign}${absolute:,.2f}"


def format_signed_money(value: Decimal | None) -> str:
    if value is None:
        return "--"
    sign = "+" if value >= 0 else "-"
    absolute = abs(value)
    return f"{sign}${absolute:,.2f}"


def format_percent(value: Decimal | None) -> str:
    if value is None:
        return "--"
    sign = "+" if value >= 0 else "-"
    absolute = abs(value)
    return f"{sign}{absolute:.2f}%"


def format_unsigned_percent(value: Decimal | None) -> str:
    if value is None:
        return "--"
    return f"{value:.2f}%"


def format_quantity(value: Decimal | None) -> str:
    if value is None:
        return "--"
    normalized = value.normalize()
    return f"{normalized:f}"


def to_date(value: Any) -> date | None:
    if value in (None, ""):
        return None

    raw_value = str(value).strip()
    if not raw_value:
        return None

    if raw_value.endswith("Z"):
        raw_value = f"{raw_value[:-1]}+00:00"

    try:
        return datetime.fromisoformat(raw_value).date()
    except ValueError:
        try:
            return date.fromisoformat(raw_value[:10])
        except ValueError:
            return None


def current_market_date() -> date:
    return datetime.now(ZoneInfo("America/New_York")).date()


def iter_months(start: date, end: date):
    year = start.year
    month = start.month

    while (year, month) <= (end.year, end.month):
        yield year, month
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1


def parse_open_market_days(days: Any) -> set[date]:
    if not isinstance(days, dict):
        return set()

    open_days: set[date] = set()
    for day in normalize_list(days.get("day")):
        if day.get("status") != "open":
            continue

        market_date = to_date(day.get("date"))
        if market_date is not None:
            open_days.add(market_date)

    return open_days


def format_market_days_held(
    acquired_date: date | None,
    market_days: set[date] | None,
) -> str:
    if acquired_date is None or market_days is None:
        return "--"

    today = current_market_date()
    held_days = sum(1 for market_date in market_days if acquired_date <= market_date <= today)
    return str(held_days)


def position_acquired_sort_key(position: dict[str, Any]) -> tuple[bool, date]:
    acquired_date = to_date(position.get("date_acquired"))
    return acquired_date is None, acquired_date or date.max


HISTORICAL_BALANCE_DATE_KEYS = ("date", "asof_date", "as_of_date")
HISTORICAL_BALANCE_VALUE_KEYS = (
    "value",
    "balance",
    "total_equity",
    "equity",
    "account_value",
)


def historical_balance_sort_key(point: DisplayHistoricalBalancePoint) -> tuple[bool, date]:
    balance_date = to_date(point.date)
    return balance_date is None, balance_date or date.max


class TradierClient:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.settings = settings
        self.transport = transport

    async def get_positions(self) -> list[DisplayPosition]:
        if not self.settings.has_tradier_credentials:
            raise TradierCredentialsMissing("Tradier account ID and API token are not configured.")

        async with httpx.AsyncClient(
            base_url=self.settings.base_url.rstrip("/"),
            headers=self.headers,
            timeout=self.settings.timeout_seconds,
            transport=self.transport,
        ) as client:
            raw_positions = await self.get_account_positions(client)
            quotes = await self.get_quotes(
                client,
                [str(position.get("symbol") or "") for position in raw_positions],
            )
            acquired_dates = [
                acquired_date
                for position in raw_positions
                if (acquired_date := to_date(position.get("date_acquired"))) is not None
            ]
            market_days = await self.get_open_market_days(client, acquired_dates)

        return [
            self.to_display_position(
                position,
                quotes.get(str(position.get("symbol") or ""), {}),
                market_days,
            )
            for position in sorted(raw_positions, key=position_acquired_sort_key)
        ]

    async def get_watchlists(self) -> list[DisplayWatchlist]:
        if not self.settings.has_tradier_token:
            raise TradierCredentialsMissing("Tradier API token is not configured.")

        async with httpx.AsyncClient(
            base_url=self.settings.base_url.rstrip("/"),
            headers=self.headers,
            timeout=self.settings.timeout_seconds,
            transport=self.transport,
        ) as client:
            watchlists = await self.get_all_watchlists(client)
            detailed_watchlists = await self.get_watchlist_details(client, watchlists)
            symbols = [
                item.get("symbol")
                for watchlist in detailed_watchlists
                for item in watchlist.get("items", [])
            ]
            quotes = await self.get_quotes(client, [str(symbol or "") for symbol in symbols])

        return [
            DisplayWatchlist(
                id=str(watchlist.get("id") or ""),
                name=str(watchlist.get("name") or "Watchlist"),
                public_id=str(watchlist.get("public_id") or ""),
                items=[
                    self.to_display_watchlist_item(item, quotes.get(str(item.get("symbol") or ""), {}))
                    for item in watchlist.get("items", [])
                    if item.get("symbol")
                ],
            )
            for watchlist in detailed_watchlists
        ]

    async def get_historical_balances(
        self,
        period: str = "MONTH",
    ) -> list[DisplayHistoricalBalancePoint]:
        if not self.settings.has_tradier_credentials:
            raise TradierCredentialsMissing("Tradier account ID and API token are not configured.")

        async with httpx.AsyncClient(
            base_url=self.settings.base_url.rstrip("/"),
            headers=self.headers,
            timeout=self.settings.timeout_seconds,
            transport=self.transport,
        ) as client:
            raw_balances = await self.get_account_historical_balances(client, period)

        points = [
            point
            for balance in raw_balances
            if (point := self.to_display_historical_balance_point(balance)) is not None
        ]
        return sorted(points, key=historical_balance_sort_key)

    async def preview_long_equity_position(
        self,
        symbol: str,
        position_size_percent: Decimal,
    ) -> PreviewedEquityOrder:
        if not self.settings.has_tradier_credentials:
            raise TradierCredentialsMissing("Tradier account ID and API token are not configured.")

        async with httpx.AsyncClient(
            base_url=self.settings.base_url.rstrip("/"),
            headers=self.headers,
            timeout=self.settings.timeout_seconds,
            transport=self.transport,
        ) as client:
            balances = await self.get_account_balances(client)
            quotes = await self.get_quotes(client, [symbol])

            account_value = to_decimal(balances.get("total_equity"))
            quote = quotes.get(symbol, {})
            last_price = to_decimal(quote.get("last"))
            quantity, target_notional = self.calculate_equity_quantity(
                account_value,
                last_price,
                position_size_percent,
            )
            order = await self.place_equity_order(client, symbol, quantity, "buy", preview=True)

        return PreviewedEquityOrder(
            symbol=symbol,
            quantity=str(quantity),
            side="buy",
            last_price=format_money(last_price),
            account_value=format_money(account_value),
            target_notional=format_money(target_notional),
            position_size_percent=format_unsigned_percent(position_size_percent),
            preview_id=str(order.get("id") or order.get("order_id") or order.get("preview_id") or "--"),
            status=str(order.get("status") or "submitted"),
        )

    async def submit_long_equity_position(
        self,
        symbol: str,
        quantity: Decimal,
        position_size_percent: Decimal,
    ) -> PlacedEquityOrder:
        if not self.settings.has_tradier_credentials:
            raise TradierCredentialsMissing("Tradier account ID and API token are not configured.")

        if quantity <= 0:
            raise TradierOrderSizingError("Unable to submit an order with zero shares.")

        async with httpx.AsyncClient(
            base_url=self.settings.base_url.rstrip("/"),
            headers=self.headers,
            timeout=self.settings.timeout_seconds,
            transport=self.transport,
        ) as client:
            order = await self.place_equity_order(client, symbol, quantity, "buy", preview=False)

        return PlacedEquityOrder(
            symbol=symbol,
            quantity=format_quantity(quantity),
            side="buy",
            last_price="--",
            account_value="--",
            target_notional="--",
            position_size_percent=format_unsigned_percent(position_size_percent),
            order_id=str(order.get("id") or order.get("order_id") or "--"),
            status=str(order.get("status") or "submitted"),
        )

    async def place_exit_equity_position(
        self,
        symbol: str,
        quantity: Decimal,
    ) -> PlacedEquityOrder:
        if not self.settings.has_tradier_credentials:
            raise TradierCredentialsMissing("Tradier account ID and API token are not configured.")

        if quantity == 0:
            raise TradierOrderSizingError("Unable to exit a position with zero shares.")

        side = "sell" if quantity > 0 else "buy_to_cover"
        exit_quantity = abs(quantity)

        async with httpx.AsyncClient(
            base_url=self.settings.base_url.rstrip("/"),
            headers=self.headers,
            timeout=self.settings.timeout_seconds,
            transport=self.transport,
        ) as client:
            order = await self.place_equity_order(client, symbol, exit_quantity, side, preview=False)

        return PlacedEquityOrder(
            symbol=symbol,
            quantity=format_quantity(exit_quantity),
            side=side,
            last_price="--",
            account_value="--",
            target_notional="--",
            position_size_percent="--",
            order_id=str(order.get("id") or order.get("order_id") or "--"),
            status=str(order.get("status") or "submitted"),
        )

    async def add_symbols_to_watchlist(
        self,
        watchlist_id: str,
        symbols: list[str],
    ) -> None:
        if not self.settings.has_tradier_token:
            raise TradierCredentialsMissing("Tradier API token is not configured.")

        symbols = sorted({symbol for symbol in symbols if symbol})
        if not symbols:
            raise TradierAPIError("No symbols were provided for the watchlist.")

        async with httpx.AsyncClient(
            base_url=self.settings.base_url.rstrip("/"),
            headers=self.headers,
            timeout=self.settings.timeout_seconds,
            transport=self.transport,
        ) as client:
            response = await client.post(
                f"/watchlists/{watchlist_id}/symbols",
                data={"symbols": ",".join(symbols)},
            )

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise TradierAPIError(f"Tradier returned HTTP {response.status_code}.") from exc

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.api_token.get_secret_value()}",
            "Accept": "application/json",
        }

    async def get_account_positions(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        response = await client.get(f"/accounts/{self.settings.account_id}/positions")
        data = self.parse_response(response)
        positions = data.get("positions") or {}
        if not isinstance(positions, dict):
            return []
        return normalize_list(positions.get("position"))

    async def get_account_balances(self, client: httpx.AsyncClient) -> dict[str, Any]:
        response = await client.get(f"/accounts/{self.settings.account_id}/balances")
        data = self.parse_response(response)
        balances = data.get("balances") or {}
        if not isinstance(balances, dict):
            raise TradierAPIError("Tradier returned an unexpected balances response.")
        return balances

    async def get_account_historical_balances(
        self,
        client: httpx.AsyncClient,
        period: str,
    ) -> list[dict[str, Any]]:
        response = await client.get(
            f"/accounts/{self.settings.account_id}/historical-balances",
            params={"period": period},
        )
        data = self.parse_response(response)
        return self.extract_historical_balance_records(data)

    async def get_open_market_days(
        self,
        client: httpx.AsyncClient,
        acquired_dates: list[date],
    ) -> set[date] | None:
        if not acquired_dates:
            return None

        today = current_market_date()
        start = min(acquired_dates)
        market_days: set[date] = set()

        try:
            for year, month in iter_months(start, today):
                response = await client.get(
                    "/markets/calendar",
                    params={"month": month, "year": year},
                )
                data = self.parse_response(response)
                calendar = data.get("calendar") or {}
                days = calendar.get("days") if isinstance(calendar, dict) else {}
                market_days.update(parse_open_market_days(days))
        except (TradierAPIError, httpx.HTTPError):
            return None

        return market_days

    async def get_all_watchlists(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        response = await client.get("/watchlists")
        data = self.parse_response(response)
        watchlists = data.get("watchlists") or {}
        if not isinstance(watchlists, dict):
            return []
        return normalize_list(watchlists.get("watchlist"))

    async def get_watchlist_details(
        self,
        client: httpx.AsyncClient,
        watchlists: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        detailed_watchlists = []

        for watchlist in watchlists:
            watchlist_id = watchlist.get("id")
            if not watchlist_id:
                continue

            response = await client.get(f"/watchlists/{watchlist_id}")
            data = self.parse_response(response)
            detail = data.get("watchlist") or {}
            if not isinstance(detail, dict):
                continue

            items = detail.get("items") or {}
            detail["items"] = normalize_list(items.get("item")) if isinstance(items, dict) else []
            detailed_watchlists.append(detail)

        return detailed_watchlists

    async def get_quotes(
        self,
        client: httpx.AsyncClient,
        symbols: list[str],
    ) -> dict[str, dict[str, Any]]:
        symbols = sorted({symbol for symbol in symbols if symbol})
        if not symbols:
            return {}

        response = await client.get("/markets/quotes", params={"symbols": ",".join(symbols)})
        data = self.parse_response(response)
        quotes = data.get("quotes") or {}
        if not isinstance(quotes, dict):
            return {}
        return {quote["symbol"]: quote for quote in normalize_list(quotes.get("quote")) if quote.get("symbol")}

    async def place_equity_order(
        self,
        client: httpx.AsyncClient,
        symbol: str,
        quantity: Decimal | int,
        side: str,
        preview: bool,
    ) -> dict[str, Any]:
        response = await client.post(
            f"/accounts/{self.settings.account_id}/orders",
            data={
                "class": "equity",
                "symbol": symbol,
                "duration": "day",
                "side": side,
                "quantity": format_quantity(to_decimal(quantity)),
                "type": "market",
                "preview": "true" if preview else "false",
                "tag": "trade-view",
            },
        )
        data = self.parse_response(response)
        order = data.get("order") or data
        if not isinstance(order, dict):
            raise TradierAPIError("Tradier returned an unexpected order response.")
        return order

    def calculate_equity_quantity(
        self,
        account_value: Decimal | None,
        last_price: Decimal | None,
        position_size_percent: Decimal,
    ) -> tuple[int, Decimal]:
        if account_value is None or account_value <= 0:
            raise TradierOrderSizingError("Unable to determine account value from Tradier balances.")
        if last_price is None or last_price <= 0:
            raise TradierOrderSizingError("Unable to determine a valid current quote for this symbol.")

        target_notional = account_value * position_size_percent / Decimal("100")
        quantity = int((target_notional / last_price).to_integral_value(rounding=ROUND_FLOOR))
        if quantity < 1:
            raise TradierOrderSizingError("Position size is too small to buy at least one whole share.")

        return quantity, target_notional

    def parse_response(self, response: httpx.Response) -> dict[str, Any]:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise TradierAPIError(f"Tradier returned HTTP {response.status_code}.") from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise TradierAPIError("Tradier returned a non-JSON response.") from exc
        if not isinstance(data, dict):
            raise TradierAPIError("Tradier returned an unexpected response shape.")
        return data

    def extract_historical_balance_records(self, data: Any) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []

        def walk(value: Any) -> None:
            if isinstance(value, list):
                for item in value:
                    walk(item)
                return

            if not isinstance(value, dict):
                return

            has_date = any(key in value for key in HISTORICAL_BALANCE_DATE_KEYS)
            has_value = any(key in value for key in HISTORICAL_BALANCE_VALUE_KEYS)
            if has_date and has_value:
                records.append(value)
                return

            for nested_value in value.values():
                walk(nested_value)

        walk(data)
        return records

    def to_display_position(
        self,
        position: dict[str, Any],
        quote: dict[str, Any],
        market_days: set[date] | None,
    ) -> DisplayPosition:
        symbol = str(position.get("symbol") or "")
        quantity = to_decimal(position.get("quantity"))
        cost_basis = to_decimal(position.get("cost_basis"))
        last = to_decimal(quote.get("last"))
        multiplier = to_decimal(quote.get("contract_size")) or Decimal("1")
        acquired_date = to_date(position.get("date_acquired"))

        current_value: Decimal | None = None
        pnl: Decimal | None = None
        pnl_percent: Decimal | None = None
        if quantity is not None and cost_basis is not None and last is not None:
            current_value = quantity * last * multiplier
            pnl = current_value - cost_basis
            if cost_basis:
                pnl_percent = pnl / abs(cost_basis) * Decimal("100")

        return DisplayPosition(
            symbol=symbol,
            name=str(quote.get("description") or symbol),
            cost_basis=format_money(cost_basis),
            shares=format_quantity(quantity),
            held_market_days=format_market_days_held(acquired_date, market_days),
            exit_quantity=format_quantity(quantity),
            pnl_dollars=format_signed_money(pnl),
            pnl_percent=format_percent(pnl_percent),
            pnl_value=float(pnl or Decimal("0")),
        )

    def to_display_historical_balance_point(
        self,
        balance: dict[str, Any],
    ) -> DisplayHistoricalBalancePoint | None:
        balance_date = next(
            (
                str(balance[key])
                for key in HISTORICAL_BALANCE_DATE_KEYS
                if balance.get(key) not in (None, "")
            ),
            "",
        )
        value = None
        for key in HISTORICAL_BALANCE_VALUE_KEYS:
            if balance.get(key) in (None, ""):
                continue

            value = to_decimal(balance[key])
            if value is not None:
                break

        if not balance_date or value is None:
            return None

        return DisplayHistoricalBalancePoint(
            date=balance_date[:10],
            value=value,
            value_display=format_money(value),
        )

    def to_display_watchlist_item(
        self,
        item: dict[str, Any],
        quote: dict[str, Any],
    ) -> DisplayWatchlistItem:
        symbol = str(item.get("symbol") or quote.get("symbol") or "")
        change_percent = to_decimal(quote.get("change_percentage"))
        change = to_decimal(quote.get("change"))
        change_display = format_percent(change_percent)
        if change_display == "--":
            change_display = format_signed_money(change)

        return DisplayWatchlistItem(
            symbol=symbol,
            name=str(quote.get("description") or symbol),
            price=format_money(to_decimal(quote.get("last"))),
            change=change_display,
            change_value=float(change or change_percent or Decimal("0")),
        )
