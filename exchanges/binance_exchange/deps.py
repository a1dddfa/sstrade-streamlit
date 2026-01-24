# -*- coding: utf-8 -*-
"""
Shared dependencies / logging for the Binance exchange implementation.

This module is intentionally small and import-only so other modules can depend on it
without creating circular imports.
"""
import logging
import time
import threading
import os
import json
from decimal import Decimal, ROUND_DOWN
from typing import Dict, List, Optional, Any, Callable

from binance.client import Client
from binance.exceptions import BinanceAPIException
from binance.ws.streams import ThreadedWebsocketManager

logger = logging.getLogger(__name__)
trade_logger = logging.getLogger("trade")
