import pg from "pg";

const { Client } = pg;
const variant = process.argv[2];
const eventId = process.argv[3];
const cutpoint = process.argv[4] ?? "";
if (!variant || !eventId) throw new Error("usage: consumer.ts VARIANT EVENT_ID [CUTPOINT]");

const requestedCommandId = eventId.replace(/^evt:/, "");
const client = new Client();
await client.connect();
let outcome = "committed";
let commandId = requestedCommandId;
function requireRowCount(actual: number | null, expected: number, operation: string): void {
  if (actual !== expected) throw new Error(`${operation} affected ${actual ?? "null"} rows; expected ${expected}`);
}
async function requireOutboxCommand(): Promise<string> {
  const result = await client.query(
    "SELECT command_id FROM outbox_events WHERE event_id = $1 FOR UPDATE",
    [eventId],
  );
  requireRowCount(result.rowCount, 1, "outbox lookup");
  return result.rows[0].command_id as string;
}
try {
  if (variant === "normal") {
    const seen = await client.query("SELECT 1 FROM processed_events WHERE event_id = $1", [eventId]);
    if (seen.rowCount) {
      outcome = "dedup_skipped";
    } else {
      await client.query("BEGIN");
      commandId = await requireOutboxCommand();
      await client.query("INSERT INTO ledger_effects(command_id, effect) VALUES ($1, $2) ON CONFLICT (command_id) DO NOTHING", [commandId, "accepted"]);
      await client.query("INSERT INTO processed_events(event_id) VALUES ($1)", [eventId]);
      const updated = await client.query("UPDATE outbox_events SET status = 'published' WHERE event_id = $1", [eventId]);
      requireRowCount(updated.rowCount, 1, "outbox publish");
      if (cutpoint === "before_commit") throw new Error("simulated crash at consumer before commit");
      await client.query("COMMIT");
    }
  } else if (variant === "missing-dedup") {
    await client.query("BEGIN");
    commandId = await requireOutboxCommand();
    await client.query("INSERT INTO ledger_effects(command_id, effect) VALUES ($1, $2)", [commandId, "accepted"]);
    await client.query("INSERT INTO processed_events(event_id) VALUES ($1) ON CONFLICT (event_id) DO NOTHING", [eventId]);
    const updated = await client.query("UPDATE outbox_events SET status = 'published' WHERE event_id = $1", [eventId]);
    requireRowCount(updated.rowCount, 1, "outbox publish");
    if (cutpoint === "before_commit") throw new Error("simulated crash at consumer before commit");
    await client.query("COMMIT");
  } else if (variant === "processed-before-ledger") {
    const seen = await client.query("SELECT 1 FROM processed_events WHERE event_id = $1", [eventId]);
    if (seen.rowCount) {
      outcome = "dedup_skipped";
    } else {
      await client.query("BEGIN");
      commandId = await requireOutboxCommand();
      await client.query("INSERT INTO processed_events(event_id) VALUES ($1)", [eventId]);
      await client.query("COMMIT");
      if (cutpoint === "after_processed_commit") {
        outcome = "simulated_crash_after_processed";
      } else {
        await client.query("BEGIN");
        await client.query("INSERT INTO ledger_effects(command_id, effect) VALUES ($1, $2) ON CONFLICT (command_id) DO NOTHING", [commandId, "accepted"]);
        const updated = await client.query("UPDATE outbox_events SET status = 'published' WHERE event_id = $1", [eventId]);
        requireRowCount(updated.rowCount, 1, "outbox publish");
        await client.query("COMMIT");
      }
    }
  } else {
    throw new Error(`unknown variant: ${variant}`);
  }
} catch (error) {
  await client.query("ROLLBACK").catch(() => undefined);
  const message = (error as Error).message;
  outcome = message === "outbox lookup affected 0 rows; expected 1" ? "rejected_missing_outbox" : `rolled_back:${message}`;
}
await client.end();
console.log(JSON.stringify({ variant, event_id: eventId, outcome }));
