require('dotenv').config();
const pool = require('../src/db/pool');

// Anchor dates for recurring events: DTSTART needs a concrete first
// occurrence, even though RRULE's BYDAY controls which weekdays actually
// repeat. Using the next Mon/Tue from today keeps these dates sane.
const MONDAY = '2026-08-10';
const TUESDAY = '2026-08-11';
const TZ_OFFSET = '+05:30'; // Asia/Kolkata, fixed offset (no DST)

const WEEKDAYS_RULE = 'FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR';
const TUE_THU_RULE = 'FREQ=WEEKLY;BYDAY=TU,TH';

function ts(date, time) {
  return `${date}T${time}:00${TZ_OFFSET}`;
}

async function upsertUser(phone, timezone) {
  const { rows } = await pool.query(
    `INSERT INTO users (phone_number, timezone)
     VALUES ($1, $2)
     ON CONFLICT (phone_number) DO UPDATE SET timezone = EXCLUDED.timezone
     RETURNING id`,
    [phone, timezone]
  );
  return rows[0].id;
}

// Scoped to type='fixed_event' so re-running this script never touches
// tasks/reminders that other flows may add to these same users later.
async function resetFixedEvents(userId) {
  await pool.query(`DELETE FROM items WHERE user_id = $1 AND type = 'fixed_event'`, [userId]);
}

async function addFixedEvent(userId, title, startTs, endTs, recurrenceRule) {
  await pool.query(
    `INSERT INTO items (user_id, type, title, start_time, end_time, recurrence_rule)
     VALUES ($1, 'fixed_event', $2, $3, $4, $5)`,
    [userId, title, startTs, endTs, recurrenceRule]
  );
}

async function main() {
  // --- Real user: your actual routine ---
  const you = await upsertUser('+919354211791', 'Asia/Kolkata');
  await resetFixedEvents(you);
  await addFixedEvent(you, 'Gym', ts(MONDAY, '05:00'), ts(MONDAY, '07:30'), WEEKDAYS_RULE);
  await addFixedEvent(you, 'Office', ts(MONDAY, '10:00'), ts(MONDAY, '19:00'), WEEKDAYS_RULE);
  console.log(`Seeded user ${you} (+919354211791): Gym + Office, Mon-Fri`);

  // --- Synthetic user A: sparse day, lots of free time ---
  const sparse = await upsertUser('+910000000001', 'Asia/Kolkata');
  await resetFixedEvents(sparse);
  await addFixedEvent(sparse, 'Morning Standup', ts(MONDAY, '09:00'), ts(MONDAY, '09:15'), WEEKDAYS_RULE);
  console.log(`Seeded user ${sparse} (+910000000001, sparse day): 1 short daily event`);

  // --- Synthetic user B: tightly packed, back-to-back, no gaps ---
  const packed = await upsertUser('+910000000002', 'Asia/Kolkata');
  await resetFixedEvents(packed);
  const packedBlocks = [
    ['Client Calls', '08:00', '10:00'],
    ['Project Work Block', '10:00', '12:30'],
    ['Lunch', '12:30', '13:00'],
    ['Team Meetings', '13:00', '15:30'],
    ['Deep Work Block', '15:30', '18:00'],
    ['Gym', '18:00', '19:30'],
  ];
  for (const [title, start, end] of packedBlocks) {
    await addFixedEvent(packed, title, ts(MONDAY, start), ts(MONDAY, end), WEEKDAYS_RULE);
  }
  console.log(`Seeded user ${packed} (+910000000002, tightly packed): ${packedBlocks.length} back-to-back blocks`);

  // --- Synthetic user C: no fixed morning routine at all ---
  const noMorning = await upsertUser('+910000000003', 'Asia/Kolkata');
  await resetFixedEvents(noMorning);
  await addFixedEvent(noMorning, 'Evening Class', ts(TUESDAY, '18:30'), ts(TUESDAY, '20:00'), TUE_THU_RULE);
  console.log(`Seeded user ${noMorning} (+910000000003, no morning routine): 1 evening event, Tue/Thu only`);

  await pool.end();
}

main().catch((err) => {
  console.error('Seed script failed:', err.message);
  process.exit(1);
});
