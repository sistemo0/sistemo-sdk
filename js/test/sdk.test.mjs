/**
 * Mock control-plane stress tests for @sistemo/sdk.
 * Run: npm test  (builds dist/ then node --test test/sdk.test.mjs)
 */

import { createServer } from "node:http";
import { after, before, beforeEach, describe, it } from "node:test";
import assert from "node:assert/strict";
import {
  APIError,
  AuthError,
  Client,
  NotFoundError,
  QuotaExceededError,
  RateLimitError,
  Sandbox,
  SistemoError,
} from "../dist/index.js";

const state = {
  log: [],
  machines: new Map(),
  failNext: null,
  failQueue: [],
  idemCreate: new Map(),
  volumesNull: false,
  jobs: new Map(),
};

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      if (!chunks.length) return resolve(undefined);
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString("utf8")));
      } catch (e) {
        reject(e);
      }
    });
    req.on("error", reject);
  });
}

function send(res, status, body) {
  const raw = body === undefined ? "" : JSON.stringify(body);
  res.writeHead(status, {
    "Content-Type": "application/json",
    "Content-Length": Buffer.byteLength(raw),
  });
  res.end(raw);
}

let baseUrl = "";
let server;

before(async () => {
  server = createServer(async (req, res) => {
    const method = req.method || "GET";
    const url = new URL(req.url || "/", "http://127.0.0.1");
    const path = url.pathname;
    const fullPath = url.pathname + url.search;
    const body = method === "POST" || method === "PUT" ? await readBody(req) : undefined;
    const auth = req.headers.authorization || "";
    const ua = req.headers["user-agent"] || "";
    const idem = req.headers["idempotency-key"] || "";
    state.log.push({ method, path, fullPath, body, auth, ua: String(ua), idem: String(idem) });

    if (state.failQueue.length) {
      const f = state.failQueue.shift();
      send(res, f.status, f.body);
      return;
    }

    if (state.failNext) {
      const f = state.failNext;
      state.failNext = null;
      send(res, f.status, f.body);
      return;
    }

    if (!String(auth).startsWith("Bearer ")) {
      send(res, 401, { detail: "missing key", code: "unauthorized" });
      return;
    }

    if (method === "POST" && path === "/v1/machines") {
      if (idem && state.idemCreate.has(idem)) {
        const mid = state.idemCreate.get(idem);
        send(res, 201, state.machines.get(mid));
        return;
      }
      const id = `m-${state.machines.size + 1}`;
      const m = { id, state: "running", ...(body || {}) };
      state.machines.set(id, m);
      if (idem) state.idemCreate.set(idem, id);
      send(res, 201, m);
      return;
    }

    if (method === "POST" && path.endsWith("/execs")) {
      const id = path.split("/")[3];
      if (!state.machines.has(id)) {
        send(res, 404, { detail: "not found" });
        return;
      }
      const script = body?.script || "";
      const eid = body?.exec_id || "e-1";
      const rec = {
        exec_id: eid,
        machine_id: id,
        state: "succeeded",
        exit_code: script.includes("fail") ? 1 : 0,
        truncated: false,
      };
      state.jobs.set(eid, { rec, stdout: Buffer.from(`out:${script}`) });
      send(res, 202, rec);
      return;
    }

    if (method === "GET" && path.includes("/execs/") && path.endsWith("/output")) {
      const eid = path.split("/")[5];
      const job = state.jobs.get(eid) || {};
      const offset = Number(url.searchParams.get("offset") || 0);
      let data = url.searchParams.get("stream") === "stderr" ? Buffer.alloc(0) : (job.stdout || Buffer.alloc(0));
      if (offset > 0) data = Buffer.alloc(0);
      send(res, 200, {
        stream: "stdout",
        offset,
        next_offset: offset + data.length,
        data: data.length ? data.toString("base64") : "",
        eof: true,
      });
      return;
    }

    if (method === "GET" && path.includes("/execs/")) {
      const eid = path.split("/")[5];
      const job = state.jobs.get(eid);
      if (!job) {
        send(res, 404, { detail: "not found" });
        return;
      }
      send(res, 200, job.rec);
      return;
    }

    if (method === "DELETE" && path.startsWith("/v1/machines/")) {
      const id = path.split("/")[3];
      state.machines.delete(id);
      send(res, 200, { status: "destroyed" });
      return;
    }

    if (method === "GET" && path === "/v1/volumes") {
      if (state.volumesNull) {
        send(res, 200, { volumes: null });
      } else {
        send(res, 200, { volumes: [{ id: "v1", name: "disk", size_gb: 10 }] });
      }
      return;
    }

    if (method === "POST" && path === "/v1/volumes") {
      send(res, 201, { id: "v-new", name: body?.name, size_gb: body?.size_gb });
      return;
    }

    send(res, 404, { detail: `no mock for ${method} ${path}` });
  });

  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const addr = server.address();
  baseUrl = `http://127.0.0.1:${addr.port}`;
});

after(async () => {
  await new Promise((resolve, reject) => server.close((e) => (e ? reject(e) : resolve())));
});

beforeEach(() => {
  state.log = [];
  state.machines.clear();
  state.failNext = null;
  state.failQueue = [];
  state.idemCreate.clear();
  state.volumesNull = false;
  state.jobs.clear();
  delete process.env.SISTEMO_API_KEY;
  delete process.env.SISTEMO_BASE_URL;
});

describe("Client", () => {
  it("requires api key", () => {
    assert.throws(() => new Client({ baseUrl }), (e) => {
      assert.ok(e instanceof SistemoError);
      assert.match(e.message, /API key/);
      return true;
    });
  });

  it("reads env and sends UA + auth", async () => {
    process.env.SISTEMO_API_KEY = "sk_env";
    process.env.SISTEMO_BASE_URL = baseUrl + "/";
    const c = new Client();
    assert.equal(c.baseUrl, baseUrl);
    await c.listVolumes();
    const last = state.log[state.log.length - 1];
    assert.equal(last.auth, "Bearer sk_env");
    assert.match(last.ua, /^@sistemo\/sdk\/\d+\.\d+\.\d+/); // exact version pinned in "version drift"
  });

  it("maps 401/404/429", async () => {
    const c = new Client({ apiKey: "k", baseUrl, maxRetries: 0 });
    state.failNext = { status: 401, body: { detail: "nope" } };
    await assert.rejects(() => c.request("GET", "/x"), AuthError);
    state.failNext = { status: 404, body: { detail: "missing" } };
    await assert.rejects(() => c.request("GET", "/x"), NotFoundError);
    state.failNext = { status: 429, body: { detail: "slow" } };
    await assert.rejects(() => c.request("GET", "/x"), RateLimitError);
  });

  it("retries GET on 503 then succeeds", async () => {
    const c = new Client({ apiKey: "k", baseUrl, maxRetries: 2, retryBackoffMs: 5 });
    state.failQueue = [
      { status: 503, body: { detail: "busy" } },
      { status: 503, body: { detail: "still" } },
    ];
    const out = await c.listVolumes();
    assert.equal(out.volumes[0].id, "v1");
    assert.equal(state.log.filter((l) => l.path === "/v1/volumes").length, 3);
  });

  it("retries POST only with Idempotency-Key", async () => {
    const c = new Client({ apiKey: "k", baseUrl, maxRetries: 2, retryBackoffMs: 5 });
    state.failNext = { status: 500, body: { detail: "boom" } };
    await assert.rejects(
      () => c.request("POST", "/v1/machines", { name: "x" }),
      (e) => e.status === 500,
    );
    assert.equal(state.log.filter((l) => l.path === "/v1/machines").length, 1);

    state.log = [];
    state.failQueue = [{ status: 500, body: { detail: "boom" } }];
    const out = await c.request("POST", "/v1/machines", { name: "y" }, { idempotencyKey: "retry-me" });
    assert.equal(out.id, "m-1");
    const creates = state.log.filter((l) => l.path === "/v1/machines");
    assert.equal(creates.length, 2);
    assert.equal(creates[0].idem, "retry-me");
    assert.equal(creates[1].idem, "retry-me");
  });

  it("listVolumes null-safe", async () => {
    const c = new Client({ apiKey: "k", baseUrl });
    state.volumesNull = true;
    const out = await c.listVolumes();
    assert.deepEqual(out.volumes, []);
  });
});

describe("Sandbox", () => {
  it("create → run → close with correct payload", async () => {
    const sb = await Sandbox.create({
      apiKey: "k",
      baseUrl,
      name: "n1",
      stack: "python",
      memoryMb: 2048,
      rootfsSizeGb: 20,
    });
    assert.match(sb.id, /^m-/);
    assert.equal(sb.state, "running");

    const create = state.log.find((l) => l.path === "/v1/machines");
    assert.deepEqual(create.body, {
      name: "n1",
      vcpus: 1,
      memory_mb: 2048,
      rootfs_size_gb: 20,
      stack: "python",
      metadata: {},
    });
    assert.ok(create.idem && create.idem.length >= 8);

    const r = await sb.run("echo hi");
    assert.equal(r.exitCode, 0);
    assert.equal(r.stdout, "out:echo hi");
    const ex = state.log.find((l) => l.path.endsWith("/execs"));
    assert.equal(ex.body.script, "echo hi");
    assert.equal(ex.body.timeout_sec, 120);
    assert.ok(ex.body.exec_id);
    assert.equal(ex.idem, "");

    await sb.close();
    assert.equal(state.machines.has(sb.id), false);
  });

  it("preserveStorage query + rootVolumeId", async () => {
    const sb = await Sandbox.create({
      apiKey: "k",
      baseUrl,
      rootVolumeId: "vol-1",
    });
    const create = state.log.find((l) => l.path === "/v1/machines");
    assert.equal(create.body.root_volume_id, "vol-1");
    await sb.close(true);
    const del = state.log.find((l) => l.method === "DELETE");
    assert.match(del.fullPath, /preserve_storage=true/);
  });

  it("connection error", async () => {
    const c = new Client({
      apiKey: "k",
      baseUrl: "http://127.0.0.1:1",
      timeoutMs: 300,
      maxRetries: 0,
    });
    await assert.rejects(() => c.request("GET", "/v1/volumes"), (e) => {
      assert.ok(e instanceof SistemoError);
      assert.match(e.message, /connection error/);
      return true;
    });
  });

  it("exec fail exit code", async () => {
    const sb = await Sandbox.create({ apiKey: "k", baseUrl });
    const r = await sb.run("fail me");
    assert.equal(r.exitCode, 1);
    await sb.close();
  });

  it("missing create id raises clean APIError", async () => {
    const { APIError } = await import("../dist/index.js");
    state.failNext = { status: 201, body: { state: "running" } };
    await assert.rejects(
      () => Sandbox.create({ apiKey: "k", baseUrl }),
      (e) => {
        assert.ok(e instanceof APIError);
        assert.equal(e.status, 502);
        assert.equal(e.code, "invalid_create_response");
        assert.match(e.detail, /missing id/);
        return true;
      },
    );
  });

  it("fixed idempotency key on create", async () => {
    const sb = await Sandbox.create({
      apiKey: "k",
      baseUrl,
      idempotencyKey: "fixed-key-abc",
    });
    const create = state.log.find((l) => l.path === "/v1/machines");
    assert.equal(create.idem, "fixed-key-abc");
    assert.equal(sb.id, "m-1");
  });

  it("create retry reuses same idempotency key", async () => {
    state.failQueue = [{ status: 503, body: { detail: "busy" } }];
    const client = new Client({ apiKey: "k", baseUrl, maxRetries: 2, retryBackoffMs: 5 });
    const sb = await Sandbox.create({ client, idempotencyKey: "create-once" });
    const creates = state.log.filter((l) => l.path === "/v1/machines");
    assert.equal(creates.length, 2);
    assert.equal(creates[0].idem, "create-once");
    assert.equal(creates[1].idem, "create-once");
    assert.equal(state.machines.size, 1);
    assert.equal(sb.id, "m-1");
  });

  it("exec HTTP 5xx is not retried", async () => {
    const sb = await Sandbox.create({ apiKey: "k", baseUrl });
    state.failQueue = [
      { status: 503, body: { detail: "agent down" } },
      { status: 200, body: { exit_code: 0, stdout: "should-not-run", stderr: "" } },
    ];
    await assert.rejects(() => sb.run("echo once"), (e) => e.status === 503);
    assert.equal(state.log.filter((l) => l.path.endsWith("/execs")).length, 1);
  });
});


describe("quota error mapping", () => {
  // A quota refusal must NOT surface as an auth failure. Both arrive as 403, but
  // they need opposite handling: an AuthError means the credential cannot do this
  // and retrying never helps, while a quota refusal means the credential is fine
  // and a retry succeeds once a machine is stopped. Raising the wrong type sends
  // a caller off to rotate a perfectly good API key.
  let srv, base, nextBody;

  before(async () => {
    srv = createServer((req, res) => {
      res.writeHead(403, { "content-type": "application/json" });
      res.end(JSON.stringify(nextBody));
    });
    await new Promise((r) => srv.listen(0, "127.0.0.1", r));
    base = `http://127.0.0.1:${srv.address().port}`;
  });
  after(() => srv.close());

  it("raises QuotaExceededError for code=quota_exceeded", async () => {
    nextBody = { code: "quota_exceeded", detail: "Machines quota reached: 1 of 1 in use." };
    const c = new Client({ apiKey: "sk_test", baseUrl: base });
    await assert.rejects(() => c.listVolumes(), (err) => {
      assert.ok(err instanceof QuotaExceededError, "should be QuotaExceededError");
      assert.ok(!(err instanceof AuthError), "must NOT be an AuthError");
      assert.equal(err.code, "quota_exceeded");
      return true;
    });
  });

  it("still raises AuthError for a plain 403", async () => {
    // Counter-guard: the obvious over-broad fix routes every 403 to the new type,
    // which hides genuine scope failures.
    nextBody = { code: "forbidden", detail: "this API key is read-only" };
    const c = new Client({ apiKey: "sk_test", baseUrl: base });
    await assert.rejects(() => c.listVolumes(), (err) => {
      assert.ok(err instanceof AuthError, "should be AuthError");
      assert.ok(!(err instanceof QuotaExceededError), "must NOT be QuotaExceededError");
      return true;
    });
  });

  it("both stay catchable as APIError", async () => {
    for (const code of ["quota_exceeded", "forbidden"]) {
      nextBody = { code, detail: "x" };
      const c = new Client({ apiKey: "sk_test", baseUrl: base });
      await assert.rejects(() => c.listVolumes(), (err) => err instanceof APIError);
    }
  });
});

describe("version drift", () => {
  // The version is stated in two places that must agree: package.json and the
  // User-Agent literal in src/index.ts.
  //
  // ⚠ The UA is not cosmetic. The production API sits behind Cloudflare, whose
  // WAF rejects generic library UAs with a 403, so this string is what makes the
  // SDK work at all — and the release job only ever compares the git tag to
  // package.json. Without this test, `npm publish` could ship 0.2.0 introducing
  // itself to the API as `@sistemo/sdk/0.1.0`, and nothing would notice.
  //
  // Unlike Python there is no runtime single-source here: reading package.json
  // from dist/ at import time would add filesystem work to every consumer just
  // to build a header. A test is the cheaper guard, and the same shape as the
  // version check already in the release job.
  it("User-Agent version matches package.json", async () => {
    const { readFileSync } = await import("node:fs");
    const { fileURLToPath } = await import("node:url");
    const pkg = JSON.parse(
      readFileSync(fileURLToPath(new URL("../package.json", import.meta.url)), "utf8"),
    );

    await new Client({ apiKey: "k", baseUrl }).listVolumes();
    const last = state.log[state.log.length - 1];

    assert.equal(
      last.ua,
      `@sistemo/sdk/${pkg.version}`,
      "the User-Agent must carry package.json's version — the WAF makes this " +
        "string load-bearing and a stale one is invisible until it 403s",
    );
  });
});
