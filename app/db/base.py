from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import settings

# Some managed Postgres providers (Render included) hand out connection
# strings using the legacy "postgres://" scheme. SQLAlchemy 2.0 rejects that
# scheme outright (NoSuchModuleError) - only "postgresql://" loads the
# dialect - so normalize it here rather than trusting the external string
# verbatim (verified directly: create_engine("postgres://...") raises).
_database_url = settings.database_url
if _database_url.startswith("postgres://"):
    _database_url = _database_url.replace("postgres://", "postgresql://", 1)

engine = create_engine(_database_url)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
