from google_auth_oauthlib.flow import Flow

from app.config import settings

# Locally this can stay the default localhost value (Desktop-type OAuth
# clients accept any localhost redirect without pre-registering it). Once
# deployed, GOOGLE_REDIRECT_URI must be set to the real HTTPS URL and that
# exact value added to the OAuth client's "Authorized redirect URIs" in
# Google Cloud Console - unlike localhost, non-localhost redirect URIs are
# not accepted unless explicitly registered.
REDIRECT_URI = settings.google_redirect_uri

# Full read/write access to calendars — we need write access to create events.
SCOPES = ["https://www.googleapis.com/auth/calendar"]

CLIENT_CONFIG = {
    "installed": {
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": [REDIRECT_URI],
    }
}


# google-auth-oauthlib enables PKCE by default: authorization_url() generates
# a random code_verifier and sends its hash to Google, and fetch_token() must
# later present that same code_verifier to complete the exchange. Since
# /auth/google and /auth/google/callback are separate stateless HTTP requests
# (each building its own Flow object), we bridge the gap by stashing the
# verifier here, keyed by `state`, between the two requests.
# In-memory only — fine for a single-process dev server; a multi-worker
# deployment would need shared storage (Redis, DB) instead.
_pending_code_verifiers: dict[str, str] = {}


def _build_flow(code_verifier: str | None = None) -> Flow:
    return Flow.from_client_config(
        CLIENT_CONFIG,
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
        code_verifier=code_verifier,
    )


# `state` carries our own user id through the round trip to Google and back,
# so the callback knows which user row to attach the refresh token to.
def get_auth_url(user_id: int) -> str:
    flow = _build_flow()
    auth_url, _ = flow.authorization_url(
        access_type="offline",  # required to receive a refresh token, not just an access token
        prompt="consent",  # forces Google to re-issue a refresh token even on repeat authorizations
        state=str(user_id),
    )
    _pending_code_verifiers[str(user_id)] = flow.code_verifier
    return auth_url


def get_refresh_token_from_code(code: str, state: str) -> str | None:
    code_verifier = _pending_code_verifiers.pop(state, None)
    flow = _build_flow(code_verifier=code_verifier)
    flow.fetch_token(code=code)
    return flow.credentials.refresh_token
