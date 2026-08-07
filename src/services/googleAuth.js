const { google } = require('googleapis');

// Desktop-type OAuth clients can use any localhost redirect without
// pre-registering it in the Google Cloud Console.
const REDIRECT_URI = 'http://localhost:3000/auth/google/callback';

// Full read/write access to calendars — we need write access to create events.
const SCOPES = ['https://www.googleapis.com/auth/calendar'];

function createOAuthClient() {
  return new google.auth.OAuth2(
    process.env.GOOGLE_CLIENT_ID,
    process.env.GOOGLE_CLIENT_SECRET,
    REDIRECT_URI
  );
}

// `state` carries our own user id through the round trip to Google and back,
// so the callback knows which user row to attach the refresh token to.
function getAuthUrl(userId) {
  const oauth2Client = createOAuthClient();
  return oauth2Client.generateAuthUrl({
    access_type: 'offline', // required to receive a refresh token, not just an access token
    prompt: 'consent', // forces Google to re-issue a refresh token even on repeat authorizations
    scope: SCOPES,
    state: String(userId),
  });
}

async function getTokensFromCode(code) {
  const oauth2Client = createOAuthClient();
  const { tokens } = await oauth2Client.getToken(code);
  return tokens;
}

module.exports = { createOAuthClient, getAuthUrl, getTokensFromCode };
