import os
import weakref
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


def default_database_url() -> str:
    return f"sqlite:///{Path(__file__).resolve().parent.parent / 'data' / 'control.db'}"


def create_app_engine(
    database_url: str | None = None,
    *,
    pool_size: int | None = None,
    max_overflow: int | None = None,
    statement_timeout_ms: int = 15000,
) -> Engine:
    """One engine per process with a connection pool.

    It used to be NullPool for SQLite, i.e. a brand-new database connection for every
    request (the bot makes 4-8 control calls per message). Connections are now reused.
    PostgreSQL URLs (postgresql+psycopg://...) get a small pool with liveness checks and
    a statement timeout so a stuck query cannot hang the bot behind it.
    """
    url = database_url or os.getenv("DATABASE_URL", "") or default_database_url()
    if url.startswith("sqlite"):
        memory = url in ("sqlite://", "sqlite:///:memory:")
        if not memory and url.startswith("sqlite:///"):
            Path(url[len("sqlite:///"):]).parent.mkdir(parents=True, exist_ok=True)
        kwargs = {"connect_args": {"check_same_thread": False, "timeout": 30}}
        if memory:
            from sqlalchemy.pool import StaticPool

            kwargs["poolclass"] = StaticPool
        engine = create_engine(url, future=True, **kwargs)

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - trivial
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()

        return engine
    return create_engine(
        url,
        future=True,
        pool_size=pool_size or int(os.getenv("DATABASE_POOL_SIZE", "5")),
        max_overflow=max_overflow if max_overflow is not None else int(os.getenv("DATABASE_MAX_OVERFLOW", "10")),
        pool_pre_ping=True,
        pool_recycle=1800,
        connect_args={
            "connect_timeout": 5,
            "options": f"-c statement_timeout={int(statement_timeout_ms)} -c lock_timeout={int(statement_timeout_ms)}",
        },
    )


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)


_session_factories: "weakref.WeakKeyDictionary[Engine, sessionmaker]" = weakref.WeakKeyDictionary()


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    factory = _session_factories.get(engine)
    if factory is None:
        factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
        _session_factories[engine] = factory
    return factory


@contextmanager
def session_scope(engine: Engine):
    session = make_session_factory(engine)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
