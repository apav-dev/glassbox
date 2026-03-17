from __future__ import annotations

import enum
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterator

from sqlalchemy import (
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    inspect,
    text,
)
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


class DailyPrice(Base):
    __tablename__ = "daily_prices"

    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)


class Ticker(Base):
    __tablename__ = "tickers"

    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    company_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sector: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    industry: Mapped[str | None] = mapped_column(String(128), nullable=True)
    country: Mapped[str | None] = mapped_column(String(128), nullable=True)
    beta: Mapped[float | None] = mapped_column(Float, nullable=True)


class Tag(Base):
    __tablename__ = "tags"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    color: Mapped[str | None] = mapped_column(String(7), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)


class TickerTag(Base):
    __tablename__ = "ticker_tags"

    symbol: Mapped[str] = mapped_column(
        String(16),
        ForeignKey("tickers.symbol"),
        primary_key=True,
    )
    tag_name: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tags.name"),
        primary_key=True,
    )
    tagged_by: Mapped[str] = mapped_column(String(64), nullable=False, default="agent")
    tagged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class TickerFundamentals(Base):
    __tablename__ = "ticker_fundamentals"

    symbol: Mapped[str] = mapped_column(
        String(16),
        ForeignKey("tickers.symbol"),
        primary_key=True,
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    market_cap: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    trailing_pe: Mapped[float | None] = mapped_column(Float, nullable=True)
    forward_pe: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_to_book: Mapped[float | None] = mapped_column(Float, nullable=True)
    ev_to_ebitda: Mapped[float | None] = mapped_column(Float, nullable=True)
    ev_to_revenue: Mapped[float | None] = mapped_column(Float, nullable=True)
    peg_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_to_sales: Mapped[float | None] = mapped_column(Float, nullable=True)
    profit_margin: Mapped[float | None] = mapped_column(Float, nullable=True)
    gross_margin: Mapped[float | None] = mapped_column(Float, nullable=True)
    operating_margin: Mapped[float | None] = mapped_column(Float, nullable=True)
    ebitda_margin: Mapped[float | None] = mapped_column(Float, nullable=True)
    return_on_equity: Mapped[float | None] = mapped_column(Float, nullable=True)
    return_on_assets: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_high_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_low_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_mean_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_median_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    analyst_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recommendation_key: Mapped[str | None] = mapped_column(String(32), nullable=True)
    recommendation_mean: Mapped[float | None] = mapped_column(Float, nullable=True)


class InvestmentThesis(Base):
    __tablename__ = "investment_theses"
    __table_args__ = (
        UniqueConstraint("symbol", "version", name="uq_investment_theses_symbol_version"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(
        String(16),
        ForeignKey("tickers.symbol"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    thesis: Mapped[str] = mapped_column(Text, nullable=False)
    catalyst: Mapped[str | None] = mapped_column(Text, nullable=True)
    edge: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    created_by: Mapped[str] = mapped_column(String(64), nullable=False, default="agent")


class RealizedPnl(Base):
    __tablename__ = "realized_pnl"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    sell_trade_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("trades.trade_id"),
        nullable=False,
        index=True,
    )
    qty: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    buy_price: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    sell_price: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    pnl: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


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
    if not inspector.has_table("tags"):
        Tag.__table__.create(bind=engine)
    if not inspector.has_table("ticker_tags"):
        TickerTag.__table__.create(bind=engine)
    if not inspector.has_table("daily_prices"):
        DailyPrice.__table__.create(bind=engine)
    if not inspector.has_table("realized_pnl"):
        RealizedPnl.__table__.create(bind=engine)
    if not inspector.has_table("ticker_fundamentals"):
        TickerFundamentals.__table__.create(bind=engine)
    if not inspector.has_table("investment_theses"):
        InvestmentThesis.__table__.create(bind=engine)
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
