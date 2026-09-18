// probe.mts — deterministic scheduler probe for the signup forced-navigation race.
//
// Reproduces the helper's exact final operations directly (not imported):
//   frontend/tests/utils/user.ts:15 `await ...click()` then :16 `await page.goto("/login")`
// Application-driven navigation under test:
//   frontend/src/hooks/useAuth.ts:32-34 signUpMutation.onSuccess -> navigate({ to: "/login" })
//
// Scheduling is event-driven via deferred promises + page.route hold. No
// wall-clock sleeps; setTimeout is used only as an outer infra bound.
// Original's forced goto runs only after the held signup request is observed,
// so the scheduler orchestrates a real admissible ordering rather than guessing.
// After release the held request is committed with route.fetch()+route.fulfill(),
// guaranteeing a real backend request. Navigation-abort is recorded, not hidden.
import { chromium, expect } from "@playwright/test";

type Event = { seq: number; case: string; event: string };

const FRONTEND = (process.env.ASSAY_API ?? "").replace(/\/$/, "");
const API = (process.env.ASSAY_API ?? "").replace(/\/$/, "");
const FIXTURE_PASSWORD = "AssayProbe12345";
const OUTER_MS = 30000;

class InfraError extends Error {}

function deferred<T = void>() {
  let resolve!: (v: T | PromiseLike<T>) => void;
  let reject!: (e?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res as (v: T | PromiseLike<T>) => void;
    reject = rej;
  });
  return { promise, resolve, reject };
}

// Outer bound only: distinguishes infra_unknown from assertion results.
function withTimeout<T>(p: Promise<T>, ms: number, label: string): Promise<T> {
  let t: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_, rej) => {
    t = setTimeout(() => rej(new InfraError(`timeout: ${label}`)), ms);
  });
  return Promise.race([p, timeout]).finally(() => clearTimeout(t));
}

const uniqEmail = (tag: string) =>
  `assay_${tag}_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}@example.com`;

async function apiCanLogin(email: string, password: string): Promise<{ ok: boolean; status: number | null }> {
  try {
    const res = await withTimeout(
      fetch(`${API}/api/v1/login/access-token`, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({ username: email, password }).toString(),
      }),
      OUTER_MS,
      "api-login",
    );
    return { ok: res.ok, status: res.status };
  } catch (e) {
    if (e instanceof InfraError) throw e;
    return { ok: false, status: null };
  }
}

function sanitizeErr(e: unknown): string {
  const s = e instanceof Error ? e.message : String(e);
  return s.replace(/https?:\/\/\S+/g, "[url]").slice(0, 200);
}

function pathnameOf(url: string): string {
  try {
    return new URL(url).pathname;
  } catch {
    return url;
  }
}

async function fillSignup(page: import("@playwright/test").Page, name: string, email: string, password: string) {
  await page.goto(`${FRONTEND}/signup`, { waitUntil: "domcontentloaded", timeout: OUTER_MS });
  await page.getByTestId("full-name-input").fill(name);
  await page.getByTestId("email-input").fill(email);
  await page.getByTestId("password-input").fill(password);
  await page.getByTestId("confirm-password-input").fill(password);
}

async function main() {
  if (!API) {
    console.log(JSON.stringify({ status: "infra_unknown", reason: "ASSAY_API missing", events: [] }));
    process.exitCode = 2;
    return;
  }
  const events: Event[] = [];
  let seq = 0;
  const log = (c: string, e: string) => events.push({ seq: seq++, case: c, event: e });

  const browser = await withTimeout(chromium.launch({ headless: true }), OUTER_MS, "launch").catch((e) => {
    if (e instanceof InfraError) {
      console.log(JSON.stringify({ status: "infra_unknown", reason: String(e), events }));
      process.exit(2);
    }
    throw e;
  });
  if (!browser) return;

  // ---- OLD case: helper's forced navigation (user.ts:15-16) ----
  const oldEmail = uniqEmail("old");
  let oldReturned = false;
  let oldStatus: number | null = null;
  let oldErr: string | null = null;
  let oldLogin: boolean | null = null;
  let oldLoginBeforeRelease: boolean | null = null;
  let prematureRecoveryStatus: number | null = null;
  let oldPath = "";

  // ---- FIXED case: await application-driven /login ----
  const fixedEmail = uniqEmail("fixed");
  let fixedOk = false;
  let fixedStatus: number | null = null;
  let fixedErr: string | null = null;
  let fixedLogin: boolean | null = null;

  try {
    // ===== OLD =====
    {
      const ctx = await browser.newContext();
      const page = await ctx.newPage();
      const gate = deferred<void>();
      const held = deferred<void>();
      const forwarded = deferred<void>();
      let done = false;
      const release = () => gate.resolve();
      try {
        await page.route("**/api/v1/users/signup", async (route) => {
          log("old", "signup_request_held");
          held.resolve();
          try {
            await gate.promise;
            log("old", "gate_released_forwarding");
            const res = await route.fetch();
            oldStatus = res.status();
            log("old", `forward_response_status_${res.status()}`);
            await route.fulfill({ response: res });
            done = true;
            log("old", "forward_fulfilled");
          } catch (e) {
            oldErr = sanitizeErr(e);
            log("old", "forward_error_observed");
          } finally {
            forwarded.resolve();
          }
        });
        await fillSignup(page, "Assay User", oldEmail, FIXTURE_PASSWORD);
        await page.getByRole("button", { name: "Sign Up" }).click();
        log("old", "clicked_signup");
        await withTimeout(held.promise, OUTER_MS, "old-held").catch((e) => {
          log("old", "held_never_observed");
          throw e;
        });
        log("old", "held_observed_before_schedule_action");
        // Exact helper final op (user.ts:16): forced navigation before commit.
        await page.goto(`${FRONTEND}/login`, { waitUntil: "domcontentloaded", timeout: OUTER_MS });
        oldPath = pathnameOf(page.url());
        const completedBeforeRelease = done || oldStatus !== null;
        oldReturned = oldPath.endsWith("/login") && !completedBeforeRelease;
        log("old", `forced_goto_done_path_${oldPath}_completed_before_release_${completedBeforeRelease}`);
        oldLoginBeforeRelease = (await apiCanLogin(oldEmail, FIXTURE_PASSWORD)).ok;
        log("old", `before_release_api_login_ok_${oldLoginBeforeRelease}`);
        const recovery = await fetch(`${API}/api/v1/password-recovery/${encodeURIComponent(oldEmail)}`, { method: "POST", signal: AbortSignal.timeout(OUTER_MS) });
        prematureRecoveryStatus = recovery.status;
        log("old", `premature_recovery_status_${recovery.status}`);
        release();
        await withTimeout(forwarded.promise, OUTER_MS, "old-forward").catch(() => {
          log("old", "forward_never_finished");
        });
        log("old", "release_settled");
      } finally {
        try {
          gate.resolve();
        } catch {}
        await page.unrouteAll({ behavior: "ignoreErrors" }).catch(() => {});
        await ctx.close().catch(() => {});
      }
    }
    // Registration ground truth via API (generic-200-no-email recovery check).
    try {
      const r = await apiCanLogin(oldEmail, FIXTURE_PASSWORD);
      oldLogin = r.ok;
      log("old", `api_login_ok_${r.ok}_status_${r.status}`);
    } catch (e) {
      if (e instanceof InfraError) throw e;
      oldLogin = null;
    }

    // ===== FIXED =====
    {
      const ctx = await browser.newContext();
      const page = await ctx.newPage();
      const gate = deferred<void>();
      const held = deferred<void>();
      const forwarded = deferred<void>();
      let stayedOnSignup = false;
      try {
        await page.route("**/api/v1/users/signup", async (route) => {
          log("fixed", "signup_request_held");
          held.resolve();
          try {
            await gate.promise;
            log("fixed", "gate_released_forwarding");
            const res = await route.fetch();
            fixedStatus = res.status();
            log("fixed", `forward_response_status_${res.status()}`);
            await route.fulfill({ response: res });
            log("fixed", "forward_fulfilled");
          } catch (e) {
            fixedErr = sanitizeErr(e);
            log("fixed", "forward_error_observed");
          } finally {
            forwarded.resolve();
          }
        });
        await fillSignup(page, "Assay User", fixedEmail, FIXTURE_PASSWORD);
        await page.getByRole("button", { name: "Sign Up" }).click();
        log("fixed", "clicked_signup");
        await withTimeout(held.promise, OUTER_MS, "fixed-held").catch((e) => {
          log("fixed", "held_never_observed");
          throw e;
        });
        stayedOnSignup = pathnameOf(page.url()).endsWith("/signup");
        log("fixed", `still_on_signup_while_held_${stayedOnSignup}`);
        gate.resolve();
        log("fixed", "gate_released");
        await withTimeout(page.waitForURL("**/login", { timeout: OUTER_MS }), OUTER_MS, "fixed-app-login").catch((e) => {
          log("fixed", "app_login_never_observed");
          throw e;
        });
        log("fixed", "app_driven_login_observed");
        await withTimeout(forwarded.promise, OUTER_MS, "fixed-forward").catch(() => {
          log("fixed", "forward_never_finished");
        });
        const okStatus = fixedStatus !== null && fixedStatus >= 200 && fixedStatus < 300;
        fixedOk = stayedOnSignup && okStatus && pathnameOf(page.url()).endsWith("/login");
        log("fixed", `awaited_success_${fixedOk}`);
      } finally {
        try {
          gate.resolve();
        } catch {}
        await page.unrouteAll({ behavior: "ignoreErrors" }).catch(() => {});
        await ctx.close().catch(() => {});
      }
    }
    try {
      const r = await apiCanLogin(fixedEmail, FIXTURE_PASSWORD);
      fixedLogin = r.ok;
      log("fixed", `api_login_ok_${r.ok}_status_${r.status}`);
    } catch (e) {
      if (e instanceof InfraError) throw e;
      fixedLogin = null;
    }

    const result = {
      status: oldReturned && fixedOk ? "counterexample_observed" : "assertion_failed",
      events,
      old: {
        returned_before_registration: oldReturned,
        url_path_after_forced_goto: oldPath,
        request_status: oldStatus,
        request_error: oldErr,
        actual_login: oldLogin,
        login_before_release: oldLoginBeforeRelease,
        recovery_before_registration_status: prematureRecoveryStatus,
      },
      fixed: {
        awaited_success: fixedOk,
        request_status: fixedStatus,
        request_error: fixedErr,
        actual_login: fixedLogin,
      },
      note: "old helper (user.ts:15-16) reaches /login before held signup commits; fixed stays /signup until release then follows app-driven /login (useAuth.ts:32-34). Ordering cause only; no never-flaky claim.",
    };
    console.log(JSON.stringify(result));

    // Strict assertions: fail loudly when the supposed counterexample is absent.
    expect(oldReturned, "old helper must return to /login before registration completes").toBe(true);
    expect(fixedOk, "fixed variant must stay on /signup until release then follow app-driven /login").toBe(true);
    expect(fixedLogin, "fixed-case user must verify login via API").toBe(true);
  } catch (e) {
    if (e instanceof InfraError) {
      console.log(JSON.stringify({ status: "infra_unknown", reason: String(e).slice(0, 200), events }));
      process.exitCode = 2;
      return;
    }
    throw e;
  } finally {
    await browser.close().catch(() => {});
  }
}

await withTimeout(main(), 120000, "outer").catch((e) => {
  if (e instanceof InfraError) {
    console.log(JSON.stringify({ status: "infra_unknown", reason: "outer timeout", events: [] }));
    process.exit(2);
  }
  throw e;
});
