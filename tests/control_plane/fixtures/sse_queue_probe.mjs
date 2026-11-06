/**
 * Controlled probe for the bounded SSE client queue.
 *
 * Starts local servers that emit scripted streams, then drives the real
 * streamSse export. Proves a slow consumer pauses the response (server-side
 * socket backpressure) and that no frame is lost or reordered after resume,
 * and that an oversized incomplete frame fails explicitly instead of growing
 * an unbounded buffer. No product code is reimplemented here.
 */
import http from "node:http";

const { streamSse } = await import(process.env.AGENTGENOME_SERVICE_HTTP);

const listen = (server) => new Promise((resolve) => {
  server.listen(0, "127.0.0.1", () => resolve(server.address().port));
});
const close = (server) => new Promise((resolve) => server.close(resolve));
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function backpressureProbe() {
  const total = 400;
  const padding = "x".repeat(16000);
  let backpressure = false;
  const server = http.createServer((request, response) => {
    response.writeHead(200, { "Content-Type": "text/event-stream" });
    let index = 0;
    let timer = null;
    const pump = () => {
      if (index >= total) {
        clearInterval(timer);
        response.end();
        return;
      }
      const frame = `id: ${index}\nevent: control\ndata: {"n":${index},"pad":"${padding}"}\n\n`;
      if (!response.write(frame)) backpressure = true;
      index += 1;
    };
    timer = setInterval(pump, 1);
    request.on("close", () => clearInterval(timer));
  });
  const port = await listen(server);
  const received = [];
  try {
    const stream = streamSse(`http://127.0.0.1:${port}/watch`, {
      timeoutMs: 10000,
    });
    const first = await stream.next();
    if (first.done) throw new Error("stream ended before its first frame");
    received.push(JSON.parse(first.value.data).n);
    // Do not consume for a while: the client must pause the response.
    await sleep(400);
    for (;;) {
      const item = await stream.next();
      if (item.done) break;
      received.push(JSON.parse(item.value.data).n);
    }
  } finally {
    await close(server);
  }
  const ordered = received.every((value, index) => value === index);
  return {
    frames: total,
    received: received.length,
    backpressure,
    ordered,
    ok: backpressure && received.length === total && ordered,
  };
}

async function oversizedProbe() {
  const server = http.createServer((request, response) => {
    response.writeHead(200, { "Content-Type": "text/event-stream" });
    // One incomplete frame with no delimiter, larger than the buffer bound.
    response.write("data: " + "y".repeat(2 * 1024 * 1024));
  });
  const port = await listen(server);
  let code = null;
  try {
    const stream = streamSse(`http://127.0.0.1:${port}/watch`, {
      timeoutMs: 5000,
    });
    await stream.next();
  } catch (error) {
    code = error.code ?? error.message;
  } finally {
    await close(server);
  }
  return { code, ok: code === "sse_frame_too_large" };
}

const result = {};
try {
  result.backpressure = await backpressureProbe();
  result.oversized = await oversizedProbe();
  result.ok = Boolean(result.backpressure.ok && result.oversized.ok);
} catch (error) {
  result.ok = false;
  result.error = String(error && error.stack ? error.stack : error);
}
process.stdout.write(JSON.stringify(result) + "\n");
process.exitCode = result.ok ? 0 : 1;
