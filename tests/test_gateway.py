from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

import alipay_gateway as gateway_module
from alipay_gateway import (
    AlipayConfigurationError,
    AlipayCredentials,
    AlipayGateway,
    format_alipay_time_expire,
    response_body,
)


def test_load_sandbox_uses_same_app_entry(tmp_path: Path) -> None:
    config = {
        "appIds": [
            {
                "appId": "sandbox-app",
                "appPrivatePkcsKey": "private-key",
                "alipayPublicKey": "public-key",
                "pid": "2088000000000000",
            }
        ]
    }
    (tmp_path / ".alipay-sandbox.json").write_text(json.dumps(config), encoding="utf-8")

    credentials = AlipayCredentials.load({"environment": "sandbox"}, tmp_path)

    assert credentials.app_id == "sandbox-app"
    assert credentials.app_private_key == "private-key"
    assert credentials.alipay_public_key == "public-key"
    assert credentials.seller_id == "2088000000000000"


def test_missing_sandbox_config_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(AlipayConfigurationError):
        AlipayCredentials.load({"environment": "sandbox"}, tmp_path)


def test_production_requires_all_security_fields(tmp_path: Path) -> None:
    with pytest.raises(AlipayConfigurationError, match="seller_id"):
        AlipayCredentials.load(
            {
                "environment": "production",
                "app_id": "app",
                "app_private_key": "private",
                "alipay_public_key": "public",
            },
            tmp_path,
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
