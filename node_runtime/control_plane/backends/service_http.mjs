/**
 * Minimal loopback HTTP client for the in-process Pi extensions.
 *
 * The service endpoint is always a loopback address chosen by the trusted
 * launcher. Node's global fetch follows whatever undici dispatcher the host
 * process installed, which can route even 127.0.0.1 through an ambient proxy.
 * `node:http` talks to the address directly and has no proxy configuration of
 * its own, so an extension always reaches the service it was given.
 */
import http from "node:http";
import https from "node:https";

const DEFAULT_TIMEOUT_MS = 60000;

/**
 * Perform one HTTP request and return the complete response.
 *
 * The timeout is one absolute deadline covering headers and the entire body,
 * not a socket idle timeout: a server that sends headers and then trickles
 * bytes forever must still fail. Resolves with `{status, headers, body}`; a
 * deadline or transport failure rejects with `code: "timeout"` or the
 * original error respectively. The request is never retried.
 */
export function requestText(url, {
  method = "GET",
  headers = {},
  body = null,
  timeoutMs = DEFAULT_TIMEOUT_MS,
} = {}) {
  return new Promise((resolve, reject) => {
    let target;
    try {
      target = new URL(url);
    } catch (error) {
      reject(error);
      return;
    }
    const transport = target.protocol === "https:" ? https : http;
    const payload = body === null || body === undefined
      ? null
      : (Buffer.isBuffer(body) ? body : Buffer.from(String(body)));
    const finalHeaders = { ...headers };
    if (payload !== null && !Object.keys(finalHeaders)
        .some((key) => key.toLowerCase() === "content-length")) {
      finalHeaders["Content-Length"] = String(payload.length);
    }
    let settled = false;
    let deadline = null;
    const finish = (deliver, value) => {
      if (settled) return;
      settled = true;
      if (deadline !== null) clearTimeout(deadline);
      deliver(value);
    };
    const fail = (error) => finish(reject, error);
    const request = transport.request(target, { method, headers: finalHeaders }, (response) => {
      const chunks = [];
      response.on("data", (chunk) => chunks.push(chunk));
      response.on("end", () => {
        finish(resolve, {
          status: response.statusCode ?? 0,
          headers: response.headers,
          body: Buffer.concat(chunks).toString("utf8"),
        });
      });
      response.on("aborted", () => fail(
        new Error("connection closed before the response body completed")));
      response.on("error", fail);
    });
    deadline = setTimeout(() => {
      const error = new Error(`request did not answer within ${timeoutMs}ms`);
      error.code = "timeout";
      fail(error);
      request.destroy();
    }, timeoutMs);
    request.on("error", fail);
    if (payload !== null) request.write(payload);
    request.end();
  });
}

/** Parse the JSON-RPC messages carried by a complete SSE response body. */
export function parseSseMessages(raw) {
  const messages = [];
  for (const block of String(raw ?? "").split(/\r?\n\r?\n/)) {
    const data = [];
    for (const line of block.split(/\r?\n/)) {
      if (!line.startsWith("data:")) continue;
      data.push(line.slice(5).replace(/^ /, ""));
    }
    if (data.length === 0) continue;
    try {
      messages.push(JSON.parse(data.join("\n")));
    } catch {
      // A malformed event is ignored; the caller reports the missing response.
    }
  }
  return messages;
}

/** One parsed SSE frame: `{id, event, data}` with `data` still as text. */
function parseSseBlock(block) {
  let id = null;
  let event = "message";
  const data = [];
  for (const line of block.split("\n")) {
    if (!line || line.startsWith(":")) continue;
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "data") data.push(value);
    else if (field === "id") id = value;
    else if (field === "event") event = value;
  }
  if (data.length === 0) return null;
  return { id, event, data: data.join("\n") };
}

// A slow reader must apply backpressure to the socket rather than letting
// parsed frames accumulate without bound in this process.
const SSE_QUEUE_HIGH_WATER = 8;
const SSE_QUEUE_LOW_WATER = 2;
const SSE_MAX_BUFFER_BYTES = 1024 * 1024;

function frameQueue({ onFull = null, onDrained = null } = {}) {
  const items = [];
  const waiters = [];
  let closed = false;
  let failure = null;
  let paused = false;
  const wake = () => {
    const waiter = waiters.shift();
    if (waiter) waiter();
  };
  return {
    push(item) {
      if (closed) return;
      items.push(item);
      if (!paused && items.length >= SSE_QUEUE_HIGH_WATER) {
        paused = true;
        if (onFull) onFull();
      }
      wake();
    },
    close() {
      closed = true;
      wake();
    },
    fail(error) {
      failure = error;
      closed = true;
      wake();
    },
    async next() {
      for (;;) {
        if (items.length > 0) {
          const value = items.shift();
          if (paused && items.length <= SSE_QUEUE_LOW_WATER) {
            paused = false;
            if (onDrained) onDrained();
          }
          return { value, done: false };
        }
        if (closed) {
          if (failure) throw failure;
          return { value: undefined, done: true };
        }
        await new Promise((resolve) => waiters.push(resolve));
      }
    },
  };
}

/**
 * Follow a read-only SSE endpoint as an async iterator of parsed frames.
 *
 * `timeoutMs` is an idle deadline: each received chunk (including a server
 * heartbeat) resets it, so a silent connection fails instead of hanging. The
 * parsed-frame queue is bounded: above its high-water mark the HTTP response
 * is paused, and it resumes only after the consumer drains it. An incomplete
 * frame larger than the buffer limit fails explicitly. The request is
 * destroyed and the abort listener removed when the iterator returns; a
 * caller resumes from the last frame id it consumed.
 */
export async function* streamSse(url, {
  headers = {},
  timeoutMs = DEFAULT_TIMEOUT_MS,
  signal = null,
} = {}) {
  const target = new URL(url);
  const transport = target.protocol === "https:" ? https : http;
  let responseRef = null;
  const queue = frameQueue({
    onFull: () => {
      if (responseRef) responseRef.pause();
    },
    onDrained: () => {
      if (responseRef) responseRef.resume();
    },
  });
  let timer = null;
  const arm = () => {
    if (timer !== null) clearTimeout(timer);
    timer = setTimeout(() => {
      const error = new Error(`SSE stream was idle for ${timeoutMs}ms`);
      error.code = "timeout";
      queue.fail(error);
      request.destroy();
    }, timeoutMs);
  };
  const request = transport.request(target, {
    method: "GET",
    headers: { ...headers, Accept: "text/event-stream" },
  }, (response) => {
    responseRef = response;
    const status = response.statusCode ?? 0;
    if (status !== 200) {
      const error = new Error(`SSE endpoint rejected the request (${status})`);
      error.code = "sse_rejected";
      error.status = status;
      queue.fail(error);
      response.resume();
      return;
    }
    arm();
    let buffer = "";
    response.setEncoding("utf8");
    response.on("data", (chunk) => {
      arm();
      buffer += chunk.replace(/\r\n/g, "\n");
      let index = buffer.indexOf("\n\n");
      while (index >= 0) {
        const block = buffer.slice(0, index);
        buffer = buffer.slice(index + 2);
        const frame = parseSseBlock(block);
        if (frame) queue.push(frame);
        index = buffer.indexOf("\n\n");
      }
      if (buffer.length > SSE_MAX_BUFFER_BYTES) {
        const error = new Error(
          `SSE frame exceeded ${SSE_MAX_BUFFER_BYTES} bytes`);
        error.code = "sse_frame_too_large";
        queue.fail(error);
        request.destroy();
      }
    });
    response.on("end", () => queue.close());
    response.on("aborted", () => queue.fail(
      new Error("SSE connection closed before the stream ended")));
    response.on("error", (error) => queue.fail(error));
  });
  request.on("error", (error) => queue.fail(error));
  const abort = () => {
    request.destroy();
    queue.close();
  };
  if (signal) {
    if (signal.aborted) abort();
    else signal.addEventListener("abort", abort, { once: true });
  }
  arm();
  request.end();
  try {
    for (;;) {
      const { value, done } = await queue.next();
      if (done) return;
      yield value;
    }
  } finally {
    if (timer !== null) clearTimeout(timer);
    if (signal) signal.removeEventListener("abort", abort);
    request.destroy();
    queue.close();
  }
}
