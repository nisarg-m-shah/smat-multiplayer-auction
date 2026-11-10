import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from .config import DATABASE_URL
from . import perf

# Render and many managed PostgreSQL providers supply postgresql:// URLs.
# This project installs psycopg v3, so explicitly select its SQLAlchemy dialect.
database_url = DATABASE_URL
if database_url.startswith("postgres://"):
    database_url = "postgresql+psycopg://" + database_url[len("postgres://"): ]
elif database_url.startswith("postgresql://"):
    database_url = "postgresql+psycopg://" + database_url[len("postgresql://"): ]

connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
engine = create_engine(
    database_url, future=True,
    # pool_pre_ping costs one extra database round trip per request (~70 ms on
    # Render -> Supabase). It is kept ON by default because it protects the first
    # bid after an idle period from a dead pooled connection. Set
    # DB_POOL_PRE_PING=0 only after measuring.
    pool_pre_ping=os.getenv("DB_POOL_PRE_PING", "1") != "0",
    connect_args=connect_args,
    pool_size=5 if not database_url.startswith("sqlite") else 5,
    max_overflow=5 if not database_url.startswith("sqlite") else 0,
)
perf.install(engine)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
