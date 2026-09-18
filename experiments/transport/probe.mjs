#!/usr/bin/env node

import http from "node:http"
import { createRequire } from "node:module"
import { resolve } from "node:path"
import process from "node:process"
import { writeFileSync } from "node:fs"
import { createHash } from "node:crypto"
import { readFileSync } from "node:fs"

const require = createRequire(import.meta.url)

function parseArgs(argv) {
  const options = {
    axiosRoot: "out/lab/fullstack/node_modules/axios",
    iterations: 5,
    output: null,
  }

  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index]
    if (argument === "--axios-root") {
      options.axiosRoot = argv[++index]
    } else if (argument === "--iterations") {
      options.iterations = Number(argv[++index])
    } else if (argument === "--output") {
      options.output = argv[++index]
      if (!options.output) throw new Error("--output requires a path")
    } else {
      throw new Error(`unknown argument: ${argument}`)
    }
  }

  if (!Number.isInteger(options.iterations) || options.iterations < 2) {
    throw new Error("--iterations must be an integer >= 2")
  }
  return options
}

function closeServer(server) {
  return new Promise((resolve, reject) => {
    server.close((error) => (error ? reject(error) : resolve()))
  })
}

async function runArm(axios, mode, keepAlive, options) {
  const connections = new Map()
  const requests = []
  let nextConnectionId = 1
  let totalRequests = 0

  const server = http.createServer((request, response) => {
    totalRequests += 1
    const connection = connections.get(request.socket)
    connection.requests += 1
    const observation = {
      request: totalRequests,
      connection: connection.id,
      connectionRequest: connection.requests,
      method: request.method,
      path: request.url,
    }
    requests.push(observation)

    request.resume()
    const shouldReset =
      (mode === "stale-keepalive" && connection.requests > 1) ||
      (mode === "server-pressure" && totalRequests > 1)
    if (shouldReset) {
      // Positive control: simulate the server retiring a connection without
      // returning an HTTP response. No client retry is performed.
      request.socket.destroy()
      return
    }

    response.writeHead(200, {
      "Content-Length": "2",
      Connection: "keep-alive",
    })
    response.end("ok")
  })

  server.on("connection", (socket) => {
    const connection = { id: nextConnectionId++, requests: 0 }
    connections.set(socket, connection)
    socket.once("close", () => connections.delete(socket))
  })

  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve))
  const address = server.address()
  const agent = new http.Agent({
    keepAlive,
    maxSockets: 1,
    maxFreeSockets: 1,
    scheduling: "lifo",
  })
  const client = axios.create({
    baseURL: `http://127.0.0.1:${address.port}`,
    httpAgent: agent,
    maxRedirects: 0,
    timeout: 1000,
  })
  const clientObservations = []

  try {
    for (let iteration = 0; iteration < options.iterations; iteration += 1) {
      for (const phase of ["warm", "probe"]) {
        try {
          const response = await client.post("/transport", { phase, iteration })
          clientObservations.push({
            phase,
            iteration,
            status: response.status,
            reusedSocket: Boolean(response.request?.reusedSocket),
            localPort: response.request?.socket?.localPort ?? null,
          })
        } catch (error) {
          clientObservations.push({
            phase,
            iteration,
            status: null,
            code: error.code ?? null,
            message: error.message,
            reusedSocket: Boolean(error.request?.reusedSocket),
            localPort: error.request?.socket?.localPort ?? null,
          })
        }
        await new Promise((resolve) => setImmediate(resolve))
      }
    }
  } finally {
    agent.destroy()
    await closeServer(server)
  }

  const failures = clientObservations.filter((observation) => observation.status === null)
  const reusedFailures = failures.filter((observation) => observation.reusedSocket)
  return {
    keepAlive,
    requests: clientObservations,
    failures: failures.length,
    reusedFailures: reusedFailures.length,
    serverRequests: requests,
    serverConnections: new Set(requests.map((request) => request.connection)).size,
  }
}

function checkMode(mode, keepAliveArm, freshArm) {
  if (mode === "stale-keepalive") {
    return (
      keepAliveArm.reusedFailures > 0 &&
      freshArm.failures === 0 &&
      keepAliveArm.failures > freshArm.failures
    )
  }
  return keepAliveArm.failures > 0 && freshArm.failures > 0
}

async function main() {
  const options = parseArgs(process.argv.slice(2))
  const save = (report) => {
    if (options.output) writeFileSync(options.output, `${JSON.stringify(report, null, 2)}\n`)
  }
  save({ verdict: "unknown", reason: "run has not completed" })
  const axios = require(resolve(options.axiosRoot))
  const modes = {}

  for (const mode of ["stale-keepalive", "server-pressure"]) {
    const keepAliveArm = await runArm(axios, mode, true, options)
    const freshArm = await runArm(axios, mode, false, options)
    modes[mode] = {
      keepAlive: keepAliveArm,
      freshConnection: freshArm,
      causalWitness: checkMode(mode, keepAliveArm, freshArm),
    }
  }

  const report = {
    node: process.version,
    axios: axios.VERSION,
    iterations: options.iterations,
    source_sha256: createHash("sha256").update(readFileSync(new URL(import.meta.url))).digest("hex"),
    modes,
    verdict: Object.values(modes).every((mode) => mode.causalWitness)
      ? "controlled-witness-passed"
      : "unresolved",
    limits: [
      "The stale mode is a deterministic local positive control, not evidence that Uvicorn caused the hosted failure.",
      "The pressure mode resets requests independent of connection reuse; it is a falsifier for keep-alive-only explanations.",
      "No retry, production client, workflow, or hosted server is modified.",
    ],
  }
  save(report)
  process.stdout.write(`${JSON.stringify(report, null, 2)}\n`)
  if (report.verdict !== "controlled-witness-passed") {
    process.exitCode = 1
  }
}

main().catch((error) => {
  process.stderr.write(`${error.stack ?? error}\n`)
  process.exitCode = 1
})
