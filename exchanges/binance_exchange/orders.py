# -*- coding: utf-8 -*-
"""
Auto-split from the original binance_exchange.py.
"""
import logging
from .deps import (
    logger, trade_logger,
    time, threading, os, json,
    Decimal, ROUND_DOWN,
    Dict, List, Optional, Any, Callable,
    Client, BinanceAPIException, ThreadedWebsocketManager,
)


class OrdersMixin:
    def _futures_algo_create_order(self, api_params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Binance USDⓈ-M Futures Algo conditional order
        正确 endpoint: POST /fapi/v1/algoOrder
        """
        try:
            # 给 signed endpoint 兜底补 timestamp/recvWindow（避免库版本差异）
            import time
            api_params = dict(api_params)
            api_params.setdefault("recvWindow", 5000)
            api_params.setdefault("timestamp", int(time.time() * 1000))

            # ✅ 关键：这里必须是 "algoOrder"（不是 "algo/order"）
            result = self.client._request_futures_api(
                method="post",
                path="algoOrder",
                signed=True,
                data=api_params
            )
            logger.info(f"币安API返回Algo订单: {result}")
            return result
        except Exception as e:
            logger.error(f"创建Algo订单失败: {e}")
            raise

    def _futures_algo_cancel_order(self, *, symbol: str, algo_id: str | int) -> Dict[str, Any]:
        """Cancel Binance USDⓈ-M Futures Algo conditional order.

        Endpoint: DELETE /fapi/v1/algoOrder
        Notes:
          - Some conditional orders are returned as algo orders (algoId/algoOrderId) rather than orderId.
          - The official param is `algoId`.
        """
        # Give signed endpoint a timestamp/recvWindow (avoid lib version differences)
        import time

        api_params: Dict[str, Any] = {
            "symbol": self._format_symbol(symbol),
            "algoId": str(algo_id),
            "recvWindow": 5000,
            "timestamp": int(time.time() * 1000),
        }
        result = self.client._request_futures_api(
            method="delete",
            path="algoOrder",
            signed=True,
            data=api_params,
        )
        logger.info(f"币安API返回Algo撤单: {result}")
        return result

    def cancel_algo_order(self, *, symbol: str, algo_id: str | int) -> bool:
        """Cancel USDⓈ-M Futures algo conditional order by algoId.
        Endpoint: DELETE /fapi/v1/algoOrder
        """
        if self.dry_run:
            logger.info(f"[DRY RUN] cancel_algo_order | symbol={symbol} algo_id={algo_id}")
            return True

        params = {
            "symbol": self._format_symbol(symbol),
            "algoId": str(algo_id),
            "recvWindow": 5000,
            "timestamp": int(time.time() * 1000),
        }
        # python-binance internal futures request (consistent with create-algo path)
        res = self.client._request_futures_api(
            method="delete",
            path="algoOrder",
            signed=True,
            data=params,
        )
        logger.info(f"cancel_algo_order resp | {res}")
        return bool(res)

    def cancel_order_any(self, *, symbol: str, order_id: str | int | None = None, client_order_id: str | None = None,
                         algo_id: str | int | None = None) -> bool:
        """Best-effort cancel that supports both standard and algo conditional orders.

        Priority:
          1) standard orderId/clientOrderId via futures_cancel_order
          2) algoId via DELETE /fapi/v1/algoOrder
        """
        if order_id is not None or client_order_id is not None:
            # Prefer standard cancel first (works for normal STOP/STOP_LIMIT too)
            oid = str(order_id) if order_id is not None else str(client_order_id)
            ok = self.cancel_order(order_id=oid, symbol=symbol)
            if ok:
                return True

        if algo_id is None:
            return False

        try:
            if self.dry_run:
                logger.info(f"[DRY RUN] 取消Algo订单成功: algo_id={algo_id}")
                trade_logger.info(
                    "CANCEL_ALGO_ORDER | dry_run=1 | algo_id=%s | symbol=%s",
                    str(algo_id), self._format_symbol(symbol)
                )
                return True

            res = self._futures_algo_cancel_order(symbol=symbol, algo_id=algo_id)
            ok = bool(res)
            if ok:
                trade_logger.info(
                    "CANCEL_ALGO_ORDER | dry_run=0 | algo_id=%s | symbol=%s",
                    str(algo_id), self._format_symbol(symbol)
                )
            return ok
        except Exception as e:
            logger.error(f"取消Algo订单失败: {e}")
            return False

    def create_order(self, symbol: str, side: str, order_type: str,
                     quantity: float, price: Optional[float] = None,
                     params: Optional[Dict] = None) -> Dict:
        """
        创建订单

        Args:
            symbol: 交易对
            side: 方向 (long/short)
            order_type: 订单类型 (limit/market/stop_limit/trailing_stop)
            quantity: 数量
            price: 价格
            params: 其他参数

        Returns:
            订单信息字典
        """
        try:
            # =========================
            # ⭐ 方案B：本地触发价 -> 到价后才提交订单到交易所
            # =========================
            p0 = (params or {}).copy()
            if not bool(p0.get("_skip_local_trigger")):
                ltp = (
                    p0.get("localTriggerPrice")
                    or p0.get("local_trigger_price")
                    or p0.get("local_trigger")
                )
                if ltp is not None:
                    immediate = bool(
                        p0.get("localTriggerImmediate")
                        or p0.get("local_trigger_immediate")
                        or False
                    )
                    # 条件：默认按方向推断（long: gte, short: lte），也可显式传
                    cond = str(
                        p0.get("localTriggerCondition")
                        or p0.get("local_trigger_condition")
                        or ("gte" if str(side).lower() == "long" else "lte")
                    ).lower().strip()

                    if not immediate:
                        # 触发时再 create_order：强制跳过本地触发逻辑，避免递归
                        req = {
                            "symbol": symbol,
                            "side": side,
                            "order_type": order_type,
                            "quantity": float(quantity),
                            "price": price,
                            "params": {k: v for k, v in p0.items()
                                       if k not in ("localTriggerPrice","local_trigger_price","local_trigger",
                                                    "localTriggerImmediate","local_trigger_immediate",
                                                    "localTriggerCondition","local_trigger_condition")}
                        }
                        req["params"]["_skip_local_trigger"] = True

                        tag = p0.get("tag") or "LOCAL_TRIGGER"
                        return self.schedule_local_trigger_order(
                            symbol=symbol,
                            activate_price=float(ltp),
                            activate_condition=str(cond),
                            order_request=req,
                            tag=str(tag),
                        )
                    # immediate=True：就当没开本地触发，继续走原下单逻辑（立即提交到交易所）

            # ⭐ 两段式：deferred_stop_limit（先到 activatePrice 再挂 STOP_LIMIT）
            if str(order_type).lower() in ("deferred_stop_limit", "delayed_stop_limit", "arm_stop_limit", "defer_stop_limit"):
                p = (params or {}).copy()
                activate_price = p.get("activatePrice") or p.get("armPrice") or p.get("deferPrice")
                stop_price = p.get("stopPrice") or p.get("stop_price")
                limit_price = p.get("limitPrice") or price
                position_side = p.get("positionSide") or p.get("position_side") or "LONG"
                tag = p.get("tag") or "DEFER_STOPLIMIT"
                act_cond = p.get("activateCondition") or p.get("armCondition")

                if activate_price is None or stop_price is None or limit_price is None:
                    raise ValueError("deferred_stop_limit requires activatePrice/stopPrice/limitPrice(or price)")

                q0 = None
                try:
                    if quantity is not None and float(quantity) > 0:
                        q0 = float(quantity)
                except Exception:
                    q0 = None

                return self.schedule_deferred_stop_limit(
                    symbol=symbol,
                    position_side=str(position_side),
                    close_side=side,
                    activate_price=float(activate_price),
                    stop_price=float(stop_price),
                    limit_price=float(limit_price),
                    quantity=q0,
                    tag=str(tag),
                    activate_condition=str(act_cond) if act_cond is not None else None,
                )

            # ========== 1. DRY RUN 分支 ==========
            if self.dry_run:
                # 模拟创建订单
                symbol_fmt = self._format_symbol(symbol)
                side_fmt = self._format_side(side)
                type_fmt = self._format_order_type(order_type)

                position_side = params.get('positionSide', 'BOTH') if params else 'BOTH'
                stop_price = params.get('stopPrice', '0.0') if params else '0.0'

                order = {
                    'orderId': str(int(time.time() * 1000)),
                    'symbol': symbol_fmt,
                    'status': 'NEW',
                    'clientOrderId': f'dry_{int(time.time() * 1000)}',
                    'price': str(price or 0.0),
                    'avgPrice': '0.0',
                    'origQty': str(quantity),
                    'executedQty': '0.0',
                    'cumQuote': '0.0',
                    'timeInForce': 'GTC',
                    'type': type_fmt,
                    'reduceOnly': False,
                    'closePosition': False,
                    'side': side_fmt,
                    'positionSide': position_side,
                    'stopPrice': stop_price,
                    'workingType': 'CONTRACT_PRICE',
                    'priceProtect': False,
                    'origType': type_fmt,
                    'time': int(time.time() * 1000),
                    'updateTime': int(time.time() * 1000)
                }

                # 把策略传进来的 tag 附加回订单对象，方便 on_order_update 使用
                if params and 'tag' in params:
                    order['tag'] = params['tag']

                trade_logger.info(
                    "CREATE_ORDER | dry_run=1 | symbol=%s | side=%s | type=%s | qty=%s | price=%s | orderId=%s | clientId=%s | tag=%s",
                    order.get("symbol"), order.get("side"), order.get("type"),
                    order.get("origQty"), order.get("price"),
                    order.get("orderId"), order.get("clientOrderId"), order.get("tag"),
                )
                return order

            # ========== 2. 实盘分支：先处理参数 ==========
            symbol_fmt = self._format_symbol(symbol)
            side_fmt = self._format_side(side)
            type_fmt = self._format_order_type(order_type)

            processed_params = (params or {}).copy()

            # 统一处理 stop_price / stopPrice
            stop_price = processed_params.get('stop_price') or processed_params.get('stopPrice')
            if stop_price is not None:
                processed_params['stopPrice'] = stop_price
                if 'stop_price' in processed_params:
                    del processed_params['stop_price']

            # 某些类型需要 stopPrice，但你当前策略主要用 STOP / TAKE_PROFIT / STOP_LOSS_LIMIT，这里保留占位逻辑
            if type_fmt in ['STOP', 'TAKE_PROFIT', 'STOP_LOSS_LIMIT', 'TAKE_PROFIT_LIMIT']:
                _has_stop_price = 'stopPrice' in processed_params
                _position_side = processed_params.get('positionSide', 'LONG')
                # 这里暂时不做额外转换，只是保留结构

            # 精度处理（注意：如果以后用到纯市价单，这里要加 price 为 None 的判断）
            quantity_precise = self.amount_to_precision(symbol_fmt, quantity)
            price_precise = self.price_to_precision(symbol_fmt, price) if price is not None else None

            # ✅ 防御：精度截断可能把 quantity 变成 0（例如低于 minQty/stepSize）
            # 这会导致 Binance 返回 "Quantity less than or equal to zero"。
            try:
                _q = float(quantity_precise)
            except Exception:
                try:
                    _q = float(Decimal(str(quantity_precise)))
                except Exception:
                    _q = None

            if _q is not None and _q <= 0:
                raise ValueError(
                    f"Quantity after precision is <= 0 (symbol={symbol_fmt}, raw={quantity}, precise={quantity_precise}). "
                    "请提高下单数量或检查该交易对的最小下单量/步进。"
                )

            if 'stopPrice' in processed_params:
                processed_params['stopPrice'] = self.price_to_precision(symbol_fmt, processed_params['stopPrice'])

            # 构造主订单参数
            order_params: Dict[str, Any] = {
                'symbol': symbol_fmt,
                'side': side_fmt,
                'type': type_fmt,
                'quantity': quantity_precise,
                'timeInForce': 'GTC',
                **processed_params
            }
            if price_precise is not None:
                order_params['price'] = price_precise

            # 默认 positionSide：只在上层没传时，保守用 BOTH（兼容单向账户）
            if 'positionSide' not in order_params:
                order_params['positionSide'] = 'BOTH'
            
            # ⭐ 如果上层传了 tag，用它生成 newClientOrderId，方便后续通过 clientOrderId 识别策略订单
            tag_value = processed_params.get('tag')
            if tag_value:
                # 简单做一下字符清洗（只保留字母数字和下划线/中划线）
                safe_tag = ''.join(
                    c if c.isalnum() or c in ['_', '-'] else '_' 
                    for c in str(tag_value)
                )
                client_id = f"{safe_tag}_{int(time.time() * 1000)}"
                order_params['newClientOrderId'] = client_id
            
            # ⭐ 对 MARKET / STOP_MARKET 不要传 timeInForce，避免报错
            if type_fmt in ['MARKET', 'STOP_MARKET']:
                order_params.pop('timeInForce', None)

            logger.info(
                f"[BINANCE] CREATE_MAIN_ORDER | symbol={symbol_fmt} | side={side_fmt} | "
                f"type={type_fmt} | qty={quantity_precise} | price={price_precise} | "
                f"params={processed_params}"
            )

            # 从 processed_params 中拿出 take_profit / stop_loss 配置（不传给 Binance）
            take_profit = processed_params.get('take_profit')
            stop_loss = processed_params.get('stop_loss')
            # 保证不会被传进 api_params
            if 'take_profit' in order_params:
                del order_params['take_profit']
            if 'stop_loss' in order_params:
                del order_params['stop_loss']

            # ========== 3. 先创建主订单 ==========
            # 只保留 Binance 支持的字段
            standard_fields = [
                'symbol', 'side', 'type', 'quantity', 'price', 'timeInForce',
                'stopPrice', 'reduceOnly', 'positionSide', 'closePosition',
                'workingType', 'priceProtect',
                'newClientOrderId',

                # ✅ 原生跟踪委托参数
                'callbackRate',
                'activationPrice',
            ]
            api_params = {k: v for k, v in order_params.items() if k in standard_fields and v is not None}

            # ✅ 市价类订单不要传 timeInForce（Binance 会报参数错误）
            if api_params.get("type") in (
                "MARKET",
                "STOP_MARKET",
                "TAKE_PROFIT_MARKET",
                "TRAILING_STOP_MARKET",
            ):
                api_params.pop("timeInForce", None)

            # Binance 对条件单（STOP / TAKE_PROFIT）在部分模式下不接受 reduceOnly
            # 例如：STOP + reduceOnly 会直接返回 -1106
            if api_params.get("type") in (
                "STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET"
            ):
                api_params.pop("reduceOnly", None)

            # ✅ 原生跟踪委托 TRAILING_STOP_MARKET：清理不允许的字段，并检查 callbackRate
            if api_params.get("type") == "TRAILING_STOP_MARKET":
                # callbackRate: 0.1 ~ 5 (单位 %)，必填
                if "callbackRate" not in api_params or api_params["callbackRate"] is None:
                    raise ValueError("TRAILING_STOP_MARKET 需要 params['callbackRate']（例如 1 表示 1%）")

                # activationPrice 可选；不传则默认以市场价激活
                # trailing stop market 不需要 price / timeInForce / stopPrice
                api_params.pop("price", None)
                api_params.pop("timeInForce", None)
                api_params.pop("stopPrice", None)

                # workingType 可选；不传也行
                # api_params.setdefault("workingType", "CONTRACT_PRICE")

            # ====== ✅ 平全仓止损：用 STOP_MARKET + closePosition=True（不要走 Algo 接口） ======
            tag_u = str(processed_params.get("tag") or "").upper()
            is_sl_like = (("AUTO_" in tag_u or "MANUAL_" in tag_u) and ("SL" in tag_u or "STOP" in tag_u))

            if ("stopPrice" in api_params) and (api_params.get("type") in ["STOP", "STOP_MARKET"]) and is_sl_like:
                # 强制用 STOP_MARKET 做触发平仓
                api_params["type"] = "STOP_MARKET"

                # closePosition=True 表示“平该 positionSide 的全仓”
                api_params["closePosition"] = True

                # closePosition 模式下不要传这些字段
                api_params.pop("quantity", None)
                api_params.pop("price", None)
                api_params.pop("timeInForce", None)

                # 这两个也不要硬塞（避免触发币安参数校验问题）
                api_params.pop("reduceOnly", None)

                logger.info(
                    f"[BINANCE] CREATE_CLOSEPOS_SL | symbol={api_params.get('symbol')} | "
                    f"side={api_params.get('side')} | type={api_params.get('type')} | "
                    f"stopPrice={api_params.get('stopPrice')} | positionSide={api_params.get('positionSide')} | "
                    f"tag={processed_params.get('tag')}"
                )

                order = self.client.futures_create_order(**api_params)
            else:
                order = self.client.futures_create_order(**api_params)

            logger.info(f"币安API返回主订单: {order}")

            # ⭐ 注册 tag 映射
            self._register_tag_mapping(processed_params.get("tag"), order)

            trade_logger.info(
                "CREATE_ORDER | dry_run=0 | symbol=%s | side=%s | type=%s | qty=%s | price=%s | orderId=%s | clientId=%s | clientAlgoId=%s | algoId=%s | tag=%s",
                order.get("symbol"), order.get("side"), order.get("type"),
                order.get("origQty") or order.get("quantity"), order.get("price"),
                order.get("orderId"), order.get("clientOrderId"),
                order.get("clientAlgoId"), order.get("algoId"),
                processed_params.get("tag"),
            )

            # 记录 TP/SL 子单 id
            order['takeProfitOrderId'] = None
            order['stopLossOrderId'] = None

            # ========== 4. 创建止盈订单（如果有配置） ==========
            if take_profit:
                tp_price = take_profit.get('price')
                if tp_price:
                    tp_price_precise = self.price_to_precision(symbol_fmt, tp_price)
                    logger.info(f"准备创建止盈订单，价格: {tp_price_precise}")

                    tp_side = 'SELL' if side_fmt == 'BUY' else 'BUY'

                    try:
                        logger.info(
                            f"[BINANCE] CREATE_TP_ORDER | symbol={symbol_fmt} | side={tp_side} | "
                            f"qty={quantity_precise} | price={tp_price_precise} | stop={tp_price_precise} | "
                            f"pos_side={order_params.get('positionSide')}"
                        )                       
                        tp_order = self.client.futures_create_order(
                            symbol=symbol_fmt,
                            side=tp_side,
                            type='TAKE_PROFIT',
                            quantity=quantity_precise,
                            price=tp_price_precise,
                            stopPrice=tp_price_precise,
                            timeInForce='GTC',
                            positionSide=order_params['positionSide'],
                            # 不传 reduceOnly，避免 -1106
                        )
                        logger.info(f"币安API返回止盈订单: {tp_order}")
                        order['takeProfitOrderId'] = tp_order.get('orderId')
                    except Exception as e:
                        logger.error(f"创建止盈订单失败: {e}")

            # ========== 5. 创建止损订单（如果有配置） ==========
            if stop_loss:
                sl_price = stop_loss.get('price')
                if sl_price:
                    sl_price_precise = self.price_to_precision(symbol_fmt, sl_price)
                    logger.info(f"准备创建止损订单，价格: {sl_price_precise}")

                    # 主单状态：未成交时不要挂 closePosition=True 的 SL（会被 Binance 拒）
                    main_status = str(order.get("status") or "").upper()
                    main_executed = float(order.get("executedQty") or 0.0)

                    # --- 方案A加速：短轮询等主单成交（避免等 WS 很久）---
                    main_checked = self._wait_main_order_fill_quick(
                        symbol_fmt=symbol_fmt,
                        order=order,
                        max_wait_sec=float(processed_params.get("sl_wait_fill_sec", 2.0)),
                        interval_sec=float(processed_params.get("sl_wait_fill_interval_sec", 0.2)),
                    )
                    order = main_checked  # 用最新状态覆盖
                    main_status = str(order.get("status") or "").upper()
                    main_executed = float(order.get("executedQty") or 0.0)

                    # ✅ 必须使用 Binance 回包里的 clientOrderId（WS 的 o.c 就是这个）
                    main_client_id = str(order.get("clientOrderId") or "")
 
                    # 记录入场方向（BUY/SELL）
                    entry_side = side_fmt  # 主单方向（BUY/SELL）

                    if main_status != "FILLED" and main_executed <= 0:
                        # ⭐ 方案A：主单未成交，只有在 clientOrderId 存在时才 defer
                        if not main_client_id:
                            logger.warning(
                                "[BINANCE] cannot defer SL: main order missing clientOrderId"
                            )
                        else:
                            self._defer_stop_loss_until_filled(
                                main_client_order_id=main_client_id,
                                symbol_fmt=symbol_fmt,
                                position_side=order_params["positionSide"],
                                sl_stop_price_precise=str(sl_price_precise),
                                entry_side=entry_side,
                                tag=processed_params.get("tag"),
                            )

                    else:
                        # ✅ 主单已成交（或至少有成交），可以直接挂 SL
                        sl_side = 'SELL' if side_fmt == 'BUY' else 'BUY'

                        try:
                            logger.info(
                                f"[BINANCE] CREATE_SL_ORDER | symbol={symbol_fmt} | side={sl_side} | "
                                f"stop={sl_price_precise} | pos_side={order_params.get('positionSide')}"
                            )
                            sl_order = self.client.futures_create_order(
                                symbol=symbol_fmt,
                                side=sl_side,
                                type="STOP_MARKET",
                                stopPrice=sl_price_precise,
                                closePosition=True,
                                positionSide=order_params["positionSide"],
                            )

                            logger.info(f"币安API返回止损订单: {sl_order}")
                            order["stopLossOrderId"] = sl_order.get("orderId")

                        except Exception as e:
                            logger.error(f"创建止损订单失败: {e}")

            # ========== 6. 给主订单挂上 tag（方便上层识别 A1/A2） ==========
            if params and 'tag' in params:
                order['tag'] = params['tag']

            return order

        except Exception as e:
            logger.error(f"创建订单失败: {e}")
            # 只在调试模式下记录详细错误栈
            if logger.isEnabledFor(logging.DEBUG):
                import traceback
                logger.error(f"错误栈: {traceback.format_exc()}")
            # 只有在 dry_run 模式下才返回模拟数据，否则抛出异常
            if self.dry_run:
                symbol_fmt = self._format_symbol(symbol)
                side_fmt = self._format_side(side)
                type_fmt = self._format_order_type(order_type)

                order = {
                    'orderId': str(int(time.time() * 1000)),
                    'symbol': symbol_fmt,
                    'status': 'NEW',
                    'clientOrderId': f'dry_{int(time.time() * 1000)}',
                    'price': str(price or 0.0),
                    'avgPrice': '0.0',
                    'origQty': '0.01',
                    'executedQty': '0.0',
                    'cumQuote': '0.0',
                    'timeInForce': 'GTC',
                    'type': type_fmt,
                    'reduceOnly': False,
                    'closePosition': False,
                    'side': side_fmt,
                    'positionSide': 'BOTH',
                    'stopPrice': '0.0',
                    'workingType': 'CONTRACT_PRICE',
                    'priceProtect': False,
                    'origType': type_fmt,
                    'time': int(time.time() * 1000),
                    'updateTime': int(time.time() * 1000)
                }
                if params and 'tag' in params:
                    order['tag'] = params['tag']
                return order
            else:
                # 在非 dry_run 模式下，抛出异常，让上层处理（比如重试主单）
                raise

    def cancel_order(self, order_id: str, symbol: Optional[str] = None) -> bool:
        """
        取消订单

        Args:
            order_id: 订单ID（可以是数字 orderId 或字符串 clientOrderId）
            symbol: 交易对 (必填；币安合约撤单需要 symbol)

        Returns:
            是否取消成功
        """
        try:
            if self.dry_run:
                logger.info(f"[DRY RUN] 取消订单成功: {order_id}")
                trade_logger.info(
                    "CANCEL_ORDER | dry_run=1 | order_id=%s | symbol=%s",
                    order_id, symbol
                )
                return True

            if not symbol:
                raise Exception("取消订单必须提供交易对")

            symbol_fmt = self._format_symbol(symbol)

            # order_id 可能是纯数字 orderId，也可能是字符串 clientOrderId
            result = None
            try:
                oid = int(order_id)
                logger.info(f"尝试按 orderId 撤单: symbol={symbol_fmt}, orderId={oid}")
                result = self.client.futures_cancel_order(symbol=symbol_fmt, orderId=oid)
            except (ValueError, TypeError):
                logger.info(
                    f"order_id 不是纯数字，按 origClientOrderId 撤单: "
                    f"symbol={symbol_fmt}, origClientOrderId={order_id}"
                )
                result = self.client.futures_cancel_order(
                    symbol=symbol_fmt,
                    origClientOrderId=str(order_id),
                )

            ok = bool(result)
            logger.info(f"取消订单返回结果: {result}")

            if ok:
                trade_logger.info(
                    "CANCEL_ORDER | dry_run=0 | order_id=%s | symbol=%s",
                    order_id, symbol_fmt
                )
            return ok

        except Exception as e:
            logger.error(f"取消订单失败: {e}")
            return False

    def cancel_all_orders(self, symbol: Optional[str] = None, side: Optional[str] = None) -> bool:
        """
        取消所有订单
        
        Args:
            symbol: 交易对 (可选，默认取消所有)
            side: 订单方向 (可选，默认取消所有方向)
            
        Returns:
            是否取消成功
        """
        try:
            if self.dry_run:
                # 模拟取消所有订单
                symbol_str = symbol if symbol else "所有交易对"
                side_str = side if side else "所有方向"
                logger.info(f"[DRY RUN] 取消{symbol_str} {side_str}所有订单成功")
                return True
            
            if not symbol:
                raise Exception("取消所有订单必须提供交易对")
            
            symbol = self._format_symbol(symbol)
            
            if side:
                # 如果指定了方向，先获取所有订单，再过滤取消
                orders = self.get_open_orders(symbol)
                if not orders:
                    logger.warning("⚠️ 获取未成交订单失败或为空，无法按方向逐个取消")
                    return False
                success_count = 0
                for order in orders:
                    if order['side'] == self._format_side(side):
                        if self.cancel_order(order['orderId'], symbol):
                            success_count += 1
                logger.info(f"取消{symbol} {side}方向订单成功: {success_count}/{len(orders)}个订单")
                return success_count > 0
            else:
                # 如果没有指定方向，直接取消所有订单
                result = self.client.futures_cancel_all_open_orders(symbol=symbol)
                logger.info(f"取消所有订单成功: {result}")
                return True
        except Exception as e:
            logger.error(f"取消所有订单失败: {e}")
            return False
