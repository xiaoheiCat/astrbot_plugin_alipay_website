from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

import alipay_gateway as gateway_module
from alipay_gateway import (
    AlipayConfigurationError,
    AlipayCredentials,
    AlipayGateway,
    format_alipay_time_expire,
    response_body,
)


def test_sandbox_credentials_are_loaded_from_plugin_config() -> None:
    credentials = AlipayCredentials.load(
        {
            "environment": "sandbox",
            "app_id": "configured-sandbox-app",
            "app_private_key": "configured-private-key",
            "alipay_public_key": "configured-public-key",
            "seller_id": "2088000000000001",
        }
    )

    assert credentials.environment == "sandbox"
    assert credentials.app_id == "configured-sandbox-app"
    assert credentials.app_private_key == "configured-private-key"
    assert credentials.alipay_public_key == "configured-public-key"
    assert credentials.seller_id == "2088000000000001"


def test_missing_sandbox_config_fails_closed() -> None:
    with pytest.raises(AlipayConfigurationError, match="插件配置页"):
        AlipayCredentials.load({"environment": "sandbox"})


def test_production_requires_all_security_fields() -> None:
    with pytest.raises(AlipayConfigurationError, match="seller_id"):
        AlipayCredentials.load(
            {
                "environment": "production",
                "app_id": "app",
                "app_private_key": "private",
                "alipay_public_key": "public",
            }
        )


def test_response_body_selects_official_response_node() -> None:
    assert response_body(
        {"alipay_trade_query_response": {"code": "10000"}},
        "alipay.trade.query",
    ) == {"code": "10000"}


def test_absolute_expiry_is_formatted_in_alipay_timezone() -> None:
    expires_at = datetime(2026, 9, 1, 12, 15, tzinfo=UTC)

    assert format_alipay_time_expire(expires_at) == "2026-09-01 20:15:00"
    with pytest.raises(ValueError, match="时区"):
        format_alipay_time_expire(datetime(2026, 9, 1, 20, 15))


@pytest.mark.asyncio
async def test_page_pay_form_uses_absolute_expiry(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeModel:
        pass

    class FakeRequest:
        def __init__(self, biz_model):
            self.biz_model = biz_model
            self.notify_url = None
            self.return_url = None

    class FakeClient:
        def page_execute(self, request, method):
            captured["request"] = request
            captured["method"] = method
            return "signed-form"

    monkeypatch.setattr(gateway_module, "AlipayTradePagePayModel", FakeModel)
    monkeypatch.setattr(gateway_module, "AlipayTradePagePayRequest", FakeRequest)
    gateway = object.__new__(AlipayGateway)
    gateway._client = FakeClient()

    result = await gateway.page_pay_form(
        out_trade_no="AIP20260901120000000000000000",
        amount=Decimal("1.00"),
        subject="AI Bot 收款",
        expires_at=datetime(2026, 9, 1, 12, 15, tzinfo=UTC),
        notify_url="https://bot.example/alipay/notify",
        return_url="https://bot.example/alipay/return",
    )

    assert result == "signed-form"
    request = captured["request"]
    assert request.biz_model.time_expire == "2026-09-01 20:15:00"
    assert not hasattr(request.biz_model, "timeout_express")
    assert captured["method"] == "POST"
