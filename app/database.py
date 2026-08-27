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


def _migrate_legacy_addresses(target_engine: Engine) -> None:
    """Move the former single-address columns into the one-to-many table once."""
    inspector = inspect(target_engine)
    tables = set(inspector.get_table_names())
    if not {"contacts", "addresses"}.issubset(tables):
        return

    legacy_columns = {"address", "city", "state", "postal_code", "country"}
    contact_columns = {column["name"] for column in inspector.get_columns("contacts")}
    if not legacy_columns.issubset(contact_columns):
        return

    with target_engine.begin() as connection:
        # The unique version row is both a durable migration marker and a
        # cross-process lock. PostgreSQL waits on an uncommitted conflicting
        # insert; SQLite serializes writers. Only the transaction receiving the
        # RETURNING row is allowed to copy and clear legacy values.
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS app_schema_migrations (
                    version VARCHAR(100) PRIMARY KEY
                )
                """
            )
        )
        claimed = connection.execute(
            text(
                """
                INSERT INTO app_schema_migrations (version)
                VALUES (:version)
                ON CONFLICT (version) DO NOTHING
                RETURNING version
                """
            ),
            {"version": "2026-typed-addresses"},
        ).scalar_one_or_none()
        if claimed is None:
            return

        connection.execute(
            text(
                """
                INSERT INTO addresses
                    (contact_id, type, street_address, city, state, postal_code, country)
                SELECT
                    contacts.id, 'Home', NULLIF(TRIM(contacts.address), ''),
                    NULLIF(TRIM(contacts.city), ''), NULLIF(TRIM(contacts.state), ''),
                    NULLIF(TRIM(contacts.postal_code), ''),
                    NULLIF(TRIM(contacts.country), '')
                FROM contacts
                WHERE (
                    NULLIF(TRIM(contacts.address), '') IS NOT NULL OR
                    NULLIF(TRIM(contacts.city), '') IS NOT NULL OR
                    NULLIF(TRIM(contacts.state), '') IS NOT NULL OR
                    NULLIF(TRIM(contacts.postal_code), '') IS NOT NULL OR
                    NULLIF(TRIM(contacts.country), '') IS NOT NULL
                )
                AND NOT EXISTS (
                    SELECT 1 FROM addresses
                    WHERE addresses.contact_id = contacts.id
                      AND COALESCE(TRIM(addresses.street_address), '') =
                          COALESCE(TRIM(contacts.address), '')
                      AND COALESCE(TRIM(addresses.city), '') =
                          COALESCE(TRIM(contacts.city), '')
                      AND COALESCE(TRIM(addresses.state), '') =
                          COALESCE(TRIM(contacts.state), '')
                      AND COALESCE(TRIM(addresses.postal_code), '') =
                          COALESCE(TRIM(contacts.postal_code), '')
                      AND COALESCE(TRIM(addresses.country), '') =
                          COALESCE(TRIM(contacts.country), '')
                )
                """
            )
        )
        # Every meaningful value is now either copied or already represented by
        # an exact typed-address match. Whitespace-only values carry no data.
        connection.execute(
            text(
                """
                UPDATE contacts
                SET address = NULL, city = NULL, state = NULL,
                    postal_code = NULL, country = NULL
                """
            )
        )


def init_db() -> None:
    """Create tables and upgrade supported older schemas; safe to call repeatedly."""
    from app import models  # noqa: F401  (register models on Base.metadata)

    Base.metadata.create_all(bind=engine)
    _upgrade_schema(engine)
    _migrate_legacy_addresses(engine)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a session that is always closed."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
