from collections.abc import Generator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def _engine_kwargs(database_url: str) -> dict:
    if not database_url.startswith("sqlite"):
        return {}

    kwargs: dict = {"connect_args": {"check_same_thread": False}}
    if ":memory:" in database_url or "mode=memory" in database_url:
        # A plain in-memory SQLite database lives and dies with its connection.
        # StaticPool keeps a single connection alive so every request — and every
        # thread FastAPI hands work to — sees the same data for the process's lifetime.
        kwargs["poolclass"] = StaticPool
    return kwargs


settings = get_settings()

engine = create_engine(
    settings.database_url,
    echo=settings.sql_echo,
    **_engine_kwargs(settings.database_url),
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@event.listens_for(Engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
    if engine.dialect.name != "sqlite":
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def _upgrade_schema(target_engine: Engine) -> None:
    """Apply small, idempotent upgrades for databases created by older releases."""
    inspector = inspect(target_engine)
    if "contacts" not in inspector.get_table_names():
        return
    if "photo" in {column["name"] for column in inspector.get_columns("contacts")}:
        return

    try:
        with target_engine.begin() as connection:
            connection.execute(text("ALTER TABLE contacts ADD COLUMN photo TEXT"))
    except SQLAlchemyError:
        # Multiple application workers can race during startup. Suppress only
        # the harmless case where another worker completed this exact upgrade.
        if "photo" in {
            column["name"] for column in inspect(target_engine).get_columns("contacts")
        }:
            return
        raise


def init_db() -> None:
    """Create tables and upgrade supported older schemas; safe to call repeatedly."""
    from app import models  # noqa: F401  (register models on Base.metadata)

    Base.metadata.create_all(bind=engine)
    _upgrade_schema(engine)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a session that is always closed."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
