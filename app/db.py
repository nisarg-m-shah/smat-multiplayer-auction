from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from .config import DATABASE_URL

# Render and many managed PostgreSQL providers supply postgresql:// URLs.
# This project installs psycopg v3, so explicitly select its SQLAlchemy dialect.
database_url = DATABASE_URL
if database_url.startswith("postgres://"):
    database_url = "postgresql+psycopg://" + database_url[len("postgres://"): ]
elif database_url.startswith("postgresql://"):
    database_url = "postgresql+psycopg://" + database_url[len("postgresql://"): ]

connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
engine = create_engine(
    database_url, future=True, pool_pre_ping=True, connect_args=connect_args,
    pool_size=5 if not database_url.startswith("sqlite") else 5,
    max_overflow=5 if not database_url.startswith("sqlite") else 0,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
