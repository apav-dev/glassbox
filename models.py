from __future__ import annotations

import enum
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterator

from sqlalchemy import Date, DateTime, Enum, Float, Numeric, String, Text, create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


class TradeAction(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"


class Trade(Base):
    __tablename__ = "trades"

    trade_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
        default=lambda: datetime.now(timezone.utc),
    )
    ticker: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    action: Mapped[TradeAction] = mapped_column(
        Enum(TradeAction, native_enum=False),
        nullable=False,
    )
    qty: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    price: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    commission: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False, default=0)
    strategy_tag: Mapped[str | None] = mapped_column(String(128), nullable=True)
    investment_thesis: Mapped[str] = mapped_column(Text, nullable=False)


class DailySnapshot(Base):
    __tablename__ = "daily_snapshots"

    date: Mapped[date] = mapped_column(Date, primary_key=True)
    equity_value: Mapped[float] = mapped_column(Numeric(18, 2), nullable=False)
    cash_balance: Mapped[float] = mapped_column(Numeric(18, 2), nullable=False)
    long_market_value: Mapped[float] = mapped_column(Numeric(18, 2), nullable=False)
    total_drawdown: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)


class Ticker(Base):
    __tablename__ = "tickers"

    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    company_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sector: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    industry: Mapped[str | None] = mapped_column(String(128), nullable=True)
    country: Mapped[str | None] = mapped_column(String(128), nullable=True)
    beta: Mapped[float | None] = mapped_column(Float, nullable=True)


def _normalize_database_url(database_url: str) -> str:
    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql+psycopg://", 1)
    if database_url.startswith("postgresql://") and "+psycopg" not in database_url:
        return database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return database_url


def _default_sqlite_url() -> str:
    sqlite_path = Path(os.getenv("SQLITE_PATH", "./glassbox.db")).expanduser()
    if not sqlite_path.is_absolute():
        sqlite_path = Path.cwd() / sqlite_path
    return f"sqlite:///{sqlite_path}"


def get_database_url(explicit_url: str | None = None) -> str:
    if explicit_url:
        return _normalize_database_url(explicit_url)

    for env_var in ("DATABASE_URL", "SQLALCHEMY_DATABASE_URL", "SQLALCHEMY_DATABASE_URI"):
        env_value = os.getenv(env_var)
        if env_value:
            return _normalize_database_url(env_value)

    postgres_host = os.getenv("POSTGRES_HOST")
    postgres_db = os.getenv("POSTGRES_DB")
    postgres_user = os.getenv("POSTGRES_USER")
    postgres_password = os.getenv("POSTGRES_PASSWORD")

    if postgres_host and postgres_db and postgres_user and postgres_password:
        postgres_port = os.getenv("POSTGRES_PORT", "5432")
        return (
            f"postgresql+psycopg://{postgres_user}:{postgres_password}"
            f"@{postgres_host}:{postgres_port}/{postgres_db}"
        )

    return _default_sqlite_url()


def create_db_engine(database_url: str | None = None, echo: bool = False) -> Engine:
    resolved_url = get_database_url(database_url)
    connect_args = {"check_same_thread": False} if resolved_url.startswith("sqlite") else {}
    return create_engine(
        resolved_url,
        echo=echo,
        pool_pre_ping=True,
        connect_args=connect_args,
    )


DATABASE_URL = get_database_url()
engine = create_db_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    inspector = inspect(engine)
    ticker_columns = {column["name"] for column in inspector.get_columns("tickers")}
    if "country" not in ticker_columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE tickers ADD COLUMN country VARCHAR(128)"))


def get_db() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
