const express = require('express');
const router = express.Router();
const pool = require('../db/pool');
const { getAuthUrl, getTokensFromCode } = require('../services/googleAuth');

// Visit /auth/google?user_id=1 to start the flow.
router.get('/google', (req, res) => {
  const userId = req.query.user_id;
  if (!userId) {
    return res.status(400).send('Missing user_id query param');
  }
  res.redirect(getAuthUrl(userId));
});

// Google redirects here after the user approves (or denies) access.
router.get('/google/callback', async (req, res) => {
  const { code, state, error } = req.query;

  if (error) {
    return res.status(400).send(`Google returned an error: ${error}`);
  }

  try {
    const tokens = await getTokensFromCode(code);

    if (!tokens.refresh_token) {
      return res
        .status(400)
        .send(
          'No refresh token returned. Revoke access at https://myaccount.google.com/permissions and try again.'
        );
    }

    const userId = Number(state);
    await pool.query('UPDATE users SET google_refresh_token = $1 WHERE id = $2', [
      tokens.refresh_token,
      userId,
    ]);

    res.send('Google Calendar connected. You can close this tab.');
  } catch (err) {
    console.error('OAuth callback error:', err.message);
    res.status(500).send(`OAuth exchange failed: ${err.message}`);
  }
});

module.exports = router;
