# -*- coding: utf-8 -*-
"""
Auto-split from the original binance_exchange.py.
"""
from .deps import (
    logger, trade_logger,
    time, threading, os, json,
    Decimal, ROUND_DOWN,
    Dict, List, Optional, Any, Callable,
    Client, BinanceAPIException, ThreadedWebsocketManager,
)


class PersistenceMixin:
    def _load_pending_stop_losses(self) -> None:
        """从本地 JSON 恢复 pending SL（进程重启不丢）。"""
        try:
            path = getattr(self, "_pending_sl_path", None)
            if not path:
                return

            if not os.path.exists(path):
                return

            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}

            # 允许两种结构：dict 或 {"data": dict}
            if isinstance(data, dict) and "data" in data and isinstance(data["data"], dict):
                data = data["data"]

            if not isinstance(data, dict):
                logger.warning(f"[PENDING_SL] invalid json structure, ignore: {path}")
                return

            with self._ws_lock:
                self._pending_stop_losses = data

            logger.info(f"[PENDING_SL] loaded: {len(data)} items from {path}")

        except Exception as e:
            logger.warning(f"[PENDING_SL] load failed, ignore: {e}", exc_info=True)

    def _save_pending_stop_losses(self) -> None:
        """把 pending SL 落盘到 JSON（尽量原子写入，避免写一半）。"""
        try:
            path = getattr(self, "_pending_sl_path", None)
            if not path:
                return

            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

            with self._ws_lock:
                data = dict(self._pending_stop_losses)

            tmp_path = f"{path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            os.replace(tmp_path, path)
            logger.info(f"[PENDING_SL] saved: {len(data)} items to {path}")

        except Exception as e:
            logger.warning(f"[PENDING_SL] save failed, ignore: {e}", exc_info=True)

    def get_pending_stop_losses(self) -> List[Dict[str, Any]]:
        """
        给 UI/上层读取待补挂止损列表（线程安全）。
        返回 list[dict]，每个元素带上 mainClientOrderId，便于排查。
        """
        with self._ws_lock:
            return [{"mainClientOrderId": k, **v} for k, v in self._pending_stop_losses.items()]

    def _load_pending_deferred_stop_limits(self) -> None:
        """从本地 JSON 恢复 pending deferred STOP_LIMIT（进程重启不丢）。"""
        try:
            path = getattr(self, "_pending_deferred_sl_path", None)
            if not path:
                return
            if not os.path.exists(path):
                return

            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}

            # 允许两种结构：dict 或 {"data": dict}
            if isinstance(data, dict) and "data" in data and isinstance(data["data"], dict):
                data = data["data"]

            if not isinstance(data, dict):
                logger.warning(f"[PENDING_DSL] invalid json structure, ignore: {path}")
                return

            with self._ws_lock:
                self._pending_deferred_stop_limits = data

            logger.info(f"[PENDING_DSL] loaded: {len(data)} items from {path}")
        except Exception as e:
            logger.warning(f"[PENDING_DSL] load failed, ignore: {e}", exc_info=True)

    def _save_pending_deferred_stop_limits(self) -> None:
        """把 pending deferred STOP_LIMIT 落盘到 JSON（尽量原子写入，避免写一半）。"""
        try:
            path = getattr(self, "_pending_deferred_sl_path", None)
            if not path:
                return

            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

            with self._ws_lock:
                data = dict(self._pending_deferred_stop_limits)

            tmp_path = f"{path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            os.replace(tmp_path, path)
            logger.info(f"[PENDING_DSL] saved: {len(data)} items to {path}")
        except Exception as e:
            logger.warning(f"[PENDING_DSL] save failed, ignore: {e}", exc_info=True)

    def get_pending_deferred_stop_limits(self) -> List[Dict[str, Any]]:
        """给 UI 用：返回当前待触发的 deferred STOP_LIMIT 列表。"""
        with self._ws_lock:
            items = list((self._pending_deferred_stop_limits or {}).values())
        # 方便 UI 展示：补一个 id 字段
        out = []
        with self._ws_lock:
            for k, v in (self._pending_deferred_stop_limits or {}).items():
                row = dict(v)
                row.setdefault("id", k)
                out.append(row)
        return out

    def schedule_deferred_stop_limit(
        self,
        *,
        symbol: str,
        position_side: str,
        close_side: str,
        activate_price: float,
        stop_price: float,
        limit_price: float,
        quantity: Optional[float] = None,
        tag: str = "DEFER_STOPLIMIT",
        activate_condition: Optional[str] = None,
    ) -> Dict[str, Any]:
        """两段式 STOP_LIMIT：先“等价格到 activate_price”，再下真正 STOP_LIMIT。

        典型用法：锁盈/保本。
        - LONG 仓：价格 >= activate_price 后，挂 SELL 的 STOP_LIMIT（stopPrice/limitPrice）
        - SHORT 仓：价格 <= activate_price 后，挂 BUY 的 STOP_LIMIT

        Args:
            symbol: 交易对
            position_side: "LONG" / "SHORT"
            close_side: "long" / "short"（沿用本项目 create_order 的 side 语义）
            activate_price: 触发挂单的“第一段”价格
            stop_price: STOP_LIMIT 的 stopPrice（第二段触发价）
            limit_price: STOP_LIMIT 的 price（第二段挂出的限价）
            quantity: 可选；不填时会在触发时用 WS 持仓获取当前真实仓位量
            tag: 业务 tag，会写进 clientOrderId 里
            activate_condition: "gte" / "lte"；不填则根据 position_side 自动推断

        Returns:
            一个 dict：status=DEFERRED + 配置回显
        """
        sym = self._format_symbol(symbol)
        ps = str(position_side).upper()
        if ps not in ("LONG", "SHORT"):
            raise ValueError("position_side must be LONG/SHORT")

        cond = (activate_condition or "").lower().strip() or ("gte" if ps == "LONG" else "lte")
        if cond not in ("gte", "lte"):
            raise ValueError("activate_condition must be gte/lte")

        # key 用时间戳+tag，避免重复覆盖
        key = f"{tag}_{int(time.time()*1000)}"
        cfg = {
            "symbol": sym,
            "positionSide": ps,
            "closeSide": str(close_side),
            "activatePrice": float(activate_price),
            "activateCondition": cond,
            "stopPrice": float(stop_price),
            "limitPrice": float(limit_price),
            "quantity": float(quantity) if quantity is not None else None,
            "tag": str(tag),
            "createdTs": int(time.time() * 1000),
        }

        with self._ws_lock:
            self._pending_deferred_stop_limits[key] = cfg
        self._save_pending_deferred_stop_limits()

        # ✅ 确保 ticker WS 在跑：没有其他订阅者时也能触发
        try:
            if not self.dry_run:
                if sym not in self._internal_deferred_ticker_cbs:
                    def _cb(_t):
                        # 不做任何事；真正的触发检查在 _handle_ws_ticker_message 里统一做
                        return
                    self._internal_deferred_ticker_cbs[sym] = _cb
                    self.ws_subscribe_ticker(sym, _cb)
        except Exception:
            # 订阅失败不影响注册（后续可依赖外部 bot 的行情订阅触发）
            logger.warning(f"[PENDING_DSL] failed to ensure ticker ws for {sym}", exc_info=True)

        return {"status": "DEFERRED", "id": key, **cfg}

    def _try_trigger_deferred_stop_limits(self, symbol_fmt: str, last_price: float) -> None:
        """被行情 WS 调用：当价格满足 activatePrice 条件时，创建真正的 STOP_LIMIT。"""
        if not symbol_fmt or last_price <= 0:
            return

        # 取出该 symbol 的所有待触发条目
        with self._ws_lock:
            items = [(k, v) for k, v in (self._pending_deferred_stop_limits or {}).items() if v.get("symbol") == symbol_fmt]

        if not items:
            return

        triggered = []
        for k, cfg in items:
            try:
                act = float(cfg.get("activatePrice") or 0.0)
                cond = str(cfg.get("activateCondition") or "").lower().strip()
                ok = (last_price >= act) if cond == "gte" else (last_price <= act)
                if ok:
                    triggered.append((k, cfg))
            except Exception:
                continue

        if not triggered:
            return

        for k, cfg in triggered:
            try:
                ps = str(cfg.get("positionSide") or "").upper()
                close_side = str(cfg.get("closeSide") or "")
                stop_price = float(cfg.get("stopPrice") or 0.0)
                limit_price = float(cfg.get("limitPrice") or 0.0)
                tag = str(cfg.get("tag") or "DEFER_STOPLIMIT")

                # ✅ 数量：优先用触发时的真实持仓（更安全）
                qty = None
                try:
                    q0 = cfg.get("quantity")
                    if q0 is not None and float(q0) > 0:
                        qty = float(q0)
                except Exception:
                    qty = None

                if qty is None:
                    # 从 WS positions 里找 symbol+positionSide 的仓位量
                    with self._ws_lock:
                        p = self._ws_positions.get(symbol_fmt) or {}
                    # p 可能是单条，也可能没区分 ps；因此 fallback 用 REST positions
                    amt = 0.0
                    try:
                        if p:
                            # futures user-stream position 里一般是 positionAmt
                            if str(p.get("positionSide") or "").upper() == ps or not p.get("positionSide"):
                                amt = float(p.get("positionAmt") or 0.0)
                    except Exception:
                        amt = 0.0

                    if amt == 0.0:
                        try:
                            pos_list = self.get_positions(symbol_fmt) or []
                            for pp in pos_list:
                                if str(pp.get("symbol") or "").upper() != symbol_fmt:
                                    continue
                                if ps and str(pp.get("positionSide") or "").upper() != ps:
                                    continue
                                amt = float(pp.get("positionAmt") or 0.0)
                                if amt != 0.0:
                                    break
                        except Exception:
                            amt = 0.0

                    qty = abs(float(amt)) if amt else 0.0

                if qty <= 0:
                    logger.warning(f"[PENDING_DSL] triggered but qty=0, skip: symbol={symbol_fmt} id={k}")
                    # 不删，等待下一次（可能 WS/REST 还没同步上仓位）
                    continue

                # 下真正 STOP_LIMIT（第二段）
                o = self.create_order(
                    symbol=symbol_fmt,
                    side=close_side,
                    order_type="stop_limit",
                    quantity=float(qty),
                    price=float(limit_price),
                    params={
                        "reduceOnly": True,
                        "timeInForce": "GTC",
                        "stopPrice": float(stop_price),
                        "positionSide": ps,
                        "tag": f"{tag}_ARMED",
                    },
                )

                logger.info(
                    f"[PENDING_DSL] armed -> placed STOP_LIMIT: symbol={symbol_fmt} id={k} last={last_price} orderId={getattr(o, 'get', lambda _:'')('orderId') if isinstance(o, dict) else ''}"
                )

                # 成功后移除
                with self._ws_lock:
                    self._pending_deferred_stop_limits.pop(k, None)
                self._save_pending_deferred_stop_limits()

            except Exception as e:
                logger.warning(f"[PENDING_DSL] trigger placement failed (will retry): {e}", exc_info=True)
