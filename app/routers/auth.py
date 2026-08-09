from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.db.base import get_db
from app.db.models import User
from app.services.google_auth import get_auth_url, get_refresh_token_from_code
from app.services.token_crypto import encrypt_token

router = APIRouter()


# Visit /auth/google?user_id=1 to start the flow.
@router.get("/google")
def start_google_auth(user_id: int = Query(...)):
    return RedirectResponse(get_auth_url(user_id))


# Google redirects here after the user approves (or denies) access.
@router.get("/google/callback")
def google_auth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
):
    if error:
        raise HTTPException(status_code=400, detail=f"Google returned an error: {error}")

    refresh_token = get_refresh_token_from_code(code, state)

    if not refresh_token:
        raise HTTPException(
            status_code=400,
            detail="No refresh token returned. Revoke access at https://myaccount.google.com/permissions and try again.",
        )

    user_id = int(state)
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail=f"User {user_id} not found")

    user.google_refresh_token = encrypt_token(refresh_token)
    db.commit()

    return PlainTextResponse("Google Calendar connected. You can close this tab.")
