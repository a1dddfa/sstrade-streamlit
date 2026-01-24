# -*- coding: utf-8 -*-
"""
Public entrypoint: BinanceExchange

This class is composed via mixins split from the original monolithic implementation.
"""
from .core import CoreBinanceExchange
from .websocket import WebsocketMixin
from .persistence import PersistenceMixin
from .rate_limit import RateLimitMixin
from .account import AccountMixin
from .orders import OrdersMixin
from .market_data import MarketDataMixin
from .precision import PrecisionMixin
from .leverage import LeverageMixin
from .scanner import ScannerMixin


class BinanceExchange(
    WebsocketMixin,
    PersistenceMixin,
    RateLimitMixin,
    AccountMixin,
    OrdersMixin,
    MarketDataMixin,
    PrecisionMixin,
    LeverageMixin,
    ScannerMixin,
    CoreBinanceExchange,
):
    """Binance exchange implementation (split into mixins)."""
    pass
