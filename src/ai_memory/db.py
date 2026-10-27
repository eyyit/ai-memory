from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import create_engine, event, inspect, MetaData, Table, Column, Integer, DateTime, select, insert, text
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    pass


SCHEMA_VERSION = 1
_schema_metadata = MetaData()
_schema_table = Table("ai_memory_schema", _schema_metadata,
    Column("id", Integer, primary_key=True), Column("version", Integer, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False))


def make_engine(url: str):
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine_options = {"poolclass": StaticPool} if url in {"sqlite://", "sqlite:///:memory:"} else {}
    engine = create_engine(url, future=True, pool_pre_ping=True, connect_args=connect_args, **engine_options)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _foreign_keys(dbapi_connection, _):
            dbapi_connection.execute("PRAGMA foreign_keys=ON")
    return engine


def make_session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


def initialize_schema(engine) -> None:
    """Create a fresh schema and refuse unversioned or mismatched existing stores."""
    existing = set(inspect(engine).get_table_names())
    domain_tables = set(Base.metadata.tables)
    if "ai_memory_schema" not in existing and existing.intersection(domain_tables):
        raise RuntimeError("Unversioned AI Memory database detected; export or migrate it explicitly before startup")
    _schema_table.create(engine, checkfirst=True)
    with engine.begin() as connection:
        current = connection.execute(select(_schema_table.c.version).where(_schema_table.c.id == 1)).scalar_one_or_none()
        if current is None:
            connection.execute(insert(_schema_table).values(id=1, version=SCHEMA_VERSION,
                updated_at=datetime.now(timezone.utc)))
        elif current != SCHEMA_VERSION:
            raise RuntimeError(f"AI Memory schema version {current} is unsupported; expected {SCHEMA_VERSION}")
    Base.metadata.create_all(engine)
    if engine.dialect.name == 'postgresql':
        with engine.begin() as connection:
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_memory_fts ON memories USING gin (to_tsvector('english', content)) WHERE active"))
