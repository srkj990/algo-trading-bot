from __future__ import annotations

import re

from brokers.base import BrokerClient, OrderRequest, OrderResult, OrderStatus, PositionSnapshot, Quote
from config import (
    get_access_token,
    get_api_key,
    get_broker_ip_mode,
    get_upstox_access_token,
    get_upstox_static_ip,
)
from network_utils import broker_request, configure_kite_client_network

UPSTOX_ORDER_URL = "https://api-hft.upstox.com/v3/order/place"
IPV4_PATTERN = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
IPV6_PATTERN = re.compile(r"\b(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}\b")


class KiteBrokerClient(BrokerClient):
    def __init__(self):
        self._client = None

    def _get_client(self):
        if self._client is None:
            from kiteconnect import KiteConnect

            self._client = configure_kite_client_network(
                KiteConnect(api_key=get_api_key()),
                ip_mode=get_broker_ip_mode(),
            )
            self._client.set_access_token(get_access_token())
        return self._client

    @staticmethod
    def _parse_symbol_exchange(symbol):
        if not symbol:
            raise ValueError("Symbol is required")
        if ":" in symbol:
            exchange, tradingsymbol = symbol.split(":", 1)
            return exchange.upper(), tradingsymbol.replace(".NS", "")
        return "NSE", symbol.replace(".NS", "")

    def _product_constant(self, product):
        kite = self._get_client()
        normalized = (product or "MIS").upper()
        mapping = {"MIS": kite.PRODUCT_MIS, "CNC": kite.PRODUCT_CNC, "NRML": kite.PRODUCT_NRML}
        return mapping.get(normalized, kite.PRODUCT_MIS)

    def _order_type_constant(self, order_type):
        kite = self._get_client()
        normalized = (order_type or "MARKET").upper()
        mapping = {
            "MARKET": kite.ORDER_TYPE_MARKET,
            "LIMIT": kite.ORDER_TYPE_LIMIT,
            "SL": kite.ORDER_TYPE_SL,
            "SL-M": kite.ORDER_TYPE_SLM,
        }
        return mapping.get(normalized, kite.ORDER_TYPE_MARKET)

    def place_order(self, order: OrderRequest) -> OrderResult:
        kite = self._get_client()
        exchange, tradingsymbol = self._parse_symbol_exchange(order.symbol)
        transaction_type = kite.TRANSACTION_TYPE_BUY if order.side == "BUY" else kite.TRANSACTION_TYPE_SELL
        exchange_map = {
            "NSE": kite.EXCHANGE_NSE,
            "BSE": kite.EXCHANGE_BSE,
            "NFO": kite.EXCHANGE_NFO,
            "BFO": kite.EXCHANGE_BFO,
        }
        kite_exchange = exchange_map.get(exchange)
        if kite_exchange is None:
            raise ValueError(f"Unsupported Kite exchange for order placement: {exchange}")
        order_id = kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange=kite_exchange,
            tradingsymbol=tradingsymbol,
            transaction_type=transaction_type,
            quantity=order.quantity,
            product=self._product_constant(order.product),
            order_type=self._order_type_constant(order.order_type),
            price=float(order.price or 0),
            trigger_price=float(order.trigger_price or 0),
            validity=order.validity,
        )
        return OrderResult(
            order_id=str(order_id),
            status=OrderStatus.PENDING,
            requested_quantity=int(order.quantity),
            pending_quantity=int(order.quantity),
        )

    def get_order_status(self, order_id: str) -> OrderResult | None:
        for item in reversed(self._get_client().orders()):
            if str(item.get("order_id") or "") != str(order_id):
                continue
            status_text = str(item.get("status") or "").upper()
            status = OrderStatus.PENDING
            if status_text in {"COMPLETE", "FILLED"}:
                status = OrderStatus.FILLED
            elif status_text in {"OPEN", "OPEN PENDING", "TRIGGER PENDING", "MODIFY PENDING", "PUT ORDER REQ RECEIVED"}:
                status = OrderStatus.PENDING
            elif int(item.get("filled_quantity") or 0) > 0:
                status = OrderStatus.PARTIAL
            elif status_text in {"REJECTED"}:
                status = OrderStatus.REJECTED
            elif status_text in {"CANCELLED", "CANCELED"}:
                status = OrderStatus.CANCELLED
            return OrderResult(
                order_id=str(order_id),
                status=status,
                message=item.get("status_message"),
                requested_quantity=int(item.get("quantity") or 0),
                filled_quantity=int(item.get("filled_quantity") or 0),
                pending_quantity=int(item.get("pending_quantity") or 0),
                average_price=float(item.get("average_price") or 0) or None,
                parent_order_id=item.get("parent_order_id"),
            )
        return None

    def get_positions(self) -> list[PositionSnapshot]:
        response = self._get_client().positions()
        snapshots = []
        for item in response.get("net", []):
            quantity = int(item.get("quantity") or item.get("net_quantity") or 0)
            if quantity == 0:
                continue
            snapshots.append(
                PositionSnapshot(
                    symbol=item.get("tradingsymbol") or "",
                    quantity=abs(quantity),
                    average_price=float(item.get("average_price") or item.get("buy_price") or 0),
                    side="BUY" if quantity > 0 else "SELL",
                )
            )
        return snapshots

    def get_quote(self, symbol: str) -> Quote:
        exchange, tradingsymbol = self._parse_symbol_exchange(symbol)
        quote_key = f"{exchange}:{tradingsymbol}"
        payload = self._get_client().quote([quote_key])
        data = payload.get(quote_key) or {}
        depth = data.get("depth") or {}
        buy = depth.get("buy") or []
        sell = depth.get("sell") or []
        best_bid = buy[0].get("price") if buy else None
        best_ask = sell[0].get("price") if sell else None
        return Quote(
            symbol=symbol,
            last_price=float(data.get("last_price") or 0.0),
            bid_price=float(best_bid) if best_bid is not None else None,
            ask_price=float(best_ask) if best_ask is not None else None,
        )

    def cancel_order(self, order_id: str) -> bool:
        self._get_client().cancel_order(variety="regular", order_id=order_id)
        return True

    def get_intraday_positions(self) -> list[dict]:
        response = self._get_client().positions()
        return [item for item in response.get("net", []) if (item.get("product") or "").upper() == "MIS"]

    def get_delivery_holdings(self) -> list[dict]:
        return self._get_client().holdings()

    def get_nfo_positions(self) -> list[dict]:
        response = self._get_client().positions()
        positions = []
        for item in response.get("net", []):
            tradingsymbol = item.get("tradingsymbol")
            if not tradingsymbol:
                continue
            exchange = (item.get("exchange") or "").upper()
            if exchange != "NFO" and not tradingsymbol.upper().endswith(("FUT", "CE", "PE")):
                continue
            quantity = int(item.get("quantity") or item.get("net_quantity") or 0)
            if quantity == 0:
                continue
            positions.append(
                {
                    "exchange": exchange,
                    "tradingsymbol": tradingsymbol,
                    "quantity": quantity,
                    "average_price": float(item.get("average_price") or item.get("buy_price") or 0),
                    "product": item.get("product"),
                }
            )
        return positions

    def get_available_margin(self, product: str | None = None) -> float | None:
        del product
        margins = self._get_client().margins()
        equity = margins.get("equity") or {}
        available = equity.get("available") or {}
        candidates = (
            available.get("live_balance"),
            available.get("cash"),
            available.get("opening_balance"),
            equity.get("net"),
        )
        for value in candidates:
            try:
                amount = float(value)
            except (TypeError, ValueError):
                continue
            if amount >= 0:
                return amount
        return None


class UpstoxBrokerClient(BrokerClient):
    def __init__(self):
        self._symbol_cache: dict[str, str] = {}

    def _headers(self):
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {get_upstox_access_token().strip()}",
        }

    @staticmethod
    def extract_error_detail(response):
        try:
            payload = response.json()
        except ValueError:
            return response.text.strip() or "Unknown Upstox error"

        errors = payload.get("errors") or []
        if errors:
            formatted = []
            for item in errors:
                code = item.get("errorCode") or item.get("error_code") or "UNKNOWN"
                message = item.get("message") or "Unknown error"
                formatted.append(f"{code}: {message}")
            return " | ".join(formatted)

        return payload.get("message") or response.text.strip() or "Unknown Upstox error"

    @staticmethod
    def extract_ip_addresses(text):
        candidates = []
        for pattern in (IPV4_PATTERN, IPV6_PATTERN):
            for match in pattern.findall(text or ""):
                cleaned = match.strip(" .,)(")
                if cleaned and cleaned not in candidates:
                    candidates.append(cleaned)
        return candidates

    @staticmethod
    def format_ip_diagnostics(broker_public_ipv4, configured_static_ip, general_public_ipv6, hinted_ips):
        parts = []
        if broker_public_ipv4:
            parts.append(f"broker outbound public IPv4: {broker_public_ipv4}")
        if configured_static_ip:
            parts.append(f"configured Upstox static IP: {configured_static_ip}")
        if general_public_ipv6:
            parts.append(f"general public IPv6 on this laptop: {general_public_ipv6}")

        extra_ips = [ip for ip in hinted_ips if ip not in {broker_public_ipv4, configured_static_ip, general_public_ipv6}]
        if extra_ips:
            parts.append(f"other IP(s) mentioned by broker: {', '.join(extra_ips)}")
        if not parts:
            return ""
        return " | " + " | ".join(parts)

    def _collect_ip_diagnostics(self):
        broker_public_ipv4 = None
        general_public_ipv6 = None

        try:
            response = broker_request("GET", "https://api.ipify.org", timeout=5, ip_mode=get_broker_ip_mode())
            broker_public_ipv4 = response.text.strip() or None
        except Exception:
            broker_public_ipv4 = None

        try:
            response = broker_request("GET", "https://api64.ipify.org", timeout=5, ip_mode="AUTO")
            candidate_ip = response.text.strip()
            if ":" in candidate_ip:
                general_public_ipv6 = candidate_ip
        except Exception:
            general_public_ipv6 = None

        return {
            "broker_public_ipv4": broker_public_ipv4,
            "general_public_ipv6": general_public_ipv6,
            "configured_static_ip": (get_upstox_static_ip() or "").strip() or None,
        }

    def _product_constant(self, product):
        normalized = (product or "MIS").upper()
        mapping = {"MIS": "I", "CNC": "D", "NRML": "D"}
        return mapping.get(normalized, "I")

    def _get_instrument_key(self, symbol):
        tradingsymbol = symbol.replace(".NS", "")
        if tradingsymbol in self._symbol_cache:
            return self._symbol_cache[tradingsymbol]

        response = broker_request(
            "GET",
            "https://api.upstox.com/v2/instruments/search",
            headers=self._headers(),
            params={
                "query": tradingsymbol,
                "exchanges": "NSE",
                "segments": "EQ",
                "page_number": 1,
                "records": 10,
            },
            timeout=30,
            ip_mode=get_broker_ip_mode(),
        )
        response.raise_for_status()
        payload = response.json()
        for item in payload.get("data", []):
            if item.get("exchange") == "NSE" and item.get("trading_symbol") == tradingsymbol:
                self._symbol_cache[tradingsymbol] = item["instrument_key"]
                return item["instrument_key"]

        raise RuntimeError(f"Upstox instrument key not found for {symbol}")

    def place_order(self, order: OrderRequest) -> OrderResult:
        payload = {
            "quantity": order.quantity,
            "product": self._product_constant(order.product),
            "validity": order.validity,
            "price": float(order.price or 0),
            "tag": (order.note or "algo")[:40],
            "instrument_token": self._get_instrument_key(order.symbol),
            "order_type": (order.order_type or "MARKET").upper(),
            "transaction_type": order.side,
            "disclosed_quantity": 0,
            "trigger_price": float(order.trigger_price or 0),
            "is_amo": False,
            "market_protection": -1,
            "slice": False,
        }
        response = broker_request(
            "POST",
            UPSTOX_ORDER_URL,
            headers=self._headers(),
            json=payload,
            timeout=30,
            ip_mode=get_broker_ip_mode(),
        )
        try:
            response.raise_for_status()
        except Exception as exc:
            detail = self.extract_error_detail(response)
            hinted_ips = self.extract_ip_addresses(detail)
            diagnostics = self._collect_ip_diagnostics()
            raise RuntimeError(
                f"Upstox order failed ({response.status_code}) at {UPSTOX_ORDER_URL}: "
                f"{detail}{self.format_ip_diagnostics(diagnostics['broker_public_ipv4'], diagnostics['configured_static_ip'], diagnostics['general_public_ipv6'], hinted_ips)}"
            ) from exc
        return OrderResult(
            order_id=str(response.json().get("data", {}).get("order_id")),
            status=OrderStatus.PENDING,
            requested_quantity=int(order.quantity),
            pending_quantity=int(order.quantity),
        )

    def get_order_status(self, order_id: str) -> OrderResult | None:
        response = broker_request(
            "GET",
            f"https://api.upstox.com/v2/order/details?order_id={order_id}",
            headers=self._headers(),
            timeout=30,
            ip_mode=get_broker_ip_mode(),
        )
        response.raise_for_status()
        payload = response.json().get("data") or {}
        status_text = str(payload.get("status") or "").upper()
        status = OrderStatus.PENDING
        if status_text in {"COMPLETE", "FILLED"}:
            status = OrderStatus.FILLED
        elif int(payload.get("filled_quantity") or 0) > 0:
            status = OrderStatus.PARTIAL
        elif status_text == "REJECTED":
            status = OrderStatus.REJECTED
        elif status_text in {"CANCELLED", "CANCELED"}:
            status = OrderStatus.CANCELLED
        return OrderResult(
            order_id=str(order_id),
            status=status,
            message=payload.get("status_message") or payload.get("message"),
            requested_quantity=int(payload.get("quantity") or 0),
            filled_quantity=int(payload.get("filled_quantity") or 0),
            pending_quantity=int(payload.get("pending_quantity") or 0),
            average_price=float(payload.get("average_price") or 0) or None,
            parent_order_id=payload.get("parent_order_id"),
        )

    def get_positions(self) -> list[PositionSnapshot]:
        snapshots = []
        for item in self.get_intraday_positions():
            quantity = int(item.get("quantity") or 0)
            if quantity == 0:
                continue
            snapshots.append(
                PositionSnapshot(
                    symbol=item.get("tradingsymbol") or "",
                    quantity=abs(quantity),
                    average_price=float(item.get("average_price") or 0),
                    side="BUY" if quantity > 0 else "SELL",
                )
            )
        return snapshots

    def get_quote(self, symbol: str) -> Quote:
        response = broker_request(
            "GET",
            "https://api.upstox.com/v2/market-quote/quotes",
            headers=self._headers(),
            params={"instrument_key": self._get_instrument_key(symbol)},
            timeout=30,
            ip_mode=get_broker_ip_mode(),
        )
        response.raise_for_status()
        data = response.json().get("data", {})
        first_item = next(iter(data.values()), {})
        depth = first_item.get("depth") or {}
        buy = depth.get("buy") or []
        sell = depth.get("sell") or []
        best_bid = buy[0].get("price") if buy else None
        best_ask = sell[0].get("price") if sell else None
        return Quote(
            symbol=symbol,
            last_price=float(first_item.get("last_price") or 0.0),
            bid_price=float(best_bid) if best_bid is not None else None,
            ask_price=float(best_ask) if best_ask is not None else None,
        )

    def cancel_order(self, order_id: str) -> bool:
        response = broker_request(
            "DELETE",
            f"https://api.upstox.com/v2/order/cancel?order_id={order_id}",
            headers=self._headers(),
            timeout=30,
            ip_mode=get_broker_ip_mode(),
        )
        response.raise_for_status()
        return True

    def get_intraday_positions(self) -> list[dict]:
        response = broker_request(
            "GET",
            "https://api.upstox.com/v2/portfolio/short-term-positions",
            headers=self._headers(),
            timeout=30,
            ip_mode=get_broker_ip_mode(),
        )
        response.raise_for_status()
        positions = []
        for item in response.json().get("data", []):
            if (item.get("product") or "").upper() != "I":
                continue
            positions.append(
                {
                    "tradingsymbol": item.get("trading_symbol"),
                    "quantity": int(item.get("quantity") or item.get("net_quantity") or 0),
                    "average_price": float(item.get("average_price") or item.get("buy_price") or 0),
                    "product": "MIS",
                }
            )
        return positions

    def get_delivery_holdings(self) -> list[dict]:
        response = broker_request(
            "GET",
            "https://api.upstox.com/v2/portfolio/long-term-holdings",
            headers=self._headers(),
            timeout=30,
            ip_mode=get_broker_ip_mode(),
        )
        response.raise_for_status()
        holdings = []
        for item in response.json().get("data", []):
            holdings.append(
                {
                    "tradingsymbol": item.get("trading_symbol"),
                    "quantity": int(item.get("quantity") or 0),
                    "t1_quantity": int(item.get("t1_quantity") or 0),
                    "average_price": float(item.get("average_price") or 0),
                    "last_price": float(item.get("last_price") or 0),
                }
            )
        return holdings

    def get_nfo_positions(self) -> list[dict]:
        raise NotImplementedError("Upstox NFO position retrieval is not implemented.")

    def get_available_margin(self, product: str | None = None) -> float | None:
        del product
        response = broker_request(
            "GET",
            "https://api.upstox.com/v2/user/get-funds-and-margin",
            headers=self._headers(),
            timeout=30,
            ip_mode=get_broker_ip_mode(),
        )
        response.raise_for_status()
        payload = response.json().get("data") or {}
        equity = payload.get("equity") or payload
        available = equity.get("available_margin")
        if available is None:
            available = equity.get("available_funds")
        if available is None:
            available = (equity.get("available") or {}).get("cash")
        try:
            return None if available is None else float(available)
        except (TypeError, ValueError):
            return None
