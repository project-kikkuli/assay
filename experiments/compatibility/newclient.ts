import { Client } from "pg";

// New-version client: only knows documents.display_name.
// Prints one JSON line: {ok:true,...} or {ok:false,sqlstate,message} (exit 0).
// Usage: node dist/newclient.js <write|read|update> [args...]
async function main(): Promise<void> {
  const [op, a, b] = process.argv.slice(2);
  const client = new Client({
    host: process.env.PGHOST,
    port: Number(process.env.PGPORT),
    user: process.env.PGUSER,
    password: process.env.PGPASSWORD,
    database: process.env.PGDATABASE,
    connectionTimeoutMillis: 5000,
    statement_timeout: 5000,
  });
  await client.connect();
  try {
    if (op === "write") {
      const r = await client.query(
        "INSERT INTO documents(display_name) VALUES ($1) RETURNING id", [a]);
      console.log(JSON.stringify({ ok: true, id: String(r.rows[0].id) }));
    } else if (op === "read") {
      const r = await client.query(
        "SELECT display_name FROM documents WHERE id = $1", [a]);
      if (r.rowCount === 0) console.log(JSON.stringify({ ok: false, message: "row missing" }));
      else console.log(JSON.stringify({ ok: true, value: r.rows[0].display_name }));
    } else if (op === "update") {
      const r = await client.query(
        "UPDATE documents SET display_name = $2 WHERE id = $1", [a, b]);
      if (r.rowCount !== 1)
        console.log(JSON.stringify({ ok: false, message: `rowcount ${String(r.rowCount)}` }));
      else console.log(JSON.stringify({ ok: true, rowcount: 1 }));
    } else {
      throw new Error(`unknown op: ${op}`);
    }
  } catch (e: unknown) {
    const err = e as { code?: string; message?: string };
    console.log(JSON.stringify({
      ok: false,
      sqlstate: err.code,
      message: String(err.message ?? e).slice(0, 300),
    }));
  } finally {
    await client.end();
  }
}

main().catch((e) => { console.error(String(e).slice(0, 300)); process.exit(1); });
