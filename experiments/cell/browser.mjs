#!/usr/bin/env node
// Runs the unmodified public items.spec.ts plus a real signup/login isolation probe.
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises"
import { createHash } from "node:crypto"
import { tmpdir } from "node:os"
import path from "node:path"
import { spawn } from "node:child_process"

function argument(name, fallback = undefined) {
  const index = process.argv.indexOf(name)
  return index < 0 ? fallback : process.argv[index + 1]
}

const source = path.resolve(argument("--source"))
const baseURL = argument("--base-url")
const output = path.resolve(argument("--output"))
if (!source || !baseURL || !output) throw new Error("--source, --base-url, and --output are required")

const frontend = path.join(source, "frontend")
const authFile = path.join(frontend, "playwright/.auth/user.json")
const reportDir = await mkdtemp(path.join(tmpdir(), "cell-playwright-"))
const reportFile = path.join(reportDir, "report.json")
const configFile = path.join(frontend, `.cell-playwright-${process.pid}.mjs`)
const config = `
import { defineConfig, devices } from "@playwright/test"
export default defineConfig({
  testDir: ${JSON.stringify(path.join(frontend, "tests"))},
  fullyParallel: false,
  retries: 0,
  reporter: [["json", { outputFile: ${JSON.stringify(reportFile)} }]],
  use: { baseURL: ${JSON.stringify(baseURL)}, trace: "off" },
  projects: [
    { name: "setup", testMatch: /auth\\.setup\\.ts/ },
    { name: "chromium", testMatch: /items\\.spec\\.ts/, use: { ...devices["Desktop Chrome"], storageState: ${JSON.stringify(authFile)} }, dependencies: ["setup"] },
  ],
})
`
await writeFile(configFile, config)

function run(command, cwd, env, timeoutMs = 120000) {
  return new Promise((resolve) => {
    const child = spawn(command[0], command.slice(1), { cwd, env, detached: true, stdio: ["ignore", "pipe", "pipe"] })
    let stdout = ""
    let stderr = ""
    child.stdout.on("data", (chunk) => { stdout += chunk })
    child.stderr.on("data", (chunk) => { stderr += chunk })
    const timer = setTimeout(() => {
      try { process.kill(-child.pid, "SIGKILL") } catch { /* already exited */ }
      resolve({ code: null, signal: "SIGKILL", timed_out: true, stdout, stderr })
    }, timeoutMs)
    child.on("close", (code, signal) => { clearTimeout(timer); resolve({ code, signal, timed_out: false, stdout, stderr }) })
  })
}

async function request(url, options = {}) {
  const response = await fetch(`${baseURL}${url}`, { ...options, signal: AbortSignal.timeout(5000) })
  let body = null
  try { body = await response.json() } catch { /* status is the evidence */ }
  return { status: response.status, body }
}

async function crossTenantProbe() {
  const suffix = `${Date.now()}-${process.pid}`
  const password = "CellE2EPassword123!"
  const emailA = `cell-a-${suffix}@example.com`
  const emailB = `cell-b-${suffix}@example.com`
  const signupA = await request("/api/v1/users/signup", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ email: emailA, password, full_name: "Cell A" }) })
  const signupB = await request("/api/v1/users/signup", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ email: emailB, password, full_name: "Cell B" }) })
  if (signupA.status !== 200 || signupB.status !== 200) throw new Error(`signup failed: ${signupA.status}/${signupB.status}`)
  async function login(email) {
    const body = new URLSearchParams({ username: email, password })
    const result = await request("/api/v1/login/access-token", { method: "POST", headers: { "content-type": "application/x-www-form-urlencoded" }, body })
    if (result.status !== 200 || !result.body?.access_token) throw new Error(`login failed: ${result.status}`)
    return result.body.access_token
  }
  const tokenA = await login(emailA)
  const tokenB = await login(emailB)
  const created = await request("/api/v1/items/", { method: "POST", headers: { authorization: `Bearer ${tokenA}`, "content-type": "application/json" }, body: JSON.stringify({ title: `cross-tenant-${suffix}`, description: "probe" }) })
  const itemId = created.body?.id
  if (created.status !== 200 || !itemId) throw new Error(`owner create failed: ${created.status}`)
  const denied = await request(`/api/v1/items/${itemId}`, { headers: { authorization: `Bearer ${tokenB}` } })
  const cleanup = await request(`/api/v1/items/${itemId}`, { method: "DELETE", headers: { authorization: `Bearer ${tokenA}` } })
  const passed = [404, 403].includes(denied.status) && cleanup.status === 200
  return { passed, signup: [signupA.status, signupB.status], login: [200, 200], owner_create: created.status, other_user_read: denied.status, owner_delete: cleanup.status }
}

async function adminPositiveControl() {
  const password = process.env.FIRST_SUPERUSER_PASSWORD
  const email = process.env.FIRST_SUPERUSER
  const body = new URLSearchParams({ username: email, password })
  const logged = await request("/api/v1/login/access-token", { method: "POST", headers: { "content-type": "application/x-www-form-urlencoded" }, body })
  if (logged.status !== 200 || !logged.body?.access_token) throw new Error(`admin login failed: ${logged.status}`)
  const headers = { authorization: `Bearer ${logged.body.access_token}`, "content-type": "application/json" }
  const title = `admin-positive-${Date.now()}-${process.pid}`
  const created = await request("/api/v1/items/", { method: "POST", headers, body: JSON.stringify({ title, description: "admin control" }) })
  const id = created.body?.id
  const updated = id ? await request(`/api/v1/items/${id}`, { method: "PUT", headers, body: JSON.stringify({ description: "admin updated" }) }) : { status: 0 }
  const read = id ? await request(`/api/v1/items/${id}`, { headers }) : { status: 0 }
  const deleted = id ? await request(`/api/v1/items/${id}`, { method: "DELETE", headers }) : { status: 0 }
  const passed = created.status === 200 && updated.status === 200 && read.status === 200 && deleted.status === 200
  return { passed, login: logged.status, create: created.status, update: updated.status, read: read.status, delete: deleted.status }
}

function sanitize(value) {
  return String(value).replaceAll(source, "$SOURCE").replace(/eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/g, "<fixture-token>").slice(-4000)
}

function casesIn(suites, cases = []) {
  for (const suite of suites ?? []) {
    for (const spec of suite.specs ?? []) {
      for (const test of spec.tests ?? []) {
        const last = test.results?.at(-1) ?? {}
        cases.push({ file: test.file ?? spec.file ?? "", title: spec.title, status: last.status ?? "missing" })
      }
    }
    casesIn(suite.suites, cases)
  }
  return cases
}

const started = performance.now()
const initial = { status: "unknown", phase: "before_startup", source: "$SOURCE", base_url: baseURL }
await writeFile(output, JSON.stringify(initial, null, 2) + "\n")
let report = initial
try {
  const allowed = {
    PATH: process.env.PATH ?? "/usr/bin:/bin",
    LANG: "C",
    HOME: process.env.HOME ?? "/tmp",
    VITE_API_URL: baseURL,
    PLAYWRIGHT_BASE_URL: baseURL,
    PLAYWRIGHT_BROWSERS_PATH: process.env.PLAYWRIGHT_BROWSERS_PATH ?? "",
    FIRST_SUPERUSER: process.env.FIRST_SUPERUSER ?? "",
    FIRST_SUPERUSER_PASSWORD: process.env.FIRST_SUPERUSER_PASSWORD ?? "",
  }
  const result = await run([process.execPath, path.join(source, "node_modules/@playwright/test/cli.js"), "test", "--config", configFile, "--project=chromium"], frontend, allowed)
  const browser = await readFile(reportFile).then((value) => JSON.parse(value.toString())).catch(() => ({}))
  const stats = browser.stats ?? {}
  const cases = casesIn(browser.suites)
  const itemCases = cases.filter((test) => test.file.endsWith("items.spec.ts"))
  const authCases = cases.filter((test) => test.file.endsWith("auth.setup.ts"))
  const expected = Number(stats.expected ?? 0)
  const inventory = { item_tests: itemCases.length, auth_setup_tests: authCases.length, total: cases.length, expected_passed_stat: expected }
  const probe = await crossTenantProbe().catch((error) => ({ passed: false, error: sanitize(error) }))
  const admin = await adminPositiveControl().catch((error) => ({ passed: false, error: sanitize(error) }))
  const passedCount = cases.filter((test) => test.status === "passed").length
  const nestedPass = cases.length === 10 && itemCases.length === 9 && authCases.length === 1 && passedCount === 10
  const passed = result.code === 0 && nestedPass && expected === cases.length && !stats.unexpected && !stats.flaky && !stats.skipped && probe.passed && admin.passed
  report = {
    status: passed ? "passed" : "failed",
    exit_code: result.code,
    timed_out: result.timed_out,
    auth_setup: { included: true, mock_auth: false, method: "real UI login against app" },
    inventory,
    cases,
    tests: { expected, actual_passed: passedCount, unexpected: stats.unexpected ?? null, flaky: stats.flaky ?? null, skipped: stats.skipped ?? null },
    cross_tenant: probe,
    admin_positive_control: admin,
    elapsed_seconds: (performance.now() - started) / 1000,
    stdout_sha256: createHash("sha256").update(result.stdout).digest("hex"),
    stderr_tail: sanitize(result.stderr),
  }
} catch (error) {
  report = { ...initial, status: "failed", phase: "startup_or_browser_error", error: sanitize(error), elapsed_seconds: (performance.now() - started) / 1000 }
} finally {
  await writeFile(output, JSON.stringify(report, null, 2) + "\n")
  await rm(configFile, { force: true })
  await rm(reportDir, { recursive: true, force: true })
}
console.log(JSON.stringify(report))
process.exitCode = report.status === "passed" ? 0 : 1
