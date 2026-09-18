// Diagnostic only: no retry, socket policy change, headers, or request bodies.
const fs = require("node:fs")
const http = require("node:http")
const path = require("node:path")

const directory = process.env.ASSAY_HTTP_TRACE
const original = http.request
let written = 0

function record(value) {
  if (!directory || written > 500) return
  if (written === 500) value = { event: "trace_truncated", limit: 500 }
  written += 1
  fs.appendFileSync(
    path.join(directory, `http-${process.pid}.jsonl`),
    `${JSON.stringify(value)}\n`,
    { mode: 0o600 },
  )
}

http.request = function observedRequest(...args) {
  const request = original.apply(this, args)
  if (request.host !== "127.0.0.1") return request
  const started = performance.now()
  const context = { method: request.method, path: request.path.split("?")[0] }
  request.once("socket", (socket) => {
    context.reusedSocket = request.reusedSocket === true
    context.localPort = socket.localPort
  })
  request.once("error", (error) => {
    record({
      ...context,
      event: "error",
      code: error.code,
      message: error.message,
      elapsed_ms: performance.now() - started,
    })
  })
  request.once("response", (response) => {
    response.once("end", () => {
      record({
        ...context,
        event: "response",
        status: response.statusCode,
        elapsed_ms: performance.now() - started,
      })
    })
  })
  return request
}
