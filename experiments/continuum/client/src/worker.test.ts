import assert from "node:assert/strict";
import { test } from "node:test";
import { Effect, Layer } from "effect";
import {
  HttpTransport,
  WorkerError,
  parseArgs,
  runOnce,
  type ClaimedJob,
} from "./worker.js";

const job: ClaimedJob = {
  id: "job-1",
  reservation_id: "reservation-1",
  tenant: "alpha",
  quantity: 2,
  delivery_key: "delivery-1",
  fence: 3,
};

function fakeTransport(responses: Response[]) {
  return Layer.succeed(HttpTransport, {
    request: () => Effect.succeed(responses.shift() ?? new Response("{}", { status: 500 })),
  });
}

test("parses the one-pass worker invocation", () => {
  assert.deepEqual(
    parseArgs(["--api", "http://api", "--provider", "http://provider", "--token", "synthetic-worker", "--once"]),
    { api: "http://api", provider: "http://provider", token: "synthetic-worker" },
  );
  assert.throws(() => parseArgs(["--api", "http://api"]), WorkerError);
});

test("claims, delivers once, and acknowledges with the current fence", async () => {
  const responses = [
    new Response(JSON.stringify({ job }), { status: 200 }),
    new Response(JSON.stringify({ receipt: "receipt:delivery-1" }), { status: 200 }),
    new Response(JSON.stringify({ ok: true }), { status: 200 }),
  ];
  const result = await Effect.runPromise(
    runOnce({ api: "http://api", provider: "http://provider", token: "synthetic-worker" }).pipe(
      Effect.provide(fakeTransport(responses)),
    ),
  );
  assert.deepEqual(result, {
    kind: "fulfilled",
    reservation_id: "reservation-1",
    delivery_key: "delivery-1",
  });
  assert.equal(responses.length, 0);
});

test("surfaces a stale fenced acknowledgement as a failure", async () => {
  const responses = [
    new Response(JSON.stringify({ job }), { status: 200 }),
    new Response(JSON.stringify({ receipt: "receipt:delivery-1" }), { status: 200 }),
    new Response(JSON.stringify({ error: "stale fence" }), { status: 409 }),
  ];
  const result = await Effect.runPromise(
    runOnce({ api: "http://api", provider: "http://provider", token: "synthetic-worker" }).pipe(
      Effect.provide(fakeTransport(responses)),
      Effect.catchTag("WorkerError", (error) =>
        Effect.succeed({ code: error.code, stage: error.stage, status: error.status }),
      ),
    ),
  );
  assert.deepEqual(result, { code: "conflict", stage: "ack", status: 409 });
});
