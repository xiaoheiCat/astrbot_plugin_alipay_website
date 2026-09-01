from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from alipay.aop.api.AlipayClientConfig import AlipayClientConfig
from alipay.aop.api.DefaultAlipayClient import DefaultAlipayClient
from alipay.aop.api.domain.AlipayTradeCloseModel import AlipayTradeCloseModel
from alipay.aop.api.domain.AlipayTradeFastpayRefundQueryModel import (
    AlipayTradeFastpayRefundQueryModel,
)
from alipay.aop.api.domain.AlipayTradePagePayModel import AlipayTradePagePayModel
from alipay.aop.api.domain.AlipayTradeQueryModel import AlipayTradeQueryModel
from alipay.aop.api.domain.AlipayTradeRefundModel import AlipayTradeRefundModel
from alipay.aop.api.request.AlipayTradeCloseRequest import AlipayTradeCloseRequest
from alipay.aop.api.request.AlipayTradeFastpayRefundQueryRequest import (
    AlipayTradeFastpayRefundQueryRequest,
)
from alipay.aop.api.request.AlipayTradePagePayRequest import AlipayTradePagePayRequest
from alipay.aop.api.request.AlipayTradeQueryRequest import AlipayTradeQueryRequest
from alipay.aop.api.request.AlipayTradeRefundRequest import AlipayTradeRefundRequest
from alipay.aop.api.util.SignatureUtils import get_sign_content, verify_with_rsa

SANDBOX_GATEWAY = "https://openapi-sandbox.dl.alipaydev.com/gateway.do"
PRODUCTION_GATEWAY = "https://openapi.alipay.com/gateway.do"
ALIPAY_TIME_ZONE = ZoneInfo("Asia/Shanghai")


class AlipayConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AlipayCredentials:
    environment: str
    app_id: str
    app_private_key: str
    alipay_public_key: str
    seller_id: str

    @classmethod
    def load(cls, config: dict[str, Any]) -> AlipayCredentials:
        environment = str(config.get("environment", "sandbox")).strip().lower()
        if environment not in {"sandbox", "production"}:
            raise AlipayConfigurationError("environment 只能是 sandbox 或 production")

        normalized = {
            "app_id": config.get("app_id"),
            "app_private_key": config.get("app_private_key"),
            "alipay_public_key": config.get("alipay_public_key"),
            "seller_id": config.get("seller_id"),
        }
        normalized = {key: str(value or "").strip() for key, value in normalized.items()}
        missing = [key for key, value in normalized.items() if not value]
        if missing:
            raise AlipayConfigurationError(
                "支付宝配置缺少字段："
                + ", ".join(missing)
                + "；请在插件配置页填写当前环境的完整凭据。"
            )
        return cls(environment=environment, **normalized)


class AlipayGateway:
    def __init__(self, credentials: AlipayCredentials):
        self.credentials = credentials
        sdk_config = AlipayClientConfig()
        sdk_config.server_url = (
            SANDBOX_GATEWAY
            if credentials.environment == "sandbox"
            else PRODUCTION_GATEWAY
        )
        sdk_config.app_id = credentials.app_id
        sdk_config.app_private_key = credentials.app_private_key
        sdk_config.alipay_public_key = credentials.alipay_public_key
        sdk_config.sign_type = "RSA2"
        sdk_config.charset = "utf-8"
        sdk_config.format = "json"
        self._client = DefaultAlipayClient(sdk_config)

    async def page_pay_form(
        self,
        *,
        out_trade_no: str,
        amount: Decimal,
        subject: str,
        expires_at: datetime,
        notify_url: str,
        return_url: str,
    ) -> str:
        model = AlipayTradePagePayModel()
        model.out_trade_no = out_trade_no
        model.total_amount = format(amount, ".2f")
        model.subject = subject
        model.product_code = "FAST_INSTANT_TRADE_PAY"
        model.time_expire = format_alipay_time_expire(expires_at)
        request = AlipayTradePagePayRequest(biz_model=model)
        request.notify_url = notify_url
        request.return_url = return_url
        return await asyncio.to_thread(self._client.page_execute, request, "POST")

    async def query(self, out_trade_no: str) -> dict[str, Any]:
        model = AlipayTradeQueryModel()
        model.out_trade_no = out_trade_no
        request = AlipayTradeQueryRequest(biz_model=model)
        return await asyncio.to_thread(self._client.execute, request)

    async def refund(
        self, out_trade_no: str, refund_amount: Decimal, out_request_no: str
    ) -> dict[str, Any]:
        model = AlipayTradeRefundModel()
        model.out_trade_no = out_trade_no
        model.refund_amount = format(refund_amount, ".2f")
        model.out_request_no = out_request_no
        request = AlipayTradeRefundRequest(biz_model=model)
        return await asyncio.to_thread(self._client.execute, request)

    async def refund_query(
        self, out_trade_no: str, out_request_no: str
    ) -> dict[str, Any]:
        model = AlipayTradeFastpayRefundQueryModel()
        model.out_trade_no = out_trade_no
        model.out_request_no = out_request_no
        request = AlipayTradeFastpayRefundQueryRequest(biz_model=model)
        return await asyncio.to_thread(self._client.execute, request)

    async def close(self, out_trade_no: str) -> dict[str, Any]:
        model = AlipayTradeCloseModel()
        model.out_trade_no = out_trade_no
        request = AlipayTradeCloseRequest(biz_model=model)
        return await asyncio.to_thread(self._client.execute, request)

    def verify_notification(self, params: dict[str, str]) -> bool:
        sign = params.get("sign", "")
        sign_type = params.get("sign_type", "")
        if not sign or sign_type.upper() != "RSA2":
            return False
        unsigned = {
            key: value
            for key, value in params.items()
            if key not in {"sign", "sign_type"}
        }
        content = get_sign_content(unsigned).encode("utf-8")
        try:
            return verify_with_rsa(self.credentials.alipay_public_key, content, sign)
        except Exception:
            return False


def response_body(payload: dict[str, Any], method: str) -> dict[str, Any]:
    key = method.replace(".", "_") + "_response"
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def format_alipay_time_expire(expires_at: datetime) -> str:
    if expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise ValueError("支付宝订单绝对到期时间必须包含时区")
    return expires_at.astimezone(ALIPAY_TIME_ZONE).strftime("%Y-%m-%d %H:%M:%S")
