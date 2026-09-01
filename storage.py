from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

PAID_STATUSES = frozenset({"TRADE_SUCCESS", "TRADE_FINISHED"})
NONPAID_TERMINAL_STATUSES = frozenset({"CLOSED", "EXPIRED", "TRADE_CLOSED"})
KNOWN_STATUSES = frozenset(
    {"CREATED", "WAIT_BUYER_PAY", *NONPAID_TERMINAL_STATUSES, *PAID_STATUSES}
)
DEFAULT_SUBJECT = "AI Bot 收款"


@dataclass(frozen=True, slots=True)
class OrderCreationLimits:
    window_seconds: int
    max_recent_per_session: int
    max_recent_global: int
    max_active_per_session: int
    max_active_global: int
    max_retained_global: int


class OrderLimitExceeded(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class QueryLimitExceeded(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class OrderStateConflict(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class Order:
    out_trade_no: str
    session: str
    amount: str
    message: str
    subject: str
    token_hash: str
    status: str
    trade_no: str | None
    created_at: str
    expires_at: str
    paid_at: str | None
    reminder_state: str
    updated_at: str

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> Order:
        return cls(**dict(row))


class OrderStore:
    def __init__(self, path: Path):
        self.path = path

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with self._connect() as db:
            await db.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS orders (
                    out_trade_no TEXT PRIMARY KEY,
                    session TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    message TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'CREATED',
                    trade_no TEXT,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    paid_at TEXT,
                    reminder_state TEXT NOT NULL DEFAULT 'pending',
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_orders_expiry ON orders(status, expires_at);
                CREATE INDEX IF NOT EXISTS idx_orders_reminder ON orders(reminder_state, status);
                CREATE TABLE IF NOT EXISTS notifications (
                    notify_id TEXT PRIMARY KEY,
                    out_trade_no TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    FOREIGN KEY(out_trade_no) REFERENCES orders(out_trade_no) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS query_budgets (
                    out_trade_no TEXT PRIMARY KEY,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_attempt_at TEXT,
                    FOREIGN KEY(out_trade_no) REFERENCES orders(out_trade_no) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_orders_session_created
                    ON orders(session, created_at);
                CREATE INDEX IF NOT EXISTS idx_orders_session_active
                    ON orders(session, status, expires_at);
                """
            )
            async with db.execute("PRAGMA table_info(orders)") as cursor:
                columns = {str(row[1]) for row in await cursor.fetchall()}
            if "subject" not in columns:
                await db.execute(
                    "ALTER TABLE orders ADD COLUMN subject "
                    "TEXT NOT NULL DEFAULT 'AI Bot 收款'"
                )
            # 修复旧版本中已记录付款时间、但状态被陈旧查询降级的数据。
            await db.execute(
                """UPDATE orders SET status='TRADE_SUCCESS'
                   WHERE paid_at IS NOT NULL
                   AND status NOT IN ('TRADE_SUCCESS','TRADE_FINISHED')"""
            )
            await db.execute(
                """UPDATE orders SET paid_at=updated_at
                   WHERE status IN ('TRADE_SUCCESS','TRADE_FINISHED')
                   AND paid_at IS NULL"""
            )
            await db.commit()

    def _connect(self) -> aiosqlite.Connection:
        return aiosqlite.connect(self.path)

    @staticmethod
    def token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    async def create(self, order: Order, limits: OrderCreationLimits) -> None:
        now = datetime.now(UTC)
        recent_since = (now - timedelta(seconds=limits.window_seconds)).isoformat()
        now_text = now.isoformat()
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                retained = await self._count(db, "SELECT COUNT(*) FROM orders", ())
                if retained >= limits.max_retained_global:
                    raise OrderLimitExceeded("retained", "收款服务订单容量已满，请稍后再试。")

                recent_session = await self._count(
                    db,
                    "SELECT COUNT(*) FROM orders WHERE session=? AND created_at>=?",
                    (order.session, recent_since),
                )
                if recent_session >= limits.max_recent_per_session:
                    raise OrderLimitExceeded("session_rate", "创建订单过于频繁，请稍后再试。")

                recent_global = await self._count(
                    db,
                    "SELECT COUNT(*) FROM orders WHERE created_at>=?",
                    (recent_since,),
                )
                if recent_global >= limits.max_recent_global:
                    raise OrderLimitExceeded("global_rate", "收款服务当前繁忙，请稍后再试。")

                active_session = await self._count(
                    db,
                    """SELECT COUNT(*) FROM orders WHERE session=?
                       AND status IN ('CREATED','WAIT_BUYER_PAY') AND expires_at>?""",
                    (order.session, now_text),
                )
                if active_session >= limits.max_active_per_session:
                    raise OrderLimitExceeded(
                        "session_active", "当前会话未完成订单已达上限，请先完成或等待过期。"
                    )

                active_global = await self._count(
                    db,
                    """SELECT COUNT(*) FROM orders
                       WHERE status IN ('CREATED','WAIT_BUYER_PAY') AND expires_at>?""",
                    (now_text,),
                )
                if active_global >= limits.max_active_global:
                    raise OrderLimitExceeded("global_active", "收款服务当前繁忙，请稍后再试。")

                await db.execute(
                    """INSERT INTO orders (
                           out_trade_no, session, amount, message, subject, token_hash,
                           status, trade_no, created_at, expires_at, paid_at,
                           reminder_state, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        order.out_trade_no,
                        order.session,
                        order.amount,
                        order.message,
                        order.subject,
                        order.token_hash,
                        order.status,
                        order.trade_no,
                        order.created_at,
                        order.expires_at,
                        order.paid_at,
                        order.reminder_state,
                        order.updated_at,
                    ),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    @staticmethod
    async def _count(
        db: aiosqlite.Connection, query: str, args: tuple[Any, ...]
    ) -> int:
        async with db.execute(query, args) as cursor:
            row = await cursor.fetchone()
            return int(row[0])

    async def get(self, out_trade_no: str, session: str | None = None) -> Order | None:
        query = "SELECT * FROM orders WHERE out_trade_no = ?"
        args: tuple[Any, ...] = (out_trade_no,)
        if session is not None:
            query += " AND session = ?"
            args += (session,)
        return await self._one(query, args)

    async def get_by_token(self, token: str) -> Order | None:
        return await self._one(
            "SELECT * FROM orders WHERE token_hash = ?", (self.token_hash(token),)
        )

    async def _one(self, query: str, args: tuple[Any, ...]) -> Order | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query, args) as cursor:
                row = await cursor.fetchone()
                return Order.from_row(row) if row else None

    async def mark_status(
        self,
        out_trade_no: str,
        status: str,
        *,
        trade_no: str | None = None,
    ) -> Order | None:
        now = utc_now()
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                order = await self._transition_status(
                    db, out_trade_no, status, trade_no=trade_no, now=now
                )
                await db.commit()
                return order
            except Exception:
                await db.rollback()
                raise

    async def apply_notification(
        self, notify_id: str, out_trade_no: str, status: str, trade_no: str | None
    ) -> bool:
        now = utc_now()
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    "INSERT OR IGNORE INTO notifications VALUES (?, ?, ?)",
                    (notify_id, out_trade_no, now),
                )
                inserted = cursor.rowcount == 1
                if inserted:
                    await self._transition_status(
                        db, out_trade_no, status, trade_no=trade_no, now=now
                    )
                await db.commit()
                return inserted
            except Exception:
                await db.rollback()
                raise

    async def _transition_status(
        self,
        db: aiosqlite.Connection,
        out_trade_no: str,
        target_status: str,
        *,
        trade_no: str | None,
        now: str,
    ) -> Order | None:
        async with db.execute(
            "SELECT * FROM orders WHERE out_trade_no=?", (out_trade_no,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None

        current = Order.from_row(row)
        if target_status not in KNOWN_STATUSES or not self._transition_allowed(
            current.status, target_status
        ):
            return current
        if current.trade_no and trade_no and current.trade_no != trade_no:
            raise OrderStateConflict("同一商户订单号出现不一致的支付宝交易号")

        next_trade_no = current.trade_no or trade_no
        paid_at = current.paid_at
        if target_status in PAID_STATUSES and paid_at is None:
            paid_at = now
        await db.execute(
            """UPDATE orders SET status=?, trade_no=?, paid_at=?, updated_at=?
               WHERE out_trade_no=?""",
            (target_status, next_trade_no, paid_at, now, out_trade_no),
        )
        async with db.execute(
            "SELECT * FROM orders WHERE out_trade_no=?", (out_trade_no,)
        ) as cursor:
            updated = await cursor.fetchone()
        return Order.from_row(updated)

    @staticmethod
    def _transition_allowed(current: str, target: str) -> bool:
        if current == target:
            return True
        if target in PAID_STATUSES:
            return current != "TRADE_FINISHED" or target == "TRADE_FINISHED"
        if current in PAID_STATUSES or current in NONPAID_TERMINAL_STATUSES:
            return False
        if current == "WAIT_BUYER_PAY":
            return target != "CREATED"
        return current == "CREATED"

    async def reserve_query(
        self,
        out_trade_no: str,
        *,
        min_interval_seconds: int,
        max_attempts: int,
    ) -> None:
        now = datetime.now(UTC)
        cutoff = (now - timedelta(seconds=min_interval_seconds)).isoformat()
        now_text = now.isoformat()
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    """SELECT attempt_count, last_attempt_at FROM query_budgets
                       WHERE out_trade_no=?""",
                    (out_trade_no,),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    await db.execute(
                        """INSERT INTO query_budgets
                           (out_trade_no, attempt_count, last_attempt_at) VALUES (?, 1, ?)""",
                        (out_trade_no, now_text),
                    )
                else:
                    attempts, last_attempt_at = int(row[0]), row[1]
                    if attempts >= max_attempts:
                        raise QueryLimitExceeded("attempts", "该订单查询次数已达上限。")
                    if last_attempt_at is not None and last_attempt_at > cutoff:
                        raise QueryLimitExceeded("cooldown", "查询过于频繁，请稍后再试。")
                    await db.execute(
                        """UPDATE query_budgets
                           SET attempt_count=attempt_count+1, last_attempt_at=?
                           WHERE out_trade_no=?""",
                        (now_text, out_trade_no),
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def claim_reminder(self, out_trade_no: str) -> bool:
        async with self._connect() as db:
            cursor = await db.execute(
                """UPDATE orders SET reminder_state='delivering', updated_at=?
                   WHERE out_trade_no=? AND reminder_state='pending'
                   AND paid_at IS NOT NULL""",
                (utc_now(), out_trade_no),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def finish_reminder(self, out_trade_no: str, success: bool) -> None:
        async with self._connect() as db:
            await db.execute(
                "UPDATE orders SET reminder_state=?, updated_at=? WHERE out_trade_no=?",
                ("delivered" if success else "pending", utc_now(), out_trade_no),
            )
            await db.commit()

    async def maintenance_candidates(self) -> list[Order]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT * FROM orders WHERE
                   (status IN ('CREATED','WAIT_BUYER_PAY') AND expires_at < ?)
                   OR (paid_at IS NOT NULL AND reminder_state='pending')
                   LIMIT 100""",
                (utc_now(),),
            ) as cursor:
                return [Order.from_row(row) async for row in cursor]

    async def purge_before(self, cutoff: str) -> None:
        async with self._connect() as db:
            await db.execute(
                """DELETE FROM query_budgets WHERE out_trade_no IN (
                   SELECT out_trade_no FROM orders WHERE updated_at < ? AND status IN
                   ('CLOSED','EXPIRED','TRADE_CLOSED','TRADE_SUCCESS','TRADE_FINISHED'))""",
                (cutoff,),
            )
            await db.execute(
                """DELETE FROM notifications WHERE out_trade_no IN (
                   SELECT out_trade_no FROM orders WHERE updated_at < ? AND status IN
                   ('CLOSED','EXPIRED','TRADE_CLOSED','TRADE_SUCCESS','TRADE_FINISHED'))""",
                (cutoff,),
            )
            await db.execute(
                """DELETE FROM orders WHERE updated_at < ? AND status IN
                   ('CLOSED','EXPIRED','TRADE_CLOSED','TRADE_SUCCESS','TRADE_FINISHED')""",
                (cutoff,),
            )
            await db.commit()
