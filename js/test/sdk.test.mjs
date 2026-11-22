/**
 * Mock control-plane stress tests for @sistemo/sdk.
 * Run: npm test  (builds dist/ then node --test test/sdk.test.mjs)
 */

import { spawn, spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, before, beforeEach, describe, it } from "node:test";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";
import {
  APIError,
  AuthError,
  Client,
  NotFoundError,
  QuotaExceededError,
  InsufficientBalanceError,
  DiskFullError,
  MachineNotRunningError,
  OutOfMemoryError,
  ProcessLimitError,
  RateLimitError,
  Sandbox,
  SistemoError,
  TooManyExecsError,
  ExecStartUnconfirmed,
  MachineTimeout,
  parseRetryAfter,
  retryDelayMs,
  startLookup,
} from "../dist/index.js";

const state = {
  log: [],
  machines: new Map(),
  failNext: null,
  failQueue: [],
  idemCreate: new Map(),
  volumesNull: false,
  paginate: false,
  dropCount: 0,
  dropStoreExec: false,
  execLookup404s: 0,
  jobs: new Map(),
  jobGone: null,
  outputGone: null,
  snapshots: [],
  promoteAfter: 0,
  machineGets: 0,
};

function acceptExec(path, body) {
  const id = path.split("/")[3];
  const script = body?.script || "";
  const eid = body?.exec_id || "e-1";
  const rec = {
    exec_id: eid,
    machine_id: id,
    state: "succeeded",
    exit_code: script.includes("fail") ? 1 : 0,
    truncated: false,
  };
  if (state.jobGone) {
    rec.state = "running";
    rec.exit_code = null;
  }
  state.jobs.set(eid, { rec, stdout: Buffer.from(`out:${script}`) });
  return rec;
}

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

function send(res, status, body, extraHeaders, reqId) {
  if (body && typeof body === "object" && reqId && body.request_id == null) {
    body = { ...body, request_id: reqId };
  }
  const raw = body === undefined ? "" : JSON.stringify(body);
  res.writeHead(status, {
    "Content-Type": "application/json",
    "Content-Length": Buffer.byteLength(raw),
    ...(extraHeaders || {}),
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
    const auth = req.headers.authorization || "";
    const ua = req.headers["user-agent"] || "";
    const idem = req.headers["idempotency-key"] || "";
    const reqId = req.headers["x-request-id"] || "";
    // Drop BEFORE reading the body, except dropStoreExec: the guest accepted
    // the POST and the ACK was lost, so we must read+store then destroy.
    // Destroy-after-read waits on undici headersTimeout (the client's timeoutMs).
    if (state.dropCount > 0) {
      state.dropCount -= 1;
      if (state.dropStoreExec && method === "POST" && path.endsWith("/execs")) {
        const stored = await readBody(req);
        state.log.push({ method, path, fullPath, body: stored, auth, ua: String(ua), idem: String(idem), reqId: String(reqId) });
        acceptExec(path, stored);
        req.destroy();
        return;
      }
      state.log.push({ method, path, fullPath, body: undefined, auth, ua: String(ua), idem: String(idem), reqId: String(reqId) });
      req.destroy();
      return;
    }
    const body = method === "POST" || method === "PUT" ? await readBody(req) : undefined;
    state.log.push({ method, path, fullPath, body, auth, ua: String(ua), idem: String(idem), reqId: String(reqId) });

    if (state.failQueue.length) {
      const f = state.failQueue.shift();
      send(res, f.status, f.body, f.headers, reqId);
      return;
    }

    if (state.failNext) {
      const f = state.failNext;
      state.failNext = null;
      send(res, f.status, f.body, f.headers, reqId);
      return;
    }

    if (state.jobGone && method === "GET" && path.includes("/execs/")) {
      send(res, state.jobGone.status, state.jobGone.body, undefined, reqId);
      return;
    }

    if (state.outputGone && method === "GET" && path.endsWith("/output")) {
      send(res, state.outputGone.status, state.outputGone.body, undefined, reqId);
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
      // Production create does not echo root_volume_id.
      delete m.root_volume_id;
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
      send(res, 202, acceptExec(path, body));
      return;
    }

    if (method === "GET" && path.endsWith("/execs")) {
      const id = path.split("/")[3];
      const cursor = url.searchParams.get("cursor");
      if (state.paginate) {
        if (cursor === "p2") {
          send(res, 200, {
            execs: [{ exec_id: "e2", machine_id: id, state: "succeeded", exit_code: 0 }],
            next_cursor: null,
          });
        } else {
          send(res, 200, {
            execs: [{ exec_id: "e1", machine_id: id, state: "succeeded", exit_code: 0 }],
            next_cursor: "p2",
          });
        }
        return;
      }
      const items = [...state.jobs.values()].filter((j) => j.rec?.machine_id === id).map((j) => j.rec);
      send(res, 200, { execs: items, next_cursor: null });
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
      if (state.execLookup404s > 0) {
        state.execLookup404s -= 1;
        send(res, 404, { detail: "not found" });
        return;
      }
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
      const cursor = url.searchParams.get("cursor");
      if (state.paginate) {
        if (cursor === "p2") {
          send(res, 200, { volumes: [{ id: "v2", name: "disk2", size_gb: 10 }], next_cursor: null });
        } else {
          send(res, 200, { volumes: [{ id: "v1", name: "disk", size_gb: 10 }], next_cursor: "p2" });
        }
        return;
      }
      if (state.volumesNull) {
        send(res, 200, { volumes: null, next_cursor: null });
      } else {
        send(res, 200, { volumes: [{ id: "v1", name: "disk", size_gb: 10 }], next_cursor: null });
      }
      return;
    }

    if (method === "GET" && path === "/v1/machines") {
      const cursor = url.searchParams.get("cursor");
      if (state.paginate) {
        if (cursor === "p2") {
          send(res, 200, { machines: [{ id: "m2", state: "running" }], next_cursor: null });
        } else {
          send(res, 200, { machines: [{ id: "m1", state: "running" }], next_cursor: "p2" });
        }
        return;
      }
      send(res, 200, { machines: [...state.machines.values()], next_cursor: null });
      return;
    }

    const parts = path.split("/").filter(Boolean);

    if (method === "GET" && path === "/v1/quotas") {
      send(res, 200, {
        tier: "free",
        quotas: [{ name: "machines", used: 1, limit: 3, description: "Machines that exist." }],
        per_machine: { vcpus: 2, memory_mb: 2048 },
        future_field: "kept",
      });
      return;
    }

    if (method === "GET" && path === "/v1/images") {
      send(res, 200, {
        images: [{ name: "base", template_id: "base_2026", is_default: true, status: "ready", revision: "2026", future: 1 }],
      });
      return;
    }

    if (method === "GET" && path === "/v1/snapshots") {
      const cursor = url.searchParams.get("cursor");
      if (state.paginate) {
        if (cursor === "p2") {
          send(res, 200, { snapshots: [{ id: "s2", name: "later", state: "ready" }], next_cursor: null });
        } else {
          send(res, 200, { snapshots: [{ id: "s1", name: "before", state: "ready" }], next_cursor: "p2" });
        }
        return;
      }
      send(res, 200, { snapshots: state.snapshots, next_cursor: null });
      return;
    }

    if (method === "POST" && parts.length === 4 && parts[0] === "v1" && parts[1] === "snapshots" && parts[3] === "restore") {
      send(res, 200, { id: parts[2], state: "ready", restored: true });
      return;
    }

    if (method === "DELETE" && parts.length === 3 && parts[0] === "v1" && parts[1] === "snapshots") {
      send(res, 200, { status: "deleted" });
      return;
    }

    if (method === "POST" && parts.length === 4 && parts[0] === "v1" && parts[1] === "machines" && (parts[3] === "stop" || parts[3] === "start")) {
      const m = state.machines.get(parts[2]);
      if (!m) {
        send(res, 404, { detail: "not found", code: "not_found" });
        return;
      }
      m.state = parts[3] === "stop" ? "stopped" : "running";
      send(res, 200, m);
      return;
    }

    if (method === "POST" && parts.length === 4 && parts[0] === "v1" && parts[1] === "machines" && parts[3] === "snapshots") {
      const snap = {
        id: `snap-${state.snapshots.length + 1}`,
        name: body?.name,
        state: "ready",
        machine_id: parts[2],
        extra: "kept",
      };
      state.snapshots.push(snap);
      send(res, 201, snap);
      return;
    }

    if (method === "GET" && parts.length === 4 && parts[0] === "v1" && parts[1] === "machines" && parts[3] === "snapshots") {
      const cursor = url.searchParams.get("cursor");
      const mine = state.snapshots.filter((s) => s.machine_id === parts[2]);
      if (state.paginate && cursor !== "p2") {
        send(res, 200, { snapshots: mine.slice(0, 1), next_cursor: "p2" });
        return;
      }
      send(res, 200, { snapshots: cursor === "p2" ? mine.slice(1) : mine, next_cursor: null });
      return;
    }

    // Locked network + secrets (0.2.0).
    if (method === "POST" && parts.length === 4 && parts[1] === "machines" && parts[3] === "network") {
      const m = state.machines.get(parts[2]);
      if (!m) return send(res, 404, { detail: "not found", code: "not_found" });
      m.network = body?.network;
      m.allow = body?.allow;
      return send(res, 200, m);
    }
    if (method === "POST" && parts.length === 4 && parts[1] === "machines" && parts[3] === "secrets") {
      const m = state.machines.get(parts[2]);
      if (!m) return send(res, 404, { detail: "not found", code: "not_found" });
      const names = (m.secrets || []).filter((n) => !(body?.unbind || []).includes(n));
      for (const n of body?.bind || []) {
        if (n === "NOPE") return send(res, 400, { detail: "no secret named NOPE", code: "validation_error" });
        if (!names.includes(n)) names.push(n);
      }
      m.secrets = names.sort();
      return send(res, 200, m);
    }
    if (method === "GET" && path.endsWith("/egress/activity")) {
      return send(res, 200, { activity: [{ host: "evil.example", action: "blocked", at: "2026-11-21T12:00:00Z" }], complete: true });
    }
    if (method === "GET" && path === "/v1/secrets") {
      return send(res, 200, { secrets: [{ name: "STRIPE_KEY", custody: "box", hosts: [], bound_machines: 0 }] });
    }

    if (method === "GET" && path.startsWith("/v1/machines/") && !path.includes("/execs")) {
      if (parts.length !== 3) {
        send(res, 404, { detail: `no mock for ${method} ${path}` });
        return;
      }
      const id = parts[2];
      const m = state.machines.get(id);
      if (!m) {
        send(res, 404, { detail: "not found", code: "not_found" });
        return;
      }
      if (m.state === "pending" || m.state === "provisioning" || m.state === "starting") {
        state.machineGets += 1;
        if (state.promoteAfter && state.machineGets >= state.promoteAfter) m.state = "running";
      }
      send(res, 200, m);
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
  state.paginate = false;
  state.dropCount = 0;
  state.dropStoreExec = false;
  state.execLookup404s = 0;
  state.jobs.clear();
  state.jobGone = null;
  state.outputGone = null;
  state.snapshots = [];
  state.promoteAfter = 0;
  state.machineGets = 0;
  // Production waits a few seconds on GET 404 after a dropped POST. Zero
  // here so a genuine miss (nothing started) stays a fast test.
  startLookup.ms = 0;
  startLookup.intervalMs = 0;
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
    assert.equal(out.nextCursor, null);
  });

  it("listVolumes keeps nextCursor and sends cursor/limit", async () => {
    const c = new Client({ apiKey: "k", baseUrl });
    state.paginate = true;
    const page = await c.listVolumes();
    assert.equal(page.volumes[0].id, "v1");
    assert.equal(page.nextCursor, "p2");
    const page2 = await c.listVolumes({ cursor: page.nextCursor, limit: 1 });
    assert.equal(page2.volumes[0].id, "v2");
    assert.equal(page2.nextCursor, null);
    const last = state.log.at(-1);
    assert.match(last.fullPath, /cursor=p2/);
    assert.match(last.fullPath, /limit=1/);
  });

  it("iterVolumes walks pages", async () => {
    const c = new Client({ apiKey: "k", baseUrl });
    state.paginate = true;
    const ids = [];
    for await (const v of c.iterVolumes()) ids.push(v.id);
    assert.deepEqual(ids, ["v1", "v2"]);
  });

  it("listMachines and iterMachines", async () => {
    const c = new Client({ apiKey: "k", baseUrl });
    state.paginate = true;
    const page = await c.listMachines();
    assert.equal(page.machines[0].id, "m1");
    assert.equal(page.nextCursor, "p2");
    const ids = [];
    for await (const m of c.iterMachines()) ids.push(m.id);
    assert.deepEqual(ids, ["m1", "m2"]);
  });

  it("a dropped GET is a SistemoError and is retried", async () => {
    const c = new Client({ apiKey: "k", baseUrl, maxRetries: 2, retryBackoffMs: 5 });
    state.dropCount = 1;
    const out = await c.listVolumes();
    assert.equal(out.volumes[0].id, "v1");
    assert.equal(state.log.filter((l) => l.path === "/v1/volumes").length, 2);
  });

  it("a dropped create (with Idempotency-Key) is retried", async () => {
    state.dropCount = 1;
    const sb = await Sandbox.create({ apiKey: "k", baseUrl, timeoutMs: 500, retryBackoffMs: 5 });
    const creates = state.log.filter((l) => l.method === "POST" && l.path === "/v1/machines");
    assert.equal(creates.length, 2);
    assert.equal(creates[0].idem, creates[1].idem);
    assert.equal(sb.id, "m-1");
  });

  it("POST without Idempotency-Key is not retried on drop", async () => {
    const c = new Client({
      apiKey: "k",
      baseUrl,
      maxRetries: 2,
      retryBackoffMs: 5,
      timeoutMs: 500,
    });
    state.dropCount = 2;
    await assert.rejects(
      () => c.request("POST", "/v1/machines/nope/execs", { exec_id: "e", script: "x" }),
      (e) => {
        assert.ok(e instanceof SistemoError);
        assert.match(e.message, /connection error/);
        return true;
      },
    );
    assert.equal(state.log.filter((l) => l.method === "POST").length, 1);
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
    assert.equal(r.ok, true);
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
    assert.equal(sb.rootVolumeId, "vol-1");
    assert.equal(state.machines.get(sb.id).root_volume_id, undefined);
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

  it("run sends env and cwd only when set", async () => {
    const sb = await Sandbox.create({ apiKey: "k", baseUrl });
    await sb.run("echo hi", 120, { env: { FOO: "bar" }, cwd: "/tmp" });
    const withOpts = state.log.find((l) => l.path.endsWith("/execs") && l.method === "POST");
    assert.deepEqual(withOpts.body.env, { FOO: "bar" });
    assert.equal(withOpts.body.cwd, "/tmp");

    state.log = [];
    await sb.run("echo hi");
    const bare = state.log.find((l) => l.path.endsWith("/execs") && l.method === "POST");
    assert.equal(bare.body.env, undefined);
    assert.equal(bare.body.cwd, undefined);
  });

  it("jobs page keeps nextCursor", async () => {
    const sb = await Sandbox.create({ apiKey: "k", baseUrl });
    state.paginate = true;
    const page = await sb.jobs(1);
    assert.equal(page[0].id, "e1");
    assert.equal(page.nextCursor, "p2");
    const page2 = await sb.jobs(1, { cursor: page.nextCursor });
    assert.equal(page2[0].id, "e2");
    assert.equal(page2.nextCursor, null);
    const last = state.log.at(-1);
    assert.match(last.fullPath, /cursor=p2/);
    assert.match(last.fullPath, /limit=1/);
  });

  it("iterJobs walks pages", async () => {
    const sb = await Sandbox.create({ apiKey: "k", baseUrl });
    state.paginate = true;
    const ids = [];
    for await (const j of sb.iterJobs({ limit: 1 })) ids.push(j.id);
    assert.deepEqual(ids, ["e1", "e2"]);
  });

  it("connect does not create", async () => {
    const sb = await Sandbox.create({ apiKey: "k", baseUrl });
    const posts = state.log.filter((l) => l.method === "POST" && l.path === "/v1/machines").length;
    const sb2 = await Sandbox.connect(sb.id, { apiKey: "k", baseUrl });
    assert.equal(sb2.id, sb.id);
    assert.equal(sb2.state, "running");
    const posts2 = state.log.filter((l) => l.method === "POST" && l.path === "/v1/machines").length;
    assert.equal(posts2, posts);
    assert.ok(state.log.some((l) => l.method === "GET" && l.path === `/v1/machines/${sb.id}`));
    const r = await sb2.run("echo hi");
    assert.equal(r.ok, true);
  });

  it("connect missing is NotFoundError", async () => {
    await assert.rejects(
      () => Sandbox.connect("nope", { apiKey: "k", baseUrl }),
      (e) => e instanceof NotFoundError,
    );
  });

  it("POST /execs connection drop is not retried", async () => {
    const sb = await Sandbox.create({ apiKey: "k", baseUrl, timeoutMs: 500 });
    state.log = [];
    state.dropCount = 2;
    await assert.rejects(() => sb.run("echo once"), (e) => {
      assert.ok(e instanceof SistemoError);
      assert.match(e.message, /connection error/);
      return true;
    });
    const execs = state.log.filter((l) => l.method === "POST" && l.path.endsWith("/execs"));
    assert.equal(execs.length, 1);
  });

  it("run() recovers a dropped start by exec_id", async () => {
    const sb = await Sandbox.create({ apiKey: "k", baseUrl, timeoutMs: 500 });
    state.log = [];
    state.dropStoreExec = true;
    state.dropCount = 1;
    const r = await sb.run("echo recovered");
    assert.equal(r.ok, true);
    assert.match(r.stdout, /echo recovered/);
    assert.equal(r.lost, false);
    const posts = state.log.filter((l) => l.method === "POST" && l.path.endsWith("/execs"));
    assert.equal(posts.length, 1);
    assert.ok(
      state.log.some((l) => l.method === "GET" && l.path.includes("/execs/")),
      "a dropped start must GET the minted exec_id",
    );
  });

  it("run() recovers a dropped start after lookup 404s", async () => {
    startLookup.ms = 2000;
    startLookup.intervalMs = 0;
    const sb = await Sandbox.create({ apiKey: "k", baseUrl, timeoutMs: 500 });
    state.log = [];
    state.dropStoreExec = true;
    state.dropCount = 1;
    state.execLookup404s = 2;
    const r = await sb.run("echo recovered");
    assert.equal(r.ok, true);
    assert.match(r.stdout, /echo recovered/);
    assert.equal(r.lost, false);
    const posts = state.log.filter((l) => l.method === "POST" && l.path.endsWith("/execs"));
    assert.equal(posts.length, 1);
    const jobGets = state.log.filter(
      (l) => l.method === "GET" && l.path.includes("/execs/") && !l.path.endsWith("/output"),
    );
    assert.ok(
      jobGets.length >= 3,
      "two 404s then a hit — a single GET would have reported nothing started",
    );
  });

  it("dropped start 404s for the whole window is the original error", async () => {
    startLookup.ms = 50;
    startLookup.intervalMs = 0;
    const sb = await Sandbox.create({ apiKey: "k", baseUrl, timeoutMs: 500 });
    state.log = [];
    state.dropCount = 1;
    await assert.rejects(() => sb.run("echo once"), (e) => {
      assert.ok(e instanceof SistemoError);
      assert.ok(!(e instanceof ExecStartUnconfirmed));
      assert.match(e.message, /connection error/);
      return true;
    });
    const posts = state.log.filter((l) => l.method === "POST" && l.path.endsWith("/execs"));
    assert.equal(posts.length, 1);
    const jobGets = state.log.filter(
      (l) => l.method === "GET" && l.path.includes("/execs/") && !l.path.endsWith("/output"),
    );
    assert.ok(jobGets.length >= 2, "the window must GET more than once before giving up");
  });

  it("dropped start raises ExecStartUnconfirmed when GET also fails", async () => {
    const sb = await Sandbox.create({ apiKey: "k", baseUrl, timeoutMs: 500, maxRetries: 0 });
    state.log = [];
    state.dropStoreExec = true;
    state.dropCount = 2;
    await assert.rejects(() => sb.start("echo hi"), (e) => {
      assert.ok(e instanceof ExecStartUnconfirmed);
      assert.ok(e.execId);
      assert.ok(e.requestId, "the unconfirmed error must carry the POST request_id, not a blank 503");
      return true;
    });
    const posts = state.log.filter((l) => l.method === "POST" && l.path.endsWith("/execs"));
    assert.equal(posts.length, 1);
  });

  it("run() succeeded + output 404 is empty, not lost", async () => {
    const sb = await Sandbox.create({ apiKey: "k", baseUrl });
    state.outputGone = { status: 404, body: { code: "not_found", detail: "gone" } };
    const r = await sb.run("echo hi");
    assert.equal(r.lost, false);
    assert.equal(r.ok, true);
    assert.equal(r.exitCode, 0);
    assert.equal(r.stdout, "");
  });

  it("run() lost when the machine stops mid-wait", async () => {
    const sb = await Sandbox.create({ apiKey: "k", baseUrl });
    state.jobGone = {
      status: 409,
      body: { code: "machine_not_running", detail: "stopped" },
    };
    const r = await sb.run("echo hi");
    assert.equal(r.lost, true);
    assert.equal(r.exitCode, -1);
    assert.equal(r.ok, false);
    assert.equal(r.stdout, "");
  });

  it("start() on a stopped machine raises MachineNotRunningError", async () => {
    const sb = await Sandbox.create({ apiKey: "k", baseUrl });
    state.failNext = {
      status: 409,
      body: { code: "machine_not_running", detail: "stopped" },
    };
    await assert.rejects(() => sb.start("echo hi"), (err) => {
      assert.ok(err instanceof MachineNotRunningError);
      assert.ok(err instanceof APIError);
      assert.equal(err.code, "machine_not_running");
      return true;
    });
    assert.equal(
      state.log.filter((l) => l.method === "GET" && l.path.includes("/execs/")).length,
      0,
      "start() must not GET-recover a refusal — nothing started",
    );
  });

  it("the 17th parallel run() is TooManyExecsError", async () => {
    const sb = await Sandbox.create({ apiKey: "k", baseUrl });
    state.failNext = {
      status: 409,
      body: { code: "too_many_execs", detail: "wait", retryable: true },
    };
    await assert.rejects(() => sb.run("echo hi"), (err) => {
      assert.ok(err instanceof TooManyExecsError);
      assert.ok(err instanceof APIError);
      assert.ok(!(err instanceof MachineNotRunningError));
      assert.equal(err.code, "too_many_execs");
      return true;
    });
    assert.equal(
      state.log.filter((l) => l.method === "GET" && l.path.includes("/execs/")).length,
      0,
      "a fresh exec_id after too_many_execs must not be GET-recovered",
    );
  });

  it("run() lost when the job 404s mid-wait", async () => {
    const sb = await Sandbox.create({ apiKey: "k", baseUrl });
    state.jobGone = { status: 404, body: { code: "not_found", detail: "gone" } };
    const r = await sb.run("echo hi");
    assert.equal(r.lost, true);
    assert.equal(r.exitCode, -1);
  });

  it("run() lost when the job 410s mid-wait", async () => {
    const sb = await Sandbox.create({ apiKey: "k", baseUrl });
    state.jobGone = { status: 410, body: { code: "gone", detail: "aged out" } };
    const r = await sb.run("echo hi");
    assert.equal(r.lost, true);
    assert.equal(r.exitCode, -1);
  });

  it("run() does not mark lost on a too_many_execs poll", async () => {
    const sb = await Sandbox.create({ apiKey: "k", baseUrl });
    state.jobGone = { status: 409, body: { code: "too_many_execs", detail: "wait" } };
    await assert.rejects(() => sb.run("echo hi"), (err) => err instanceof TooManyExecsError);
  });
});

describe("request id and Retry-After", () => {
  it("every request sends X-Request-Id", async () => {
    const c = new Client({ apiKey: "k", baseUrl });
    await c.request("GET", "/v1/volumes");
    assert.ok(state.log.at(-1).reqId);
    assert.equal(c.lastRequestId, state.log.at(-1).reqId);
  });

  it("an error carries requestId", async () => {
    const c = new Client({ apiKey: "k", baseUrl, maxRetries: 0 });
    state.failNext = { status: 500, body: { detail: "boom" } };
    await assert.rejects(() => c.request("GET", "/v1/volumes"), (err) => {
      assert.ok(err.requestId);
      assert.equal(err.requestId, c.lastRequestId);
      return true;
    });
  });

  it("retries reuse the same request id", async () => {
    const c = new Client({ apiKey: "k", baseUrl, maxRetries: 2, retryBackoffMs: 5 });
    state.failQueue = [
      { status: 503, body: { detail: "busy" } },
      { status: 503, body: { detail: "still busy" } },
    ];
    await c.listVolumes();
    const ids = state.log.filter((l) => l.path === "/v1/volumes").map((l) => l.reqId);
    assert.equal(ids.length, 3);
    assert.equal(new Set(ids).size, 1);
  });

  it("close retries 429 honouring Retry-After", async () => {
    const client = new Client({ apiKey: "k", baseUrl, retryBackoffMs: 5 });
    const sb = await Sandbox.create({ client });
    state.failQueue = [
      {
        status: 429,
        body: { detail: "slow", code: "rate_limited" },
        headers: { "Retry-After": "0" },
      },
    ];
    await sb.close();
    const deletes = state.log.filter(
      (l) => l.method === "DELETE" && l.path.startsWith("/v1/machines/"),
    );
    assert.equal(deletes.length, 2);
  });

  it("retryDelayMs honours Retry-After above the 8s backoff cap", () => {
    const err = new RateLimitError(429, "slow", "rate_limited", {}, { retryAfter: 12 });
    const d = retryDelayMs(0, 10, 8000, err, () => 0.5);
    assert.ok(d >= 12_000, `delay ${d} must be at least Retry-After 12s`);
    assert.ok(d <= 60_000, `delay ${d} must cap at 60s`);
  });

  it("Retry-After is spread so refused callers do not return together", () => {
    const err = new RateLimitError(429, "slow", "rate_limited", {}, { retryAfter: 2 });
    const lo = retryDelayMs(0, 10, 8000, err, () => 0);
    const hi = retryDelayMs(0, 10, 8000, err, () => 0.999);
    assert.ok(lo >= 2000, `lo ${lo}`);
    assert.ok(hi - lo > 900, `spread ${hi - lo} too small`);
    assert.ok(hi <= 3000, `hi ${hi}`);
  });

  it("parseRetryAfter caps at 60 and rejects junk", () => {
    assert.equal(parseRetryAfter("999"), 60);
    assert.equal(parseRetryAfter("12"), 12);
    assert.equal(parseRetryAfter("nope"), undefined);
    assert.equal(parseRetryAfter("-1"), undefined);
  });

  it("an error lifts retryable and docUrl", async () => {
    const c = new Client({ apiKey: "k", baseUrl, maxRetries: 0 });
    state.failNext = {
      status: 409,
      body: {
        code: "too_many_execs",
        detail: "wait",
        retryable: true,
        doc_url: "https://docs.sistemo.io/errors/too_many_execs",
      },
    };
    await assert.rejects(() => c.request("GET", "/v1/volumes"), (err) => {
      assert.ok(err instanceof TooManyExecsError);
      assert.equal(err.retryable, true);
      assert.equal(err.docUrl, "https://docs.sistemo.io/errors/too_many_execs");
      return true;
    });
  });

  it("retryable absent is undefined, not false", async () => {
    const c = new Client({ apiKey: "k", baseUrl, maxRetries: 0 });
    state.failNext = { status: 500, body: { detail: "boom" } };
    await assert.rejects(() => c.request("GET", "/v1/volumes"), (err) => {
      assert.equal(err.retryable, undefined);
      assert.equal(err.docUrl, undefined);
      return true;
    });
  });

  it("retryable false stays false", () => {
    const err = new APIError(400, "bad", "validation_error", { retryable: false });
    assert.equal(err.retryable, false);
  });
});

describe("hung body", () => {
  // ⚠ 0.1.3 cleared the abort timer when fetch() resolved (headers only), so a
  // hung body waited on Node's default undici bodyTimeout — 300 s. Keep the
  // abort live through res.text() or this test fails at the 2s timeout.
  it("aborts a hung body instead of waiting 300s", { timeout: 2000 }, async () => {
    const hung = createServer((req, res) => {
      res.writeHead(200, { "Content-Type": "application/json", "Content-Length": "100" });
      if (typeof res.flushHeaders === "function") res.flushHeaders();
      res.write(" ");
      // never res.end() — body stays open
    });
    await new Promise((r) => hung.listen(0, "127.0.0.1", r));
    const hungUrl = `http://127.0.0.1:${hung.address().port}`;
    try {
      const c = new Client({ apiKey: "k", baseUrl: hungUrl, timeoutMs: 80, maxRetries: 0 });
      const t0 = Date.now();
      await assert.rejects(() => c.listVolumes(), (err) => {
        assert.ok(err instanceof SistemoError);
        assert.match(err.message, /connection error/);
        return true;
      });
      assert.ok(Date.now() - t0 < 500, `hung body took ${Date.now() - t0}ms; abort is not live through the body read`);
    } finally {
      hung.closeAllConnections?.();
      await new Promise((r) => hung.close(r));
    }
  });
});

describe("invalid JSON 200", () => {
  // JSON.parse sat outside the fetch try/catch, so a proxy HTML 200 leaked
  // SyntaxError — not a SistemoError, and GET did not retry.
  it("html 200 is a SistemoError", async () => {
    const srv = createServer((_req, res) => {
      res.writeHead(200, { "Content-Type": "text/html" });
      res.end("<html>nope</html>");
    });
    await new Promise((r) => srv.listen(0, "127.0.0.1", r));
    try {
      const c = new Client({
        apiKey: "k",
        baseUrl: `http://127.0.0.1:${srv.address().port}`,
        maxRetries: 0,
      });
      await assert.rejects(() => c.listVolumes(), (err) => {
        assert.ok(err instanceof SistemoError);
        assert.ok(!(err instanceof APIError));
        assert.match(err.message, /invalid JSON/);
        return true;
      });
    } finally {
      await new Promise((r) => srv.close(r));
    }
  });

  it("json null 200 is a SistemoError", async () => {
    const srv = createServer((_req, res) => {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end("null");
    });
    await new Promise((r) => srv.listen(0, "127.0.0.1", r));
    try {
      const c = new Client({
        apiKey: "k",
        baseUrl: `http://127.0.0.1:${srv.address().port}`,
        maxRetries: 0,
      });
      await assert.rejects(() => c.listVolumes(), (err) => {
        assert.ok(err instanceof SistemoError);
        assert.match(err.message, /expected an object/);
        return true;
      });
    } finally {
      await new Promise((r) => srv.close(r));
    }
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

describe("insufficient balance mapping", () => {
  let srv, base;

  before(async () => {
    srv = createServer((_req, res) => {
      res.writeHead(402, { "content-type": "application/json" });
      res.end(JSON.stringify({ code: "insufficient_balance", detail: "balance is 0" }));
    });
    await new Promise((r) => srv.listen(0, "127.0.0.1", r));
    base = `http://127.0.0.1:${srv.address().port}`;
  });
  after(() => srv.close());

  it("raises InsufficientBalanceError for 402", async () => {
    const c = new Client({ apiKey: "sk_test", baseUrl: base, maxRetries: 0 });
    await assert.rejects(() => c.listVolumes(), (err) => {
      assert.ok(err instanceof InsufficientBalanceError);
      assert.ok(!(err instanceof AuthError));
      assert.ok(!(err instanceof QuotaExceededError));
      assert.ok(err instanceof APIError);
      return true;
    });
  });
});

describe("409 is not a balance error", () => {
  it("exec_id_in_use stays a plain APIError", async () => {
    const c = new Client({ apiKey: "k", baseUrl, maxRetries: 0 });
    state.failNext = { status: 409, body: { code: "exec_id_in_use", detail: "in use" } };
    await assert.rejects(() => c.listVolumes(), (err) => {
      assert.ok(err instanceof APIError);
      assert.ok(!(err instanceof InsufficientBalanceError));
      assert.ok(!(err instanceof TooManyExecsError));
      return true;
    });
  });

  it("maps each start-refused 409 to its class", async () => {
    const cases = [
      ["too_many_execs", TooManyExecsError],
      ["machine_process_limit", ProcessLimitError],
      ["machine_disk_full", DiskFullError],
      ["machine_out_of_memory", OutOfMemoryError],
      ["machine_not_running", MachineNotRunningError],
    ];
    for (const [code, cls] of cases) {
      state.failNext = { status: 409, body: { code, detail: "x" } };
      const c = new Client({ apiKey: "k", baseUrl, maxRetries: 0 });
      await assert.rejects(() => c.request("GET", "/v1/volumes"), (err) => {
        assert.ok(err instanceof cls, `${code} should be ${cls.name}`);
        assert.ok(err instanceof APIError);
        assert.ok(!(err instanceof InsufficientBalanceError));
        return true;
      });
    }
  });
});

// `await using` is syntax. Node 20 (CI) and this host's Node 22 reject it at
// parse time, so the proof lives in a child script and is skipped where the
// runtime cannot parse it. Node 24 in CI runs it.
function runtimeParsesAwaitUsing() {
  const r = spawnSync(
    process.execPath,
    ["--input-type=module", "-e", "await using x = { async [Symbol.asyncDispose]() {} };"],
    { encoding: "utf8" },
  );
  return r.status === 0;
}
const parsesAwaitUsing = runtimeParsesAwaitUsing();

describe("0.1.8 surface", () => {
  it("quotas keep unknown fields", async () => {
    const c = new Client({ apiKey: "k", baseUrl });
    const quotas = await c.getQuotas();
    assert.equal(quotas.tier, "free");
    assert.equal(quotas.quotas[0].name, "machines");
    assert.equal(quotas.per_machine.vcpus, 2);
    assert.equal(quotas.future_field, "kept");
    assert.equal(state.log.at(-1).path, "/v1/quotas");
  });

  it("images keep unknown fields", async () => {
    const c = new Client({ apiKey: "k", baseUrl });
    const images = await c.listImages();
    assert.equal(images[0].name, "base");
    assert.equal(images[0].future, 1);
    assert.equal(state.log.at(-1).path, "/v1/images");
  });

  it("null image catalog is an empty list", async () => {
    const c = new Client({ apiKey: "k", baseUrl, maxRetries: 0 });
    state.failNext = { status: 200, body: { images: null } };
    assert.deepEqual(await c.listImages(), []);
  });

  it("stop and boot are not exec start", async () => {
    const sb = await Sandbox.create({ apiKey: "k", baseUrl });
    await sb.stop();
    const stop = state.log.at(-1);
    assert.equal(stop.method, "POST");
    assert.equal(stop.path, `/v1/machines/${sb.id}/stop`);
    assert.equal(sb.state, "stopped");
    await sb.boot();
    const boot = state.log.at(-1);
    assert.equal(boot.path, `/v1/machines/${sb.id}/start`);
    assert.equal(sb.state, "running");
    await sb.start("echo hi");
    const started = state.log.at(-1);
    assert.ok(started.path.endsWith("/execs"));
    assert.equal(started.body.script, "echo hi");
  });

  it("connect keeps rootVolumeId and does not wait", async () => {
    state.machines.set("m-9", { id: "m-9", state: "provisioning", root_volume_id: "vol-keep" });
    const sb = await Sandbox.connect("m-9", { apiKey: "k", baseUrl });
    assert.equal(sb.state, "provisioning");
    assert.equal(sb.rootVolumeId, "vol-keep");
    assert.equal(state.log.filter((l) => l.method === "GET").length, 1);
  });

  it("wait returns immediately when running and keeps rootVolumeId", async () => {
    const sb = await Sandbox.create({ apiKey: "k", baseUrl, rootVolumeId: "vol-1" });
    const started = performance.now();
    await sb.wait(30_000, { pollIntervalMs: 30_000 });
    assert.ok(performance.now() - started < 2000);
    assert.equal(sb.state, "running");
    assert.equal(sb.rootVolumeId, "vol-1");
  });

  it("wait fails immediately when stopped or error", async () => {
    const sb = await Sandbox.create({ apiKey: "k", baseUrl });
    for (const st of ["stopped", "error"]) {
      state.machines.get(sb.id).state = st;
      state.log = [];
      const started = performance.now();
      await assert.rejects(() => sb.wait(30_000, { pollIntervalMs: 30_000 }), (e) => {
        assert.ok(e instanceof SistemoError);
        assert.ok(!(e instanceof MachineTimeout));
        assert.match(e.message, new RegExp(st));
        return true;
      });
      assert.ok(performance.now() - started < 2000);
      assert.equal(state.log.length, 1);
    }
  });

  it("wait polls until running", async () => {
    const sb = await Sandbox.create({ apiKey: "k", baseUrl });
    state.machines.get(sb.id).state = "starting";
    state.machineGets = 0;
    state.promoteAfter = 2;
    await sb.wait(5000, { pollIntervalMs: 10 });
    assert.equal(sb.state, "running");
    assert.ok(state.machineGets >= 2);
  });

  it("wait requires a deadline and enforces it", async () => {
    const sb = await Sandbox.create({ apiKey: "k", baseUrl });
    state.machines.get(sb.id).state = "provisioning";
    await assert.rejects(() => sb.wait(undefined), SistemoError);
    await assert.rejects(() => sb.wait(0, { pollIntervalMs: 30_000 }), MachineTimeout);
    assert.equal(sb.state, "provisioning");
  });

  it("snapshots round-trip and page", async () => {
    const sb = await Sandbox.create({ apiKey: "k", baseUrl });
    const created = await sb.createSnapshot("before");
    assert.equal(created.name, "before");
    assert.equal(created.extra, "kept");
    assert.equal(state.log.at(-1).path, `/v1/machines/${sb.id}/snapshots`);
    const page = await sb.listSnapshots();
    assert.equal(page.snapshots[0].id, created.id);
    const restored = await sb.restoreSnapshot(created.id);
    assert.equal(state.log.at(-1).path, `/v1/snapshots/${created.id}/restore`);
    assert.equal(restored.restored, true);
    const deleted = await sb.deleteSnapshot(created.id);
    assert.equal(deleted.status, "deleted");
    const c = new Client({ apiKey: "k", baseUrl });
    state.paginate = true;
    const first = await c.listSnapshots({ limit: 1 });
    assert.equal(first.nextCursor, "p2");
    const second = await c.listSnapshots({ cursor: first.nextCursor });
    assert.equal(second.nextCursor, null);
    const ids = [];
    for await (const s of c.iterSnapshots()) ids.push(s.id);
    assert.deepEqual(ids, ["s1", "s2"]);
  });

  it("null snapshot list is empty", async () => {
    const c = new Client({ apiKey: "k", baseUrl, maxRetries: 0 });
    state.failNext = { status: 200, body: { snapshots: null, next_cursor: null } };
    assert.deepEqual((await c.listSnapshots()).snapshots, []);
  });

  it("asyncDispose closes and swallows a close error", async () => {
    const sb = await Sandbox.create({ apiKey: "k", baseUrl });
    const id = sb.id;
    await sb[Symbol.asyncDispose]();
    assert.equal(state.machines.has(id), false);

    // maxRetries: 0 — DELETE retries 5xx, and a retry would hide the error
    // this is supposed to swallow.
    const sb2 = await Sandbox.create({ apiKey: "k", baseUrl, maxRetries: 0 });
    state.failNext = { status: 500, body: { detail: "no" } };
    const mark = state.log.length;
    await sb2[Symbol.asyncDispose]();
    // The id is reused once the first machine is gone (`m-${size+1}`), so
    // count only the deletes this call made.
    const deletes = state.log.slice(mark).filter((l) => l.method === "DELETE");
    assert.equal(deletes.length, 1);
    assert.equal(state.machines.has(sb2.id), true);
  });

  it("await using runs close", { skip: !parsesAwaitUsing }, async () => {
    // A child, not this file: `await using` must not live in a module Node 20
    // has to parse. spawn (not spawnSync) so this process can still serve the mock.
    const dir = mkdtempSync(join(tmpdir(), "sdk-using-"));
    const file = join(dir, "use.mjs");
    const sdk = fileURLToPath(new URL("../dist/index.js", import.meta.url));
    writeFileSync(
      file,
      `import { Sandbox } from ${JSON.stringify(sdk)};
const baseUrl = process.argv[2];
{
  await using sb = await Sandbox.create({ apiKey: "k", baseUrl });
  console.log(sb.id);
}
`,
    );
    try {
      const r = await new Promise((resolve) => {
        const child = spawn(process.execPath, [file, baseUrl]);
        let stdout = "";
        let stderr = "";
        child.stdout.on("data", (d) => {
          stdout += d;
        });
        child.stderr.on("data", (d) => {
          stderr += d;
        });
        child.on("close", (status) => resolve({ status, stdout, stderr }));
      });
      assert.equal(r.status, 0, r.stderr);
      assert.equal(state.machines.has(r.stdout.trim()), false);
    } finally {
      rmSync(dir, { recursive: true, force: true });
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

describe("locked network and secrets (0.2.0)", () => {
  const createBody = () => state.log.filter((e) => e.method === "POST" && e.path === "/v1/machines").at(-1).body;
  const mk = (o = {}) => Sandbox.create({ apiKey: "sk_test", baseUrl, ...o });

  it("omitted network sends nothing, so the default stays the server's", async () => {
    const sb = await mk();
    const body = createBody();
    for (const k of ["network", "allow", "secrets"]) assert.equal(k in body, false, k);
    assert.equal(sb.network, "open");
  });

  it("locked with an empty allow-list is sent as an empty list", async () => {
    const sb = await mk({ network: "locked", allow: [] });
    assert.equal(createBody().network, "locked");
    assert.deepEqual(createBody().allow, []);
    assert.equal(sb.network, "locked");
  });

  it("binds secrets on create and through sb.secrets", async () => {
    const sb = await mk({ network: "locked", allow: ["api.example.com"], secrets: ["STRIPE_KEY"] });
    assert.deepEqual(createBody().secrets, ["STRIPE_KEY"]);
    assert.deepEqual(sb.secrets.names, ["STRIPE_KEY"]);
    assert.deepEqual(await sb.secrets.bind("DB_URL"), ["DB_URL", "STRIPE_KEY"]);
    assert.deepEqual(await sb.secrets.unbind("STRIPE_KEY"), ["DB_URL"]);
    assert.deepEqual(state.log.at(-1).body, { bind: [], unbind: ["STRIPE_KEY"] });
  });

  it("an unknown secret name is an APIError and changes nothing", async () => {
    const sb = await mk();
    await assert.rejects(sb.secrets.bind("NOPE"), (e) => e instanceof APIError && e.status === 400);
    assert.deepEqual(sb.secrets.names, []);
  });

  it("a bare-string allow is a TypeError, and nothing is sent", async () => {
    const sb = await mk();
    const n = state.log.length;
    await assert.rejects(sb.setNetwork("locked", "pypi.org"), TypeError);
    await assert.rejects(Sandbox.create({ apiKey: "sk_test", baseUrl, network: "locked", allow: "pypi.org" }), TypeError);
    assert.equal(state.log.length, n);
  });

  it("allow without locked is refused locally; open with no list is fine", async () => {
    const sb = await mk();
    const n = state.log.length;
    await assert.rejects(sb.setNetwork("open", ["pypi.org"]), SistemoError);
    await assert.rejects(Sandbox.create({ apiKey: "sk_test", baseUrl, allow: ["pypi.org"] }), SistemoError);
    assert.equal(state.log.length, n);
    await sb.setNetwork("open");
    assert.equal(sb.network, "open");
  });

  it("an unknown network value says so, and nothing is sent", async () => {
    const sb = await mk();
    const n = state.log.length;
    for (const bad of ["closed", "LOCKED"]) {
      await assert.rejects(sb.setNetwork(bad, ["pypi.org"]), /network must be "open" or "locked"/);
    }
    assert.equal(state.log.length, n);
  });

  it("setNetwork is retried on a 503 (a whole-list replace is safe to repeat)", async () => {
    const sb = await mk();
    state.failNext = { status: 503, body: { code: "unavailable", detail: "busy" } };
    await sb.setNetwork("locked", ["pypi.org"]);
    const posts = state.log.filter((e) => e.method === "POST" && e.path.endsWith("/network"));
    assert.equal(posts.length, 2);
    assert.deepEqual(sb.allow, ["pypi.org"]);
  });

  it("secrets.names is a copy — changing it changes nothing", async () => {
    const sb = await mk({ secrets: ["K"] });
    sb.secrets.names.push("FAKE");
    assert.deepEqual(sb.secrets.names, ["K"]);
  });

  it("setNetwork sends the whole list; egress passes its filters", async () => {
    const sb = await mk();
    await sb.setNetwork("locked", ["pypi.org", "files.pythonhosted.org"]);
    assert.deepEqual(state.log.at(-1).body, { network: "locked", allow: ["pypi.org", "files.pythonhosted.org"] });
    assert.deepEqual([sb.network, sb.allow], ["locked", ["pypi.org", "files.pythonhosted.org"]]);
    const log = await sb.egress({ action: "blocked", limit: 10 });
    assert.equal(log.activity[0].host, "evil.example");
    assert.equal(state.log.at(-1).fullPath, `/v1/machines/${sb.id}/egress/activity?action=blocked&limit=10`);
  });

  it("connect reads network and secrets", async () => {
    const sb = await mk({ network: "locked", allow: ["a.example.com"], secrets: ["K"] });
    const again = await Sandbox.connect(sb.id, { apiKey: "sk_test", baseUrl });
    assert.deepEqual([again.network, again.allow, again.secrets.names], ["locked", ["a.example.com"], ["K"]]);
  });

  it("listSecrets returns names, never values", async () => {
    const list = await new Client({ apiKey: "sk_test", baseUrl }).listSecrets();
    assert.equal(list[0].name, "STRIPE_KEY");
    assert.equal("value" in list[0], false);
  });
});
