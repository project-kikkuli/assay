// Adapted from ../outbox/consumer.ts, the public MIT fixture.
// This file intentionally preserves the original pre-lock processed check.
import readline from "node:readline";
import pg from "../outbox/node_modules/pg/lib/index.js";

const { Client } = pg;
const eventId = process.argv[2];
const barrier = process.argv[3] ?? "";
if (!eventId) throw new Error("usage: consumer.ts EVENT_ID [BARRIER]");

const client = new Client();
const emit = (phase: string, extra: Record<string, unknown> = {}) => {
  process.stdout.write(`${JSON.stringify({ phase, ...extra })}\n`);
};
const waitForController = () =>
  new Promise<void>((resolve) => {
    const input = readline.createInterface({ input: process.stdin });
    input.once("line", () => {
      input.close();
      resolve();
    });
  });
const barrierAt = async (name: string) => {
  if (barrier !== name) return;
  emit(name);
  await waitForController();
};

let outcome = "committed";
let errorCode: string | null = null;
try {
  await client.connect();
  emit("connected", { backend_pid: client.processID });
  const seen = await client.query(
    "SELECT 1 FROM processed_events WHERE event_id = $1",
    [eventId],
  );
  emit("precheck_done", { seen: seen.rowCount !== 0 });
  if (seen.rowCount) {
    outcome = "dedup_skipped";
  } else {
    await client.query("BEGIN");
    emit("waiting_for_row_lock");
    const locked = await client.query(
      "SELECT command_id FROM outbox_events WHERE event_id = $1 FOR UPDATE",
      [eventId],
    );
    if (locked.rowCount !== 1) throw new Error("outbox row missing");
    emit("row_lock_acquired");
    const commandId = locked.rows[0].command_id as string;
    await client.query(
      "INSERT INTO ledger_effects(command_id, effect) VALUES ($1, $2) ON CONFLICT (command_id) DO NOTHING",
      [commandId, "accepted"],
    );
    await client.query("INSERT INTO processed_events(event_id) VALUES ($1)", [
      eventId,
    ]);
    await client.query(
      "UPDATE outbox_events SET status = 'published' WHERE event_id = $1",
      [eventId],
    );
    await barrierAt("before_commit");
    await client.query("COMMIT");
    await barrierAt("after_commit");
  }
} catch (error) {
  await client.query("ROLLBACK").catch(() => undefined);
  errorCode = (error as { code?: string }).code ?? null;
  outcome = errorCode === "23505" ? "rolled_back_unique_violation" : "rolled_back";
}
emit("outcome", { outcome, error_code: errorCode });
await client.end();
