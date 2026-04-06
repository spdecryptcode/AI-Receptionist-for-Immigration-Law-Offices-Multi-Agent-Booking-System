"""
Alembic environment for async SQLAlchemy (asyncpg driver).

run_migrations_offline(): generates SQL without a live DB connection.
run_migrations_online(): connects to the DB and applies migrations.

Both paths import all models so Alembic's autogenerate can diff them.
"""

import asyncio
import os
from logging.config import fileConfig

from dotenv import load_dotenv

load_dotenv()

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Import Base so Alembic autogenerate can see all models
from app.database.models import Base  # noqa: F401 — needed for metadata

# ---------------------------------------------------------------------------
# Alembic Config object (provides access to alembic.ini values)
# ---------------------------------------------------------------------------

config = context.config

# Override sqlalchemy.url from env var DB_URL or DATABASE_URL if present
# This lets docker-compose and CI inject the real connection string without
# editing alembic.ini.
db_url = os.environ.get("DATABASE_URL") or os.environ.get("DB_URL")
if db_url:
    # Normalise to asyncpg URL for the async engine.
    async_url = db_url
    if async_url.startswith("postgres://"):
        async_url = "postgresql+asyncpg://" + async_url[len("postgres://"):]
    elif async_url.startswith("postgresql://"):
        async_url = "postgresql+asyncpg://" + async_url[len("postgresql://"):]
    elif async_url.startswith("postgresql+psycopg2://"):
        async_url = "postgresql+asyncpg://" + async_url[len("postgresql+psycopg2://"):]
    elif async_url.startswith("postgres+psycopg2://"):
        async_url = "postgresql+asyncpg://" + async_url[len("postgres+psycopg2://"):]
    config.set_main_option("sqlalchemy.url", async_url.replace("%", "%%"))

# Interpret the config file for Python logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


# ---------------------------------------------------------------------------
# Offline mode — generates SQL script without connecting
# ---------------------------------------------------------------------------


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


# ---------------------------------------------------------------------------
# Online mode — connects to DB and applies migrations
# ---------------------------------------------------------------------------


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
