import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { Context, Data, Effect, Layer } from "effect";

const MAX_DIAGNOSTIC_LENGTH = 240;
const REQUEST_TIMEOUT_MS = 10_000;

export type WorkerArgs = {
  api: string;
  provider: string;
  token: string;
};

type JsonObject = Record<string, unknown>;

export type ClaimedJob = {
  id: string;
  reservation_id: string;
  tenant: string;
  quantity: number;
  delivery_key: string;
  fence: number;
};

type WorkerSuccess =
  | { kind: "idle" }
  | { kind: "fulfilled"; reservation_id: string; delivery_key: string };

type WorkerFailure = {
  kind: "error";
  code: string;
  stage: string;
  status?: number;
  detail: string;
};

export class WorkerError extends Data.TaggedError("WorkerError")<{
  code: string;
  stage: string;
  detail: string;
  status?: number;
}> {}

type HttpTransport = {
  request: (
    url: string,
    init: RequestInit,
  ) => Effect.Effect<Response, WorkerError>;
};

export const HttpTransport = Context.GenericTag<HttpTransport>(
  "continuum/HttpTransport",
);

const HttpTransportLive = Layer.succeed(HttpTransport, {
  request: (url: string, init: RequestInit) =>
    Effect.tryPromise({
      try: () => fetch(url, { ...init, signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS) }),
      catch: (cause) =>
        new WorkerError({
          code: "network_error",
          stage: "http",
          detail: boundedText(cause instanceof Error ? cause.message : String(cause)),
        }),
    }),
});

function boundedText(value: string): string {
  const singleLine = value.replace(/[\u0000-\u001f\u007f\n\r\t]+/g, " ").trim();
  return singleLine.length <= MAX_DIAGNOSTIC_LENGTH
    ? singleLine
    : `${singleLine.slice(0, MAX_DIAGNOSTIC_LENGTH - 1)}…`;
}

function asObject(value: unknown, stage: string): JsonObject {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new WorkerError({
      code: "invalid_response",
      stage,
      detail: "response must be a JSON object",
    });
  }
  return value as JsonObject;
}

function requiredString(value: unknown, field: string, stage: string): string {
  if (typeof value !== "string" || value.length === 0 || value.length > 512) {
    throw new WorkerError({
      code: "invalid_response",
      stage,
      detail: `${field} must be a nonempty bounded string`,
    });
  }
  return value;
}

function requiredInteger(value: unknown, field: string, stage: string): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value)) {
    throw new WorkerError({
      code: "invalid_response",
      stage,
      detail: `${field} must be an integer`,
    });
  }
  return value;
}

function joinUrl(base: string, path: string): string {
  const normalizedBase = base.endsWith("/") ? base : `${base}/`;
  return new URL(path.replace(/^\//, ""), normalizedBase).toString();
}

function parseUrl(value: string, flag: string): string {
  try {
    const parsed = new URL(value);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
      throw new Error("unsupported protocol");
    }
    return value;
  } catch {
    throw new WorkerError({
      code: "usage",
      stage: "arguments",
      detail: `${flag} must be an http or https URL`,
    });
  }
}

export function parseArgs(argv: readonly string[]): WorkerArgs {
  const values = new Map<string, string>();
  let once = false;
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === "--once") {
      once = true;
      continue;
    }
    if (argument !== "--api" && argument !== "--provider" && argument !== "--token") {
      throw new WorkerError({ code: "usage", stage: "arguments", detail: "expected --api, --provider, --token, and --once" });
    }
    const value = argv[index + 1];
    if (!value || value.startsWith("--")) {
      throw new WorkerError({ code: "usage", stage: "arguments", detail: `${argument} needs a value` });
    }
    values.set(argument, value);
    index += 1;
  }
  if (!once) {
    throw new WorkerError({ code: "usage", stage: "arguments", detail: "--once is required" });
  }
  const api = values.get("--api");
  const provider = values.get("--provider");
  const token = values.get("--token");
  if (!api || !provider || !token) {
    throw new WorkerError({ code: "usage", stage: "arguments", detail: "all worker arguments are required" });
  }
  return { api: parseUrl(api, "--api"), provider: parseUrl(provider, "--provider"), token };
}

function jsonBody(value: unknown, stage: string): string {
  try {
    const serialized = JSON.stringify(value);
    if (serialized === undefined) throw new Error("body is undefined");
    return serialized;
  } catch {
    throw new WorkerError({ code: "request_error", stage, detail: "request body is not serializable" });
  }
}

function safeSync<T>(thunk: () => T, stage: string): Effect.Effect<T, WorkerError> {
  return Effect.try({
    try: thunk,
    catch: (cause) =>
      cause instanceof WorkerError
        ? cause
        : new WorkerError({
            code: "request_error",
            stage,
            detail: boundedText(cause instanceof Error ? cause.message : String(cause)),
          }),
  });
}

function requestJson(
  transport: HttpTransport,
  url: string,
  stage: string,
  token: string | undefined,
  body: unknown,
): Effect.Effect<unknown, WorkerError> {
  const headers: Record<string, string> = { "content-type": "application/json" };
  if (token !== undefined) headers.authorization = `Bearer ${token}`;
  return Effect.gen(function* (_) {
    const response = yield* _(
      transport.request(url, {
        method: "POST",
        headers,
        body: yield* _(safeSync(() => jsonBody(body, stage), stage)),
      }),
    );
    const responseText = yield* _(
      Effect.tryPromise({
        try: () => response.text(),
        catch: (cause) =>
          new WorkerError({
            code: "invalid_response",
            stage,
            status: response.status,
            detail: boundedText(cause instanceof Error ? cause.message : String(cause)),
          }),
      }),
    );
    const parsed = yield* _(
      Effect.try({
        try: () => JSON.parse(responseText) as unknown,
        catch: () =>
          new WorkerError({
            code: "invalid_response",
            stage,
            status: response.status,
            detail: "response was not JSON",
          }),
      }),
    );
    if (!response.ok) {
      return yield* _(
        Effect.fail(
          new WorkerError({
            code: response.status === 409 ? "conflict" : "http_error",
            stage,
            status: response.status,
            detail: boundedText(JSON.stringify(parsed)),
          }),
        ),
      );
    }
    return parsed;
  });
}

function parseClaim(value: unknown): ClaimedJob | null {
  const object = asObject(value, "claim");
  if (object.job === null) return null;
  const job = asObject(object.job, "claim");
  const quantity = requiredInteger(job.quantity, "quantity", "claim");
  const fence = requiredInteger(job.fence, "fence", "claim");
  if (quantity < 1 || quantity > 8 || fence < 1) {
    throw new WorkerError({ code: "invalid_response", stage: "claim", detail: "claim contains out-of-range values" });
  }
  return {
    id: requiredString(job.id, "id", "claim"),
    reservation_id: requiredString(job.reservation_id, "reservation_id", "claim"),
    tenant: requiredString(job.tenant, "tenant", "claim"),
    quantity,
    delivery_key: requiredString(job.delivery_key, "delivery_key", "claim"),
    fence,
  };
}

function parseProviderReceipt(value: unknown): string {
  const object = asObject(value, "provider");
  return requiredString(object.receipt, "receipt", "provider");
}

export function runOnce(args: WorkerArgs): Effect.Effect<WorkerSuccess, WorkerError, HttpTransport> {
  return Effect.gen(function* (_) {
    const transport = yield* _(HttpTransport);
    const claimPayload = yield* _(
      requestJson(transport, joinUrl(args.api, "/worker/claim"), "claim", args.token, {}),
    );
    const claimed = yield* _(safeSync(() => parseClaim(claimPayload), "claim"));
    if (claimed === null) return { kind: "idle" as const };

    const providerPayload = yield* _(
      requestJson(transport, joinUrl(args.provider, "/deliver"), "provider", undefined, {
        key: claimed.delivery_key,
        tenant: claimed.tenant,
        reservation_id: claimed.reservation_id,
        quantity: claimed.quantity,
      }),
    );
    const receipt = yield* _(safeSync(() => parseProviderReceipt(providerPayload), "provider"));

    yield* _(
      requestJson(transport, joinUrl(args.api, "/worker/ack"), "ack", args.token, {
        id: claimed.id,
        fence: claimed.fence,
        receipt,
      }),
    );
    return {
      kind: "fulfilled" as const,
      reservation_id: claimed.reservation_id,
      delivery_key: claimed.delivery_key,
    };
  });
}

function output(value: unknown): void {
  process.stdout.write(`${JSON.stringify(value)}\n`);
}

export async function main(argv = process.argv.slice(2)): Promise<void> {
  try {
    const args = parseArgs(argv);
    const result = await Effect.runPromise(
      runOnce(args).pipe(
        Effect.provide(HttpTransportLive),
        Effect.catchTag(
          "WorkerError",
          (cause): Effect.Effect<WorkerFailure> =>
            Effect.succeed({
              kind: "error",
              code: cause.code,
              stage: cause.stage,
              ...(cause.status === undefined ? {} : { status: cause.status }),
              detail: boundedText(cause.detail),
            }),
        ),
      ),
    );
    output(result);
    if (result.kind === "error") process.exitCode = 1;
  } catch (cause) {
    if (cause instanceof WorkerError) {
      output({
        kind: "error",
        code: cause.code,
        stage: cause.stage,
        ...(cause.status === undefined ? {} : { status: cause.status }),
        detail: boundedText(cause.detail),
      });
    } else {
      output({ kind: "error", code: "internal_error", stage: "worker", detail: "unexpected worker failure" });
    }
    process.exitCode = 1;
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  await main();
}
