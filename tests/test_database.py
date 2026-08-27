from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import StaticPool

from app.database import Base, _migrate_legacy_addresses, _upgrade_schema


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


def test_legacy_single_address_is_migrated_once():
    legacy_engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    try:
        with legacy_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE contacts (
                        id INTEGER PRIMARY KEY,
                        address VARCHAR(300), city VARCHAR(120), state VARCHAR(120),
                        postal_code VARCHAR(20), country VARCHAR(120)
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO contacts (id, address, city, state, postal_code, country)
                    VALUES (7, '1 Market St', 'San Francisco', 'CA', '94105', 'USA')
                    """
                )
            )

        # create_all leaves the legacy contacts table in place and creates the
        # new addresses table registered in model metadata.
        Base.metadata.create_all(bind=legacy_engine)
        _migrate_legacy_addresses(legacy_engine)
        _migrate_legacy_addresses(legacy_engine)

        with legacy_engine.connect() as connection:
            migrated = connection.execute(
                text(
                    """
                    SELECT contact_id, type, street_address, city, state, postal_code, country
                    FROM addresses
                    """
                )
            ).one()
            legacy = connection.execute(
                text("SELECT address, city, state, postal_code, country FROM contacts WHERE id = 7")
            ).one()

        assert tuple(migrated) == (7, "Home", "1 Market St", "San Francisco", "CA", "94105", "USA")
        assert tuple(legacy) == (None, None, None, None, None)
    finally:
        legacy_engine.dispose()
