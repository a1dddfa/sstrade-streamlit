# -*- coding: utf-8 -*-
"""
Auto-split from the original binance_exchange.py.
"""
import uuid
from .deps import (
    logger, trade_logger,
    time, threading, os, json,
    Decimal, ROUND_DOWN,
    Dict, List, Optional, Any, Callable,
    Client, BinanceAPIException, ThreadedWebsocketManager,
)


class PersistenceMixin:
    # =========================
    # ⭐ Local-trigger orders (方案B)
    # =========================
    def _load_pending_local_trigger_orders(self) -> None:
        """从本地 JSON 恢复 pending 本地触发订单（进程重启不丢）。"""
        try:
            path = getattr(self, "_pending_local_trigger_path", None)
            if not path or (not os.path.exists(path)):
                return
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            if isinstance(data, dict) and "data" in data and isinstance(data["data"], dict):
                data = data["data"]
            if not isinstance(data, dict):
                logger.warning(f"[PENDING_LTR] invalid json structure, ignore: {path}")
                return
            with self._ws_lock:
                self._pending_local_trigger_orders = data
            logger.info(f"[PENDING_LTR] loaded: {len(data)} items from {path}")
        except Exception as e:
            logger.warning(f"[PENDING_LTR] load failed, ignore: {e}", exc_info=True)

    def _save_pending_local_trigger_orders(self) -> None:
        """把 pending 本地触发订单落盘到 JSON（尽量原子写入）。"""
        try:
            path = getattr(self, "_pending_local_trigger_path", None)
            if not path:
                return
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with self._ws_lock:
                data = dict(self._pending_local_trigger_orders)
            tmp_path = f"{path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
            logger.info(f"[PENDING_LTR] saved: {len(data)} items to {path}")
        except Exception as e:
            logger.warning(f"[PENDING_LTR] save failed, ignore: {e}", exc_info=True)

    def get_pending_local_trigger_orders(self):
        """
        给 UI 用：返回当前"还活跃/待触发"的本地触发订单列表。
        只返回 triggerStatus == PENDING 的条目。
        """
        out = []
        with self._ws_lock:
            for k, v in (self._pending_local_trigger_orders or {}).items():
                try:
                    st = str(v.get("triggerStatus") or "").upper()
                except Exception:
                    st = ""
                if st != "PENDING":
                    continue
                row = dict(v)
                row.setdefault("id", k)
                out.append(row)
        return out

    def get_local_trigger_order_history(self):
        """
        给 UI 或后续功能用：返回已结束的本地触发订单历史列表。
        返回 triggerStatus != PENDING 的条目，并按时间倒序排序。
        """
        out = []
        with self._ws_lock:
            for k, v in (self._pending_local_trigger_orders or {}).items():
                try:
                    st = str(v.get("triggerStatus") or "").upper()
                except Exception:
                    st = ""
                if st == "PENDING":
                    continue
                row = dict(v)
                row.setdefault("id", k)
                out.append(row)
        # 按时间倒序排序：优先用 triggeredTs，其次用 createdTs
        def get_sort_key(item):
            try:
                # 优先用触发时间
                ts = item.get("triggeredTs")
                if ts:
                    return -ts
                # 其次用创建时间
                ts = item.get("createdTs")
                if ts:
                    return -ts
            except Exception:
                pass
            return 0
        out.sort(key=get_sort_key)
        return out

    def cancel_local_trigger_order(self, oid: str) -> bool:
        """
        取消本地触发订单：标记为 CANCELLED 并落盘。
        取消后不会再被 _try_trigger_local_trigger_orders 处理（它只处理 triggerStatus=PENDING）。
        """
        if not oid:
            return False

        with self._ws_lock:
            cur = (self._pending_local_trigger_orders or {}).get(oid)
            if not cur:
                return False

            cur["triggerStatus"] = "CANCELLED"
            cur["triggerResult"] = "CANCELLED"
            cur["orderStatus"] = "CANCELLED"
            cur["orderError"] = None
            cur["triggerError"] = None
            cur["cancelledTs"] = int(time.time() * 1000)
            self._pending_local_trigger_orders[oid] = cur

        self._save_pending_local_trigger_orders()
        return True

    def clear_finished_local_trigger_orders(self) -> int:
        """
        清除已结束(不再活跃)的本地触发单：
        - triggerStatus != PENDING 的都视为已结束（例如 TRIGGERED 后成功/失败）
        返回清除数量
        """
        removed = 0
        with self._ws_lock:
            keys = [
                k for k, v in (self._pending_local_trigger_orders or {}).items()
                if str(v.get("triggerStatus", "PENDING")).upper() != "PENDING"
            ]
            for k in keys:
                self._pending_local_trigger_orders.pop(k, None)
                removed += 1
        if removed:
            self._save_pending_local_trigger_orders()
        return removed

    def schedule_local_trigger_order(
        self,
        *,
        symbol: str,
        activate_price: float,
        activate_condition: str,
        order_request: dict,
        tag: str = "LOCAL_TRIGGER",
    ) -> dict:
        """
        方案B：本地触发价满足后，才把真实订单提交到交易所。
        """
        sym = self._format_symbol(symbol)
        cond = str(activate_condition or "").lower().strip()
        if cond not in ("gte", "lte"):
            raise ValueError("activate_condition must be gte/lte")

        oid = f"{tag}_{int(time.time()*1000)}_{uuid.uuid4().hex[:8]}"
        cfg = {
            "symbol": sym,
            "activatePrice": float(activate_price),
            "activateCondition": cond,
            "orderRequest": dict(order_request or {}),
            "tag": str(tag),
            "createdTs": int(time.time() * 1000),

            # =========================
            # UI 可观测状态字段（本地触发单）
            # =========================
            # 是否触发
            "triggerStatus": "PENDING",        # PENDING | TRIGGERED
            # 触发后执行结果
            "triggerResult": "PENDING",        # PENDING | SUCCESS | FAILED
            "triggerError": None,
            "triggeredTs": None,

            # 真实订单提交状态
            "orderStatus": "NONE",             # NONE | SUBMITTING | SUBMITTED | FAILED
            "orderError": None,
            "orderResult": None,
            "orderId": None,
            "clientOrderId": None,
            "submittedTs": None,
        }
        with self._ws_lock:
            self._pending_local_trigger_orders[oid] = cfg
        self._save_pending_local_trigger_orders()
        return {"status": "LOCAL_DEFERRED", "id": oid, **cfg}

    def _try_trigger_local_trigger_orders(self, symbol_fmt: str, last_price: float) -> None:
        """轮询触发：满足 activatePrice 条件后，提交真实订单到交易所。"""
        if not symbol_fmt or last_price <= 0:
            return
        
        # 1️⃣ 只处理“还没触发、也没成功下单”的订单
        with self._ws_lock:
            items = [
                (k, v) for k, v in (self._pending_local_trigger_orders or {}).items()
                if v.get("symbol") == symbol_fmt
                and v.get("triggerStatus", "PENDING") == "PENDING"
                and v.get("orderStatus", "NONE") in ("NONE", "FAILED")
            ]
        if not items:
            return

        triggered = []
        for k, cfg in items:
            act = float(cfg.get("activatePrice") or 0.0)
            cond = str(cfg.get("activateCondition") or "").lower().strip()
            if (last_price >= act) if cond == "gte" else (last_price <= act):
                triggered.append((k, cfg))

        for k, cfg in triggered:
            now_ms = int(time.time() * 1000)
            try:
                # 2️⃣ 命中触发价 → 先落库 TRIGGERED 状态（防重入）
                with self._ws_lock:
                    cur = self._pending_local_trigger_orders.get(k)
                    if not cur:
                        continue
                    cur["triggerStatus"] = "TRIGGERED"
                    cur["triggerResult"] = "PENDING"
                    cur["triggeredTs"] = now_ms
                    cur["orderStatus"] = "SUBMITTING"
                    cur["submittedTs"] = now_ms
                    self._pending_local_trigger_orders[k] = cur
                self._save_pending_local_trigger_orders()

                req = dict(cfg.get("orderRequest") or {})
                p = dict(req.get("params") or {})
                p["_skip_local_trigger"] = True
                req["params"] = p
                
                # 提交真实订单
                o = self.create_order(
                    symbol=req.get("symbol") or symbol_fmt,
                    side=req["side"],
                    order_type=req["order_type"],
                    quantity=float(req.get("quantity") or 0.0),
                    price=req.get("price"),
                    params=req.get("params"),
                )

                # 3️⃣ create_order 成功 → 写入真实订单回执
                with self._ws_lock:
                    cur = self._pending_local_trigger_orders.get(k)
                    if not cur:
                        continue
                    cur["triggerResult"] = "SUCCESS"
                    cur["orderStatus"] = "SUBMITTED"
                    cur["orderResult"] = o
                    cur["orderId"] = o.get("orderId")
                    cur["clientOrderId"] = o.get("clientOrderId")
                    self._pending_local_trigger_orders[k] = cur
                self._save_pending_local_trigger_orders()
                
            except Exception as e:
                # 4️⃣ create_order 失败 → 状态可见，不吞单
                logger.warning(f"[LOCAL_TRIGGER] trigger failed: {e}", exc_info=True)
                with self._ws_lock:
                    cur = self._pending_local_trigger_orders.get(k)
                    if not cur:
                        continue
                    cur["triggerResult"] = "FAILED"
                    cur["triggerError"] = repr(e)
                    cur["orderStatus"] = "FAILED"
                    cur["orderError"] = repr(e)
                    self._pending_local_trigger_orders[k] = cur
                self._save_pending_local_trigger_orders()

    def poll_local_triggers_once(self) -> None:
        """给轮询线程用：ticker 拉取 + 本地触发"""
        symbols = set()
        with self._ws_lock:
            for v in (self._pending_deferred_stop_limits or {}).values():
                if v.get("symbol"):
                    symbols.add(v["symbol"])
            # ✅ 只轮询"待触发"的本地触发单（triggerStatus == PENDING）
            # 历史记录（TRIGGERED/FAILED/CANCELLED...）不应再驱动行情拉取
            for v in (self._pending_local_trigger_orders or {}).values():
                try:
                    st = str(v.get("triggerStatus") or "").upper()
                except Exception:
                    st = ""
                if st != "PENDING":
                    continue
                if v.get("symbol"):
                    symbols.add(v["symbol"])
        for sym in symbols:
            t = self.get_ticker(sym) or {}
            px = float(t.get("lastPrice") or t.get("markPrice") or 0.0)
            if px > 0:
                self._try_trigger_deferred_stop_limits(sym, px)
                self._try_trigger_local_trigger_orders(sym, px)

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
