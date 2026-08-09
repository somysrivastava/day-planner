from datetime import datetime, timezone
from typing import Optional

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import User
from app.services.token_crypto import decrypt_token


# Builds a Credentials object holding only a refresh token. The
# google-api-python-client transport layer sees it has no valid access
# token and silently exchanges the refresh token for one before the
# first API call — we never handle that refresh step ourselves.
def _get_credentials_for_user(db: Session, user_id: int) -> Credentials:
    user = db.get(User, user_id)
    if not user or not user.google_refresh_token:
        raise ValueError(f"User {user_id} has not connected Google Calendar yet")

    return Credentials(
        token=None,
        refresh_token=decrypt_token(user.google_refresh_token),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
    )


def list_upcoming_events(db: Session, user_id: int, max_results: int = 10):
    creds = _get_credentials_for_user(db, user_id)
    service = build("calendar", "v3", credentials=creds)

    now = datetime.now(timezone.utc).isoformat()
    result = (
        service.events()
        .list(
            calendarId="primary",
            timeMin=now,
            maxResults=max_results,
            singleEvents=True,  # expand recurring events into individual instances
            orderBy="startTime",
        )
        .execute()
    )

    return result.get("items", [])


def create_event(
    db: Session,
    user_id: int,
    summary: str,
    description: str,
    start_datetime: str,
    end_datetime: str,
    time_zone: str,
    recurrence: Optional[list[str]] = None,
):
    creds = _get_credentials_for_user(db, user_id)
    service = build("calendar", "v3", credentials=creds)

    event_body = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start_datetime, "timeZone": time_zone},
        "end": {"dateTime": end_datetime, "timeZone": time_zone},
    }
    if recurrence:
        event_body["recurrence"] = recurrence  # e.g. ["RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"]

    return service.events().insert(calendarId="primary", body=event_body).execute()
