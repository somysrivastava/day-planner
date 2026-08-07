from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.base import Base, engine, get_db
from app.db.models import Item, User  # noqa: F401 (registers models with Base)
from app.routers import auth

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Day Planner")
app.include_router(auth.router, prefix="/auth", tags=["auth"])


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
