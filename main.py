from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit

import qrcode
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
from fastapi import Request
from starlette.requests import ClientDisconnect

from .alipay_gateway import AlipayCredentials, AlipayGateway, response_body
from .public_routes import PublicRouteRegistrar, alipay_ack, page, payment_form_page
from .reminder import inject_payment_reminder
from .resource_limits import (
    AsyncCapacity,
    RequestBodyRejected,
    SlidingWindowRateLimiter,
    parse_unique_form_body,
    read_limited_body,
)
from .storage import (
    Order,
    OrderCreationLimits,
    OrderLimitExceeded,
    OrderStore,
    QueryLimitExceeded,
)

PLUGIN_NAME = "astrbot_plugin_alipay_website"
ORDER_NUMBER_RE = re.compile(r"^AIP\d{14}[0-9A-F]{12}$")
PAID_STATUSES = {"TRADE_SUCCESS", "TRADE_FINISHED"}
MAINTENANCE_INTERVAL_SECONDS = 300
TERMINAL_RETENTION_DAYS = 30
NOTIFY_BODY_LIMIT_BYTES = 64 * 1024
NOTIFY_MAX_FIELDS = 100
NOTIFY_READ_TIMEOUT_SECONDS = 5
NOTIFY_MAX_CONCURRENT = 16
RETURN_QUERY_LIMIT_BYTES = 8 * 1024
RETURN_MAX_FIELDS = 100
RETURN_MAX_CONCURRENT = 16
RETURN_GLOBAL_PER_MINUTE = 240
CREATE_MAX_CONCURRENT = 4
QR_MAX_CONCURRENT = 2
VERIFY_PER_SESSION_PER_MINUTE = 12
QUERY_GLOBAL_PER_MINUTE = 120
QUERY_MIN_INTERVAL_SECONDS = 5
QUERY_MAX_ATTEMPTS_PER_ORDER = 60
GATEWAY_MAX_CONCURRENT = 8
QUERY_STATUSES = {"WAIT_BUYER_PAY", "TRADE_CLOSED", *PAID_STATUSES}
CREATE_LIMITS = OrderCreationLimits(
    window_seconds=60,
    max_recent_per_session=3,
    max_recent_global=30,
    max_active_per_session=3,
    max_active_global=500,
    max_retained_global=10_000,
)


@register(
    PLUGIN_NAME,
    "xiaoheiCat",
    "为 AstrBot Agent 提供支付宝 AI 网页应用收款工具",
    "1.0.2",
)
class AlipayWebsitePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context, config)
        self.config = config
        data_dir = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
        self.store = OrderStore(data_dir / "orders.sqlite3")
        self.qr_dir = data_dir / "qrcodes"
        self.routes = PublicRouteRegistrar()
        self._route_task: asyncio.Task | None = None
        self._maintenance_task: asyncio.Task | None = None
        self._notify_capacity = AsyncCapacity(NOTIFY_MAX_CONCURRENT)
        self._return_capacity = AsyncCapacity(RETURN_MAX_CONCURRENT)
        self._create_capacity = AsyncCapacity(CREATE_MAX_CONCURRENT)
        self._qr_semaphore = asyncio.Semaphore(QR_MAX_CONCURRENT)
        self._gateway_capacity = AsyncCapacity(GATEWAY_MAX_CONCURRENT)
        self._verify_rate = SlidingWindowRateLimiter(
            VERIFY_PER_SESSION_PER_MINUTE, 60
        )
        self._query_rate = SlidingWindowRateLimiter(QUERY_GLOBAL_PER_MINUTE, 60)
        self._return_rate = SlidingWindowRateLimiter(RETURN_GLOBAL_PER_MINUTE, 60)
        self._query_inflight: set[str] = set()
        self._query_inflight_guard = asyncio.Lock()

    async def initialize(self) -> None:
        await self.store.initialize()
        self.qr_dir.mkdir(parents=True, exist_ok=True)
        self.context.add_llm_tools(
            FunctionTool(
                name="create_alipay_bill",
                description=(
                    "创建人民币支付宝收款订单。金额必须为 0.01 到 50.00 元；创建后会先发送 message，"
                    "再发送支付二维码。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "cny": {
                            "type": "number",
                            "minimum": 0.01,
                            "maximum": 50.0,
                            "multipleOf": 0.01,
                            "description": "人民币金额，最多两位小数",
                        },
                        "message": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 200,
                            "description": "发送二维码前先发给用户的一句话消息",
                        },
                    },
                    "required": ["cny", "message"],
                    "additionalProperties": False,
                },
                handler=self.create_alipay_bill,
            ),
            FunctionTool(
                name="verify_alipay_bill",
                description="向支付宝服务端复核当前会话创建的订单是否付款成功。",
                parameters={
                    "type": "object",
                    "properties": {
                        "out_trade_no": {
                            "type": "string",
                            "pattern": ORDER_NUMBER_RE.pattern,
                            "description": "create_alipay_bill 返回的商户订单号",
                        }
                    },
                    "required": ["out_trade_no"],
                    "additionalProperties": False,
                },
                handler=self.verify_alipay_bill,
            ),
        )
        # AstrBot 在插件 initialize() 结束后才构造 Dashboard；不可在这里同步等待 APP。
        self._route_task = asyncio.create_task(
            self._register_public_routes(), name="alipay-public-route-registration"
        )
        self._maintenance_task = asyncio.create_task(
            self._maintenance_loop(), name="alipay-order-maintenance"
        )

    def _credentials(self) -> AlipayCredentials:
        return AlipayCredentials.load(self.config)

    def _gateway(self) -> AlipayGateway:
        return AlipayGateway(self._credentials())

    def _ttl_minutes(self) -> int:
        raw = self.config.get("order_ttl_minutes", 15)
        try:
            if isinstance(raw, bool):
                raise ValueError
            if isinstance(raw, str) and not re.fullmatch(r"\d+", raw.strip()):
                raise ValueError
            if isinstance(raw, float) and not raw.is_integer():
                raise ValueError
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("订单有效期必须是 5 到 30 分钟的整数") from exc
        if not 5 <= value <= 30:
            raise ValueError("订单有效期必须是 5 到 30 分钟的整数")
        return value

    def _callback_base(self) -> str:
        raw = str(self.context.get_config().get("callback_api_base", "")).strip()
        parts = urlsplit(raw)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError(
                "请先在 AstrBot 设置中填写有效的“对外可达的回调接口地址”"
            )
        if parts.query or parts.fragment:
            raise ValueError("对外可达的回调接口地址不能包含查询参数或片段")
        credentials = self._credentials()
        if credentials.environment == "production" and parts.scheme != "https":
            raise ValueError("生产环境的对外可达回调接口地址必须使用 HTTPS")
        return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))

    async def create_alipay_bill(
        self, event: AstrMessageEvent, cny: float | str, message: str
    ) -> str:
        if not await self._create_capacity.acquire():
            return "收款服务当前繁忙，请稍后再创建订单。"
        try:
            amount = self._parse_amount(cny)
            message = self._validate_message(message)
            self._gateway()
            base = self._callback_base()
            ttl = self._ttl_minutes()
            now = datetime.now(UTC)
            out_trade_no = (
                "AIP" + now.strftime("%Y%m%d%H%M%S") + secrets.token_hex(6).upper()
            )
            token = secrets.token_urlsafe(32)
            landing_url = f"{base}/alipay?{urlencode({'token': token})}"
            order = Order(
                out_trade_no=out_trade_no,
                session=event.unified_msg_origin,
                amount=format(amount, ".2f"),
                message=message,
                token_hash=self.store.token_hash(token),
                status="CREATED",
                trade_no=None,
                created_at=now.isoformat(),
                expires_at=(now + timedelta(minutes=ttl)).isoformat(),
                paid_at=None,
                reminder_state="pending",
                updated_at=now.isoformat(),
            )
            try:
                await self.store.create(order, CREATE_LIMITS)
            except OrderLimitExceeded as exc:
                return str(exc)
            qr_path = self.qr_dir / f"{out_trade_no}.png"
            async with self._qr_semaphore:
                await asyncio.to_thread(self._write_qr, landing_url, qr_path)
            sent_text = await self.context.send_message(
                event.unified_msg_origin, MessageChain().message(message)
            )
            sent_qr = await self.context.send_message(
                event.unified_msg_origin, MessageChain().file_image(str(qr_path))
            )
            if not sent_text or not sent_qr:
                logger.warning("订单 %s 已创建，但平台消息可能未全部发送成功。", out_trade_no)
            return (
                f"支付宝订单已创建。商户订单号：{out_trade_no}；金额：{order.amount} 元；"
                f"有效期：{ttl} 分钟。消息和二维码已按顺序发送。"
            )
        finally:
            await self._create_capacity.release()

    async def verify_alipay_bill(
        self, event: AstrMessageEvent, out_trade_no: str
    ) -> str:
        if not await self._verify_rate.allow(event.unified_msg_origin):
            return "当前会话查询过于频繁，请稍后再试。"
        if not ORDER_NUMBER_RE.fullmatch(str(out_trade_no)):
            return "商户订单号格式不正确。"
        order = await self.store.get(out_trade_no, event.unified_msg_origin)
        if order is None:
            return "未找到当前会话创建的这笔订单。"
        try:
            status, trade_no = await self._query_and_update(order)
        except QueryLimitExceeded as exc:
            return str(exc)
        if status in PAID_STATUSES:
            return (
                f"支付宝已确认付款成功。商户订单号：{out_trade_no}；"
                f"支付宝交易号：{trade_no or '未返回'}；金额：{order.amount} 元。"
            )
        labels = {
            "WAIT_BUYER_PAY": "等待买家付款",
            "TRADE_CLOSED": "交易已关闭",
            "CREATED": "订单已创建，尚未确认付款",
            "EXPIRED": "订单已过期",
        }
        return f"尚未确认付款成功。商户订单号：{out_trade_no}；状态：{labels.get(status, status)}。"

    @staticmethod
    def _parse_amount(value: float | str) -> Decimal:
        try:
            original = Decimal(str(value))
            if not original.is_finite():
                raise ValueError
            amount = original.quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("cny 必须是有效金额") from exc
        if amount < Decimal("0.01") or amount > Decimal("50.00"):
            raise ValueError("cny 必须在 0.01 到 50.00 之间")
        if original != amount:
            raise ValueError("cny 最多只能有两位小数")
        return amount

    @staticmethod
    def _validate_message(value: str) -> str:
        message = str(value).strip()
        if not message or len(message) > 200 or "\n" in message or "\r" in message:
            raise ValueError("message 必须是一行且不超过 200 个字符")
        return message

    @staticmethod
    def _write_qr(content: str, path: Path) -> None:
        image = qrcode.make(content)
        image.save(path)

    async def _landing_page(self, request: Request):
        token = request.query_params.get("token", "")
        if not token or len(token) > 128:
            return page("链接无效", "<h1>支付链接无效</h1>", status_code=404)
        order = await self.store.get_by_token(token)
        if order is None:
            return page("链接无效", "<h1>支付链接无效</h1>", status_code=404)
        if order.status in PAID_STATUSES or order.paid_at is not None:
            return page("已支付", "<h1>付款已完成</h1><p>现在可以安全的关闭此页面了。</p>")
        expires_at = datetime.fromisoformat(order.expires_at)
        if order.status not in {"CREATED", "WAIT_BUYER_PAY"} or expires_at <= datetime.now(
            UTC
        ):
            return page(
                "已过期",
                "<h1>订单已过期</h1><p>请返回聊天重新创建订单。</p>",
                status_code=410,
            )
        try:
            if not await self._gateway_capacity.acquire():
                return page(
                    "暂时不可用",
                    "<h1>支付请求较多</h1><p>请稍后重试。</p>",
                    status_code=503,
                )
            try:
                gateway = self._gateway()
                base = self._callback_base()
                form = await gateway.page_pay_form(
                    out_trade_no=order.out_trade_no,
                    amount=Decimal(order.amount),
                    subject="AI Bot 收款",
                    expires_at=expires_at,
                    notify_url=f"{base}/alipay/notify",
                    return_url=f"{base}/alipay/return",
                )
            finally:
                await self._gateway_capacity.release()
            current = await self.store.get(order.out_trade_no)
            if current is None or current.status not in {"CREATED", "WAIT_BUYER_PAY"}:
                if current is not None and (
                    current.status in PAID_STATUSES or current.paid_at is not None
                ):
                    return page("已支付", "<h1>付款已完成</h1><p>可以关闭此页面。</p>")
                return page(
                    "已过期",
                    "<h1>订单已过期</h1><p>请返回聊天重新创建订单。</p>",
                    status_code=410,
                )
            if expires_at <= datetime.now(UTC):
                return page(
                    "已过期",
                    "<h1>订单已过期</h1><p>请返回聊天重新创建订单。</p>",
                    status_code=410,
                )
            return payment_form_page(form)
        except Exception:
            logger.exception("生成支付宝支付表单失败，订单号：%s", order.out_trade_no)
            return page(
                "暂时不可用",
                "<h1>暂时无法发起支付</h1><p>请稍后重试。</p>",
                status_code=503,
            )

    async def _notify_callback(self, request: Request):
        if not await self._notify_capacity.acquire():
            return alipay_ack(False)
        try:
            content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
            if content_type != "application/x-www-form-urlencoded":
                return alipay_ack(False)
            try:
                async with asyncio.timeout(NOTIFY_READ_TIMEOUT_SECONDS):
                    body = await read_limited_body(request, NOTIFY_BODY_LIMIT_BYTES)
                params = parse_unique_form_body(body, NOTIFY_MAX_FIELDS)
            except (RequestBodyRejected, ClientDisconnect, TimeoutError):
                return alipay_ack(False)
            try:
                gateway = self._gateway()
                if not gateway.verify_notification(params):
                    return alipay_ack(False)
                out_trade_no = params.get("out_trade_no", "")
                order = await self.store.get(out_trade_no)
                if order is None or not self._notification_matches(order, params, gateway):
                    return alipay_ack(False)
                notify_id = params.get("notify_id") or hashlib.sha256(
                    (params.get("sign", "") + params.get("trade_status", "")).encode()
                ).hexdigest()
                status = params.get("trade_status", "")
                inserted = await self.store.apply_notification(
                    notify_id, out_trade_no, status, params.get("trade_no")
                )
                if inserted and status in PAID_STATUSES:
                    asyncio.create_task(self._deliver_reminder(out_trade_no))
                return alipay_ack(True)
            except Exception:
                logger.exception("处理支付宝异步通知失败")
                return alipay_ack(False)
        finally:
            await self._notify_capacity.release()

    @staticmethod
    def _notification_matches(
        order: Order, params: dict[str, str], gateway: AlipayGateway
    ) -> bool:
        try:
            amount_matches = Decimal(params.get("total_amount", "")) == Decimal(order.amount)
        except InvalidOperation:
            return False
        credentials = gateway.credentials
        return (
            params.get("app_id") == credentials.app_id
            and params.get("out_trade_no") == order.out_trade_no
            and params.get("seller_id") == credentials.seller_id
            and amount_matches
            and not params.get("refund_fee")
            and params.get("trade_status")
            in {"WAIT_BUYER_PAY", "TRADE_CLOSED", *PAID_STATUSES}
        )

    async def _return_page(self, request: Request):
        if not await self._return_capacity.acquire():
            return page(
                "等待确认",
                "<h1>支付结果确认中</h1><p>请返回聊天稍后查询。</p>",
                status_code=503,
            )
        try:
            if not await self._return_rate.allow("global"):
                return page(
                    "等待确认",
                    "<h1>支付结果确认中</h1><p>请返回聊天稍后查询。</p>",
                    status_code=429,
                )
            raw_query = request.scope.get("query_string", b"")
            if not isinstance(raw_query, bytes) or len(raw_query) > RETURN_QUERY_LIMIT_BYTES:
                return page("参数无效", "<h1>支付返回参数无效</h1>", status_code=400)
            try:
                params = parse_unique_form_body(raw_query, RETURN_MAX_FIELDS)
            except RequestBodyRejected:
                return page("参数无效", "<h1>支付返回参数无效</h1>", status_code=400)
            try:
                gateway = self._gateway()
                if not gateway.verify_notification(params):
                    return page(
                        "支付结果",
                        "<h1>正在确认支付结果</h1><p>请返回聊天，由 AI 查询订单状态。</p>",
                    )
                order = await self.store.get(params.get("out_trade_no", ""))
                if order is None or params.get("app_id") != gateway.credentials.app_id:
                    return page("订单不存在", "<h1>未找到订单</h1>", status_code=404)
                try:
                    status, _ = await self._query_and_update(order)
                except QueryLimitExceeded:
                    return page(
                        "等待确认",
                        "<h1>支付结果确认中</h1><p>请返回聊天稍后查询。</p>",
                    )
                if status in PAID_STATUSES:
                    return page(
                        "支付成功",
                        "<h1>付款成功</h1><p>可以关闭此页面并返回聊天。</p>",
                    )
                return page(
                    "等待确认",
                    "<h1>支付结果确认中</h1><p>请返回聊天稍后查询。</p>",
                )
            except Exception:
                logger.exception("处理支付宝同步返回失败")
                return page(
                    "等待确认",
                    "<h1>支付结果确认中</h1><p>请返回聊天稍后查询。</p>",
                )
        finally:
            await self._return_capacity.release()

    async def _query_and_update(self, order: Order) -> tuple[str, str | None]:
        if not await self._claim_query(order.out_trade_no):
            raise QueryLimitExceeded("inflight", "该订单正在查询，请稍后再试。")
        try:
            current = await self.store.get(order.out_trade_no)
            if current is None:
                raise RuntimeError("订单不存在")
            if current.status in PAID_STATUSES or current.paid_at is not None:
                return current.status, current.trade_no
            if not await self._gateway_capacity.acquire():
                raise QueryLimitExceeded("concurrency", "支付宝查询繁忙，请稍后再试。")
            global_reserved = False
            try:
                if not await self._query_rate.allow("global"):
                    raise QueryLimitExceeded("global_rate", "支付宝查询繁忙，请稍后再试。")
                global_reserved = True
                try:
                    await self.store.reserve_query(
                        order.out_trade_no,
                        min_interval_seconds=QUERY_MIN_INTERVAL_SECONDS,
                        max_attempts=QUERY_MAX_ATTEMPTS_PER_ORDER,
                    )
                except QueryLimitExceeded:
                    await self._query_rate.refund("global")
                    global_reserved = False
                    raise
                payload = await self._gateway().query(order.out_trade_no)
            finally:
                await self._gateway_capacity.release()
            if not global_reserved:
                raise RuntimeError("支付宝查询预算状态异常")

            body = response_body(payload, "alipay.trade.query")
            if body.get("code") == "10000":
                if body.get("out_trade_no") != order.out_trade_no:
                    raise RuntimeError("支付宝查询响应订单号不匹配")
                if Decimal(str(body.get("total_amount"))) != Decimal(order.amount):
                    raise RuntimeError("支付宝查询响应金额不匹配")
                queried_status = str(body.get("trade_status") or "")
                if queried_status not in QUERY_STATUSES:
                    raise RuntimeError("支付宝查询响应交易状态无效")
                updated = await self.store.mark_status(
                    order.out_trade_no,
                    queried_status,
                    trade_no=body.get("trade_no"),
                )
                if updated is None:
                    raise RuntimeError("订单不存在")
                if updated.status in PAID_STATUSES or updated.paid_at is not None:
                    asyncio.create_task(self._deliver_reminder(order.out_trade_no))
                return updated.status, updated.trade_no
            sub_code = str(body.get("sub_code") or body.get("code") or "QUERY_FAILED")
            if sub_code == "ACQ.TRADE_NOT_EXIST":
                latest = await self.store.get(order.out_trade_no)
                if latest is None:
                    raise RuntimeError("订单不存在")
                return latest.status, latest.trade_no
            raise RuntimeError(f"支付宝订单查询失败：{sub_code}")
        finally:
            await self._release_query(order.out_trade_no)

    async def _claim_query(self, out_trade_no: str) -> bool:
        async with self._query_inflight_guard:
            if out_trade_no in self._query_inflight:
                return False
            self._query_inflight.add(out_trade_no)
            return True

    async def _release_query(self, out_trade_no: str) -> None:
        async with self._query_inflight_guard:
            self._query_inflight.discard(out_trade_no)

    async def _deliver_reminder(self, out_trade_no: str) -> None:
        mode = str(self.config.get("callback_reminder_mode", "user_message"))
        if mode == "off":
            await self.store.finish_reminder(out_trade_no, True)
            return
        if mode not in {"user_message", "fake_tool_call"}:
            logger.error("未知的支付宝回调提醒模式：%s", mode)
            return
        if not await self.store.claim_reminder(out_trade_no):
            return
        order = await self.store.get(out_trade_no)
        success = False
        try:
            if order is None:
                return
            await inject_payment_reminder(self.context, order.session, out_trade_no, mode)
            success = True
        except Exception:
            logger.exception("注入支付宝付款提醒失败，订单号：%s", out_trade_no)
        finally:
            await self.store.finish_reminder(out_trade_no, success)

    async def _maintenance_loop(self) -> None:
        while True:
            try:
                await self._run_maintenance()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("支付宝订单定期维护失败")
            await asyncio.sleep(MAINTENANCE_INTERVAL_SECONDS)

    async def _run_maintenance(self) -> None:
        for order in await self.store.maintenance_candidates():
            if order.status in PAID_STATUSES:
                await self._deliver_reminder(order.out_trade_no)
                continue
            try:
                status, _ = await self._query_and_update(order)
                if status == "WAIT_BUYER_PAY":
                    if not await self._gateway_capacity.acquire():
                        continue
                    try:
                        body = response_body(
                            await self._gateway().close(order.out_trade_no),
                            "alipay.trade.close",
                        )
                    finally:
                        await self._gateway_capacity.release()
                    if body.get("code") == "10000":
                        await self.store.mark_status(order.out_trade_no, "CLOSED")
                elif status == "CREATED":
                    await self.store.mark_status(order.out_trade_no, "EXPIRED")
            except QueryLimitExceeded as exc:
                if exc.code == "attempts":
                    await self.store.mark_status(order.out_trade_no, "EXPIRED")
                continue
            except Exception:
                logger.warning("维护过期订单 %s 时查询失败，将在下轮重试。", order.out_trade_no)
        cutoff = (datetime.now(UTC) - timedelta(days=TERMINAL_RETENTION_DAYS)).isoformat()
        await self.store.purge_before(cutoff)
        for qr_path in self.qr_dir.glob("*.png"):
            if await self.store.get(qr_path.stem) is None:
                with suppress(OSError):
                    qr_path.unlink()

    async def terminate(self) -> None:
        if self._route_task:
            self._route_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._route_task
        if self._maintenance_task:
            self._maintenance_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._maintenance_task
        self.routes.unregister()
        manager = self.context.get_llm_tool_manager()
        manager.remove_func("create_alipay_bill")
        manager.remove_func("verify_alipay_bill")

    async def _register_public_routes(self) -> None:
        try:
            await self.routes.register(
                self._landing_page, self._notify_callback, self._return_page
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("支付宝公开支付路由注册失败")
