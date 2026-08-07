require('dotenv').config();
const pool = require('../src/db/pool');
const { listUpcomingEvents, createEvent } = require('../src/services/calendar');

const USER_ID = 1;

async function main() {
  console.log('Fetching your upcoming events...\n');
  const events = await listUpcomingEvents(USER_ID, 10);

  if (events.length === 0) {
    console.log('No upcoming events found.');
  } else {
    for (const event of events) {
      const start = event.start.dateTime || event.start.date;
      console.log(`- ${event.summary}  (${start})`);
    }
  }

  console.log('\nCreating a test event one hour from now...');
  const { rows } = await pool.query('SELECT timezone FROM users WHERE id = $1', [USER_ID]);
  const timeZone = rows[0].timezone;

  const start = new Date(Date.now() + 60 * 60 * 1000);
  const end = new Date(start.getTime() + 30 * 60 * 1000);

  const created = await createEvent(USER_ID, {
    summary: 'Day Planner test event',
    description: 'Created by scripts/test-calendar.js to verify write access.',
    startDateTime: start.toISOString(),
    endDateTime: end.toISOString(),
    timeZone,
  });

  console.log(`Created: ${created.summary}`);
  console.log(`View it: ${created.htmlLink}`);

  await pool.end();
}

main().catch((err) => {
  console.error('Test script failed:', err.message);
  process.exit(1);
});
