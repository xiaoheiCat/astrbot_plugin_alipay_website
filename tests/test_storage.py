from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from storage import (
    Order,
    OrderCreationLimits,
    OrderLimitExceeded,
    OrderStore,
    QueryLimitExceeded,
)

TEST_LIMITS = OrderCreationLimits(
    window_seconds=60,
    max_recent_per_session=100,
    max_recent_global=100,
    max_active_per_session=100,
    max_active_global=100,
    max_retained_global=100,
)


def make_order(
    store: OrderStore,
    order_no: str = "AIP20260901120000000000000000",
    *,
    token: str = "secret-token",
) -> Order:
    now = datetime.now(UTC)
    return Order(
        out_trade_no=order_no,
        session="platform:FriendMessage:user",
        amount="1.00",
        message="请支付",
        token_hash=store.token_hash(token),
        status="CREATED",
        trade_no=None,
        created_at=now.isoformat(),
        expires_at=(now + timedelta(minutes=15)).isoformat(),
        paid_at=None,
        reminder_state="pending",
        updated_at=now.isoformat(),
    )


@pytest.mark.asyncio
async def test_token_is_hashed_and_session_scope_is_enforced(tmp_path) -> None:
    store = OrderStore(tmp_path / "orders.sqlite3")
    await store.initialize()
    order = make_order(store)
    await store.create(order, TEST_LIMITS)

    assert await store.get_by_token("secret-token") == order
    assert await store.get(order.out_trade_no, "another-session") is None
    assert "secret-token" not in store.path.read_bytes().decode("latin1")


@pytest.mark.asyncio
async def test_notification_and_reminder_are_idempotent(tmp_path) -> None:
    store = OrderStore(tmp_path / "orders.sqlite3")
    await store.initialize()
    order = make_order(store)
    await store.create(order, TEST_LIMITS)

    assert await store.apply_notification(
        "notify-1", order.out_trade_no, "TRADE_SUCCESS", "trade-1"
    )
    assert not await store.apply_notification(
        "notify-1", order.out_trade_no, "TRADE_SUCCESS", "trade-1"
    )
    assert await store.claim_reminder(order.out_trade_no)
    assert not await store.claim_reminder(order.out_trade_no)
    await store.finish_reminder(order.out_trade_no, True)
    updated = await store.get(order.out_trade_no)
    assert updated is not None
    assert updated.status == "TRADE_SUCCESS"
    assert updated.reminder_state == "delivered"


@pytest.mark.asyncio
async def test_concurrent_creation_cannot_cross_atomic_session_limit(tmp_path) -> None:
    store = OrderStore(tmp_path / "orders.sqlite3")
    await store.initialize()
    limits = OrderCreationLimits(
        window_seconds=60,
        max_recent_per_session=3,
        max_recent_global=10,
        max_active_per_session=10,
        max_active_global=10,
        max_retained_global=10,
    )

    async def create(index: int) -> bool:
        order = make_order(
            store,
            f"AIP20260901120000{index:012X}",
            token=f"token-{index}",
        )
        try:
            await store.create(order, limits)
        except OrderLimitExceeded:
            return False
        return True

    results = await asyncio.gather(*(create(index) for index in range(4)))

    assert sorted(results) == [False, True, True, True]


@pytest.mark.asyncio
async def test_paid_state_is_monotonic_across_queries_and_notifications(tmp_path) -> None:
    store = OrderStore(tmp_path / "orders.sqlite3")
    await store.initialize()
    order = make_order(store)
    await store.create(order, TEST_LIMITS)
    await store.mark_status(order.out_trade_no, "WAIT_BUYER_PAY")
    assert await store.apply_notification(
        "notify-paid", order.out_trade_no, "TRADE_SUCCESS", "trade-1"
    )

    stale = await store.mark_status(
        order.out_trade_no, "WAIT_BUYER_PAY", trade_no="stale-trade"
    )
    assert stale is not None
    assert stale.status == "TRADE_SUCCESS"
    assert stale.trade_no == "trade-1"

    assert await store.apply_notification(
        "notify-closed", order.out_trade_no, "TRADE_CLOSED", None
    )
    final = await store.get(order.out_trade_no)
    assert final is not None
    assert final.status == "TRADE_SUCCESS"
    assert final.trade_no == "trade-1"
    assert await store.claim_reminder(order.out_trade_no)


@pytest.mark.asyncio
async def test_real_payment_can_upgrade_an_expired_local_order(tmp_path) -> None:
    store = OrderStore(tmp_path / "orders.sqlite3")
    await store.initialize()
    order = make_order(store)
    await store.create(order, TEST_LIMITS)
    await store.mark_status(order.out_trade_no, "EXPIRED")

    assert await store.apply_notification(
        "notify-late", order.out_trade_no, "TRADE_SUCCESS", "trade-1"
    )
    updated = await store.get(order.out_trade_no)
    assert updated is not None
    assert updated.status == "TRADE_SUCCESS"
    assert updated.paid_at is not None


@pytest.mark.asyncio
async def test_query_budget_enforces_cooldown(tmp_path) -> None:
    store = OrderStore(tmp_path / "orders.sqlite3")
    await store.initialize()
    order = make_order(store)
    await store.create(order, TEST_LIMITS)

    await store.reserve_query(
        order.out_trade_no, min_interval_seconds=5, max_attempts=60
    )
    with pytest.raises(QueryLimitExceeded, match="频繁"):
        await store.reserve_query(
            order.out_trade_no, min_interval_seconds=5, max_attempts=60
        )


@pytest.mark.asyncio
async def test_exhausted_query_budget_can_end_locally_without_rejecting_late_payment(
    tmp_path,
) -> None:
    store = OrderStore(tmp_path / "orders.sqlite3")
    await store.initialize()
    order = make_order(store)
    await store.create(order, TEST_LIMITS)

    await store.reserve_query(
        order.out_trade_no, min_interval_seconds=0, max_attempts=1
    )
    with pytest.raises(QueryLimitExceeded) as raised:
        await store.reserve_query(
            order.out_trade_no, min_interval_seconds=0, max_attempts=1
        )
    assert raised.value.code == "attempts"

    expired = await store.mark_status(order.out_trade_no, "EXPIRED")
    assert expired is not None
    assert expired.status == "EXPIRED"
    assert await store.apply_notification(
        "notify-after-budget", order.out_trade_no, "TRADE_SUCCESS", "trade-1"
    )
    paid = await store.get(order.out_trade_no)
    assert paid is not None
    assert paid.status == "TRADE_SUCCESS"
