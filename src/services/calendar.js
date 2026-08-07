const { google } = require('googleapis');
const pool = require('../db/pool');
const { createOAuthClient } = require('./googleAuth');

// Builds an OAuth2 client pre-loaded with this user's refresh token.
// The googleapis library uses it to silently mint a fresh access token
// before each API call — we never handle that refresh step ourselves.
async function getAuthorizedClientForUser(userId) {
  const { rows } = await pool.query(
    'SELECT google_refresh_token FROM users WHERE id = $1',
    [userId]
  );
  const refreshToken = rows[0]?.google_refresh_token;

  if (!refreshToken) {
    throw new Error(`User ${userId} has not connected Google Calendar yet`);
  }

  const oauth2Client = createOAuthClient();
  oauth2Client.setCredentials({ refresh_token: refreshToken });
  return oauth2Client;
}

async function listUpcomingEvents(userId, maxResults = 10) {
  const auth = await getAuthorizedClientForUser(userId);
  const calendar = google.calendar({ version: 'v3', auth });

  const { data } = await calendar.events.list({
    calendarId: 'primary',
    timeMin: new Date().toISOString(),
    maxResults,
    singleEvents: true, // expand recurring events into individual instances
    orderBy: 'startTime',
  });

  return data.items;
}

async function createEvent(userId, { summary, description, startDateTime, endDateTime, timeZone }) {
  const auth = await getAuthorizedClientForUser(userId);
  const calendar = google.calendar({ version: 'v3', auth });

  const { data } = await calendar.events.insert({
    calendarId: 'primary',
    requestBody: {
      summary,
      description,
      start: { dateTime: startDateTime, timeZone },
      end: { dateTime: endDateTime, timeZone },
    },
  });

  return data;
}

module.exports = { listUpcomingEvents, createEvent };
