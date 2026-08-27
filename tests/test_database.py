from concurrent.futures import ThreadPoolExecutor

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


def test_whitespace_only_legacy_address_is_cleared_without_creating_a_row():
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
                    VALUES (8, '   ', '', NULL, '  ', NULL)
                    """
                )
            )

        Base.metadata.create_all(bind=legacy_engine)
        _migrate_legacy_addresses(legacy_engine)

        with legacy_engine.connect() as connection:
            assert connection.execute(text("SELECT COUNT(*) FROM addresses")).scalar_one() == 0
            legacy = connection.execute(
                text("SELECT address, city, state, postal_code, country FROM contacts WHERE id = 8")
            ).one()
        assert tuple(legacy) == (None, None, None, None, None)
    finally:
        legacy_engine.dispose()


def test_legacy_address_is_preserved_when_contact_already_has_an_address():
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
                    VALUES (9, '12 Home Lane', 'London', NULL, NULL, 'UK')
                    """
                )
            )

        Base.metadata.create_all(bind=legacy_engine)
        with legacy_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO addresses
                        (contact_id, type, street_address, city, state, postal_code, country)
                    VALUES (9, 'Work', '1 Market St', 'San Francisco', 'CA', '94105', 'USA')
                    """
                )
            )

        _migrate_legacy_addresses(legacy_engine)

        with legacy_engine.connect() as connection:
            addresses = connection.execute(
                text(
                    """
                    SELECT type, street_address, city, country
                    FROM addresses WHERE contact_id = 9 ORDER BY id
                    """
                )
            ).all()
        assert [tuple(address) for address in addresses] == [
            ("Work", "1 Market St", "San Francisco", "USA"),
            ("Home", "12 Home Lane", "London", "UK"),
        ]
    finally:
        legacy_engine.dispose()


def test_concurrent_startups_claim_address_migration_once(tmp_path):
    database_path = tmp_path / "legacy.db"
    legacy_engine = create_engine(
        f"sqlite+pysqlite:///{database_path}",
        connect_args={"check_same_thread": False, "timeout": 10},
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
                    VALUES (10, 'Concurrent Way', 'Portland', 'OR', '97201', 'USA')
                    """
                )
            )
        Base.metadata.create_all(bind=legacy_engine)

        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(_migrate_legacy_addresses, [legacy_engine] * 4))

        with legacy_engine.connect() as connection:
            assert connection.execute(text("SELECT COUNT(*) FROM addresses")).scalar_one() == 1
            assert (
                connection.execute(
                    text(
                        """
                        SELECT COUNT(*) FROM app_schema_migrations
                        WHERE version = '2026-typed-addresses'
                        """
                    )
                ).scalar_one()
                == 1
            )
    finally:
        legacy_engine.dispose()
