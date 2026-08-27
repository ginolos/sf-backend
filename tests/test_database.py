from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import StaticPool

from app.database import _upgrade_schema


def test_upgrade_adds_photo_column_to_legacy_contacts_table():
    legacy_engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    try:
        with legacy_engine.begin() as connection:
            connection.execute(text("CREATE TABLE contacts (id INTEGER PRIMARY KEY)"))

        _upgrade_schema(legacy_engine)
        _upgrade_schema(legacy_engine)  # The startup upgrade must be idempotent.

        columns = {column["name"] for column in inspect(legacy_engine).get_columns("contacts")}
        assert columns == {"id", "photo"}
    finally:
        legacy_engine.dispose()
