from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.base import Base, engine, get_db
from app.db.models import Item, User  # noqa: F401 (registers models with Base)
from app.routers import auth
from app.services.scheduler_jobs import get_scheduler

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Day Planner")
app.include_router(auth.router, prefix="/auth", tags=["auth"])


@app.on_event("startup")
def _start_scheduler() -> None:
    # get_scheduler() is a lazy singleton - previously it only got
    # instantiated (and its persisted jobs resumed) the first time some
    # orchestrator code path happened to call it. On a freshly started
    # process (every deploy, every restart) that meant nudges/check-ins
    # already sitting in the SQLAlchemyJobStore from before the restart
    # would NOT resume firing until the first unrelated orchestrator call
    # incidentally started the scheduler - not guaranteed to happen promptly,
    # or at all, if nothing calls into orchestrator in the meantime. Starting
    # it explicitly here guarantees persisted jobs resume as soon as the
    # process comes up.
    get_scheduler()


@app.get("/health")
def health_check(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        return {"status": "ok", "db": "connected"}
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={"status": "error", "db": "unreachable"},
        )
