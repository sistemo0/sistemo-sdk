// close() keeps trying until the machine is deleted (audit AD1): 11–13 of 20
// parallel close() calls threw on 429 and left those machines running and billed.
import { createServer } from "node:http";
import { after, before, beforeEach, describe, it } from "node:test";
import assert from "node:assert/strict";
import { APIError, Client, Sandbox, SistemoError } from "../dist/index.js";

const script = []; // [status, code, retryAfter] answered in order to DELETE
let deletes = 0;
let server;
let base;

before(async () => {
  server = createServer((req, res) => {
    const send = (status, body, headers = {}) => {
      res.writeHead(status, { "content-type": "application/json", ...headers });
      res.end(JSON.stringify(body));
    };
    if (req.method === "GET") return send(200, { id: "m-1", state: "running" });
    if (req.method === "DELETE") {
      // The API refuses a body on a route that takes none (400): so does this.
      if (Number(req.headers["content-length"] ?? 0) > 0) return send(400, { code: "validation_error" });
      deletes++;
      const [status, code, ra] = script.shift() ?? [200];
      return send(status, status === 200 ? { status: "destroyed" } : { code, detail: code },
        ra === undefined ? {} : { "retry-after": String(ra) });
    }
    send(404, { code: "not_found" });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  base = `http://127.0.0.1:${server.address().port}`;
});
after(() => server.close());
beforeEach(() => { script.length = 0; deletes = 0; });

const sandbox = () =>
  Sandbox.connect("m-1", { client: new Client({ apiKey: "k", baseUrl: base, maxRetries: 2, retryBackoffMs: 1, retryMaxBackoffMs: 5 }) });

describe("Sandbox.close", () => {
  it("waits out the rate limit however many 429s — more than maxRetries", async () => {
    for (let i = 0; i < 5; i++) script.push([429, "rate_limited", 0]);
    await (await sandbox()).close();
    assert.equal(deletes, 6);
  });

  it("treats an already-deleted machine as closed", async () => {
    script.push([404, "not_found"]);
    await (await sandbox()).close();
  });

  it("waits out a machine still mid-transition", async () => {
    script.push([409, "invalid_state_transition"]);
    await (await sandbox()).close();
    assert.equal(deletes, 2);
  });

  it("throws at once on a refusal waiting cannot fix", async () => {
    script.push([409, "conflict"]);
    await assert.rejects((await sandbox()).close(), (e) => e instanceof APIError && e.status === 409);
    assert.equal(deletes, 1);
  });

  it("gives up at its deadline", async () => {
    for (let i = 0; i < 5; i++) script.push([429, "rate_limited", 30]);
    const t0 = Date.now();
    await assert.rejects((await sandbox()).close(false, { timeoutMs: 1000 }), (e) => e instanceof APIError && e.status === 429);
    assert.ok(Date.now() - t0 < 1000, "slept past the deadline");
  });

  it("await using reports a machine it could not delete instead of hiding it", async () => {
    script.push([409, "conflict"]);
    const warned = [];
    const orig = process.emitWarning;
    process.emitWarning = (m) => warned.push(String(m));
    try {
      await (await sandbox())[Symbol.asyncDispose]();
    } finally {
      process.emitWarning = orig;
    }
    assert.ok(warned.some((m) => m.includes("m-1") && m.includes("still running")), JSON.stringify(warned));
  });
});

describe("validation edges (audit AD5)", () => {
  it("refuses an empty idempotency key, like Python", async () => {
    const c = new Client({ apiKey: "k", baseUrl: base, maxRetries: 0 });
    await assert.rejects(c.request("POST", "/v1/machines", {}, { idempotencyKey: "" }), SistemoError);
  });
});
