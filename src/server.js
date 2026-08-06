require('dotenv').config();
const express = require('express');
const pool = require('./db/pool');

const app = express();
app.use(express.json());

app.get('/health', async (req, res) => {
  try {
    await pool.query('SELECT 1');
    res.json({ status: 'ok', db: 'connected' });
  } catch (err) {
    console.error('Health check DB failure:', err.message);
    res.status(500).json({ status: 'error', db: 'unreachable' });
  }
});

const port = process.env.PORT || 3000;
app.listen(port, () => {
  console.log(`day-planner server listening on port ${port}`);
});
