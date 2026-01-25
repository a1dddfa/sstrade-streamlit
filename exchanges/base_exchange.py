# -*- coding: utf-8 -*-
"""
交易所基类
定义统一的API接口
"""
from abc import ABC, abstractmethod
from typing import Dict, List, Optional



import logging
import copy
logger = logging.getLogger(__name__)

# -----------------------------
# Sensitive logging helpers
# -----------------------------
_SENSITIVE_KEYS = {
    "api_key",
    "api_secret",
    "secret",
    "password",
    "token",
    "access_token",
    "refresh_token",
    "private_key",
}

def _mask_sensitive(obj):
    """
    Recursively mask sensitive fields in dict/list for safe logging.
    """
    try:
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                if isinstance(k, str) and k.lower() in _SENSITIVE_KEYS:
                    out[k] = "***MASKED***"
                else:
                    out[k] = _mask_sensitive(v)
            return out
        if isinstance(obj, list):
            return [_mask_sensitive(x) for x in obj]
        return obj
    except Exception:
        # If anything goes wrong, don't risk leaking secrets
        return "***MASKED***"

class BaseExchange(ABC):
    """
    交易所基类
    """
    
    def __init__(self, config: Dict, global_config: Dict = None):
        """
        初始化交易所
        
        Args:
            config: 配置字典
            global_config: 全局配置字典
        """
        self.config = config
        self.global_config = global_config or {}
        self.name = config.get("name", "")
        self.api_key = config.get("api_key", "")
        self.api_secret = config.get("api_secret", "")
        self.api_passphrase = config.get("api_passphrase", "")
        self.testnet = config.get("testnet", False)
        self.proxy = config.get("proxy", None)
        # 添加dry_run支持
        # 打印调试信息
        safe_global = _mask_sensitive(copy.deepcopy(global_config or {}))
        safe_config = _mask_sensitive(copy.deepcopy(config or {}))
        logger.info(
            f"BaseExchange初始化，global_config: {safe_global}, dry_run from global: {self.global_config.get('dry_run', 'NOT FOUND')}"
        )
        logger.info(
            f"Config: {safe_config}, dry_run from config: {self.config.get('dry_run', 'NOT FOUND')}"
        )
        
        self.dry_run = self.global_config.get('dry_run', False) or self.config.get('dry_run', False)
        logger.info(f"最终dry_run值: {self.dry_run}")
        
        # 支持的订单类型
        self.supported_order_types = ['limit', 'market', 'stop_limit', 'trailing_stop']
        
        # 初始化交易所客户端（dry_run模式下不初始化）
        self.client = None
        if not self.dry_run:
            self._init_client()
    
    @abstractmethod
    def _init_client(self):
        """
        初始化交易所客户端
        """
        pass
    
    @abstractmethod
    def get_balance(self) -> Dict:
        """
        获取账户余额
        
        Returns:
            余额信息字典
        """
        pass
    
    @abstractmethod
    def get_ticker(self, symbol: str) -> Dict:
        """
        获取行情信息
        
        Args:
            symbol: 交易对
            
        Returns:
            行情信息字典
        """
        pass
    
    @abstractmethod
    def get_order_book(self, symbol: str, limit: int = 10) -> Dict:
        """
        获取订单簿
        
        Args:
            symbol: 交易对
            limit: 获取数量
            
        Returns:
            订单簿信息字典
        """
        pass
    
    @abstractmethod
    def create_order(self, symbol: str, side: str, order_type: str, 
                     quantity: float, price: Optional[float] = None, 
                     params: Optional[Dict] = None) -> Dict:
        """
        创建订单
        
        Args:
            symbol: 交易对
            side: 方向 (buy/sell/long/short)
            order_type: 订单类型 (limit/market/stop_limit/trailing_stop)
            quantity: 数量
            price: 价格
            params: 其他参数
            
        Returns:
            订单信息字典
        """
        pass
    
    @abstractmethod
    def get_order(self, order_id: str, symbol: Optional[str] = None) -> Dict:
        """
        获取订单信息
        
        Args:
            order_id: 订单ID
            symbol: 交易对 (可选)
            
        Returns:
            订单信息字典
        """
        pass
    
    def get_order_status(self, symbol: str, order_id: str) -> Optional[str]:
        """
        获取订单状态
        
        Args:
            symbol: 交易对
            order_id: 订单ID
            
        Returns:
            订单状态字符串，如果获取失败返回None
        """
        try:
            order = self.get_order(order_id, symbol)
            return order.get('status')
        except Exception as e:
            logger.error(f"获取订单状态失败: {e}")
            return None
    
    @abstractmethod
    def cancel_order(self, order_id: str, symbol: Optional[str] = None) -> bool:
        """
        取消订单
        
        Args:
            order_id: 订单ID
            symbol: 交易对 (可选)
            
        Returns:
            是否取消成功
        """
        pass
    
    @abstractmethod
    def cancel_all_orders(self, symbol: Optional[str] = None, side: Optional[str] = None) -> bool:
        """
        取消所有订单
        
        Args:
            symbol: 交易对 (可选，默认取消所有)
            side: 订单方向 (可选，默认取消所有方向)
            
        Returns:
            是否取消成功
        """
        pass
    
    @abstractmethod
    def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict]:
        """
        获取所有未成交订单
        
        Args:
            symbol: 交易对 (可选，默认获取所有)
            
        Returns:
            未成交订单列表
        """
        pass
    
    @abstractmethod
    def get_positions(self, symbol: Optional[str] = None) -> List[Dict]:
        """
        获取持仓信息
        
        Args:
            symbol: 交易对 (可选，默认返回所有)
            
        Returns:
            持仓信息列表
        """
        pass
    
    @abstractmethod
    def get_kline(self, symbol: str, interval: str, limit: int = 100) -> List[Dict]:
        """
        获取K线数据
        
        Args:
            symbol: 交易对
            interval: 时间周期
            limit: 获取数量
            
        Returns:
            K线数据列表
        """
        pass
    
    def _format_symbol(self, symbol: str) -> str:
        """
        格式化交易对
        
        Args:
            symbol: 交易对
            
        Returns:
            格式化后的交易对
        """
        # 默认实现，子类可重写
        return symbol
    
    def _format_order_type(self, order_type: str) -> str:
        """
        格式化订单类型
        
        Args:
            order_type: 订单类型
            
        Returns:
            格式化后的订单类型
        """
        # 默认实现，子类可重写
        return order_type
    
    def _format_side(self, side: str) -> str:
        """
        格式化方向
        
        Args:
            side: 方向
            
        Returns:
            格式化后的方向
        """
        # 默认实现，子类可重写
        return side
