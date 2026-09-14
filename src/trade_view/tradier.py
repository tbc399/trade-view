from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from trade_view.config import Settings


class TradierCredentialsMissing(RuntimeError):
    pass


class TradierAPIError(RuntimeError):
    pass


@dataclass(frozen=True)
class DisplayPosition:
    symbol: str
    name: str
    cost_basis: str
    shares: str
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


def format_quantity(value: Decimal | None) -> str:
    if value is None:
        return "--"
    normalized = value.normalize()
    return f"{normalized:f}"


class TradierClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def get_positions(self) -> list[DisplayPosition]:
        if not self.settings.has_tradier_credentials:
            raise TradierCredentialsMissing("Tradier account ID and API token are not configured.")

        async with httpx.AsyncClient(
            base_url=self.settings.base_url.rstrip("/"),
            headers=self.headers,
            timeout=self.settings.timeout_seconds,
        ) as client:
            raw_positions = await self.get_account_positions(client)
            quotes = await self.get_quotes(
                client,
                [str(position.get("symbol") or "") for position in raw_positions],
            )

        return [
            self.to_display_position(position, quotes.get(str(position.get("symbol") or ""), {}))
            for position in raw_positions
        ]

    async def get_watchlists(self) -> list[DisplayWatchlist]:
        if not self.settings.has_tradier_token:
            raise TradierCredentialsMissing("Tradier API token is not configured.")

        async with httpx.AsyncClient(
            base_url=self.settings.base_url.rstrip("/"),
            headers=self.headers,
            timeout=self.settings.timeout_seconds,
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

    def to_display_position(
        self,
        position: dict[str, Any],
        quote: dict[str, Any],
    ) -> DisplayPosition:
        symbol = str(position.get("symbol") or "")
        quantity = to_decimal(position.get("quantity"))
        cost_basis = to_decimal(position.get("cost_basis"))
        last = to_decimal(quote.get("last"))
        multiplier = to_decimal(quote.get("contract_size")) or Decimal("1")

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
            pnl_dollars=format_signed_money(pnl),
            pnl_percent=format_percent(pnl_percent),
            pnl_value=float(pnl or Decimal("0")),
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
