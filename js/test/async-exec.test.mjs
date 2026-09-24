/**
 * Async exec tests for @sistemo/sdk against a mock control plane.
 * Run: npm test
 */

import { createServer } from "node:http";
import { after, before, beforeEach, describe, it } from "node:test";
import assert from "node:assert/strict";
import {
  AsyncExecUnsupportedError,
  ExecStartUnconfirmed,
  GoneError,
  JobTimeout,
  NotFoundError,
  Sandbox,
} from "../dist/index.js";

const scene = {};

function jobRec(state = "running", exitCode = null, extra = {}) {
  return {
    exec_id: "11111111-2222-3333-4444-555555555555",
    machine_id: "m1",
    state,
    exit_code: exitCode,
    stdout_bytes: 0,
    stderr_bytes: 0,
    truncated: false,
    ...extra,
  };
}

function page(offset, bytes, eof = false) {
  return {
    stream: "stdout",
    offset,
    next_offset: offset + bytes.length,
    data: Buffer.from(bytes).toString("base64"),
    eof,
  };
}

let server, baseUrl;

before(async () => {
  server = createServer((req, res) => {
    const url = new URL(req.url, "http://x");
    const send = (status, body) => {
      const raw = JSON.stringify(body);
      res.writeHead(status, { "content-type": "application/json", "content-length": Buffer.byteLength(raw) });
      res.end(raw);
    };
    let bodyChunks = [];
    req.on("data", (c) => bodyChunks.push(c));
    req.on("end", () => {
      if (req.method === "POST" && url.pathname.endsWith("/execs")) {
        let parsed = {};
        try { parsed = JSON.parse(Buffer.concat(bodyChunks).toString() || "{}"); } catch { /* ignore */ }
        (scene.starts ??= []).push(parsed.exec_id);
        if (scene.startStatus) return send(scene.startStatus, scene.startBody ?? {});
        return send(202, { ...scene.job, exec_id: parsed.exec_id ?? scene.job.exec_id });
      }
      if (req.method === "POST") return send(201, { id: "m1", state: "running" });
      if (req.method === "DELETE") return send(200, scene.cancelled ?? scene.job);
      if (url.pathname.endsWith("/output")) {
        const offset = Number(url.searchParams.get("offset") ?? 0);
        const found = (scene.pages ?? []).find((p) => p.offset === offset);
        return send(200, found ?? { stream: "stdout", offset, next_offset: offset, data: "", eof: true });
      }
      if (url.pathname.endsWith("/execs")) return send(200, { execs: [scene.job] });
      return send(200, scene.job);
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  baseUrl = `http://127.0.0.1:${server.address().port}`;
});

after(() => server.close());
beforeEach(() => {
  for (const k of Object.keys(scene)) delete scene[k];
  scene.job = jobRec();
});

// A real Sandbox via the documented entry point, so the test exercises the
// same construction path a user gets.
async function newSandbox() {
  return await Sandbox.create({ apiKey: "sk_live_test", baseUrl });
}

describe("the exit-code contract", () => {
  it("a running job has no exit code — null, never 0", async () => {
    const sb = await newSandbox();
    const j = await sb.job("x");
    assert.equal(j.exitCode, null);
    assert.equal(j.done, false);
    assert.equal(j.ok, false);
  });

  it("lost is not failed, and is never ok", async () => {
    scene.job = jobRec("lost");
    const sb = await newSandbox();
    const j = await sb.job("x");
    assert.equal(j.done, true);
    assert.equal(j.lost, true);
    assert.equal(j.exitCode, null);
    assert.equal(j.ok, false, "a lost job must never report ok — we do not know that it worked");
  });

  it("a failed job is done but not ok", async () => {
    scene.job = jobRec("failed", 7);
    const sb = await newSandbox();
    const j = await sb.job("x");
    assert.equal(j.done, true);
    assert.equal(j.ok, false);
    assert.equal(j.exitCode, 7);
  });

  it("a succeeded job is ok", async () => {
    scene.job = jobRec("succeeded", 0);
    const sb = await newSandbox();
    assert.equal((await sb.job("x")).ok, true);
  });
});

describe("streaming", () => {
  it("decodes a multi-byte character split across two pages", async () => {
    // '€' is three bytes; splitting it is rare enough to survive testing and
    // common enough to corrupt real logs.
    const euro = Buffer.from("€", "utf8");
    scene.pages = [
      page(0, Buffer.concat([Buffer.from("cost: "), euro.subarray(0, 1)])),
      page(7, Buffer.concat([euro.subarray(1), Buffer.from("9\n")]), true),
    ];
    scene.job = jobRec("succeeded", 0);
    const sb = await newSandbox();
    const j = await sb.job("x");
    let out = "";
    for await (const c of j.stream({ pollIntervalMs: 0 })) out += c;
    assert.equal(out, "cost: €9\n");
  });

  it("an empty page from a running job is not the end", async () => {
    scene.pages = [
      page(0, Buffer.from("start\n")),
      { stream: "stdout", offset: 6, next_offset: 6, data: "", eof: false },
    ];
    scene.job = jobRec("running");
    const sb = await newSandbox();
    const j = await sb.job("x");
    let out = "";
    await assert.rejects(
      async () => {
        for await (const c of j.stream({ pollIntervalMs: 5, timeoutMs: 200 })) out += c;
      },
      JobTimeout,
    );
    assert.equal(out, "start\n", "the stream must not stop on an empty page");
  });

  it("binary output survives the round trip", async () => {
    const raw = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x00, 0xff, 0xfe]);
    scene.pages = [page(0, raw, true)];
    scene.job = jobRec("succeeded", 0);
    const sb = await newSandbox();
    const j = await sb.job("x");
    const parts = [];
    for await (const c of j.streamBytes({ pollIntervalMs: 0 })) parts.push(Buffer.from(c));
    assert.deepEqual(Buffer.concat(parts), raw);
  });
});

describe("start semantics", () => {
  it("mints an exec_id — without it a lost start response is unrecoverable", async () => {
    const sb = await newSandbox();
    const job = await sb.start("echo hi");
    const ids = (scene.starts ?? []).filter(Boolean);
    assert.ok(ids.length, "start() sent no exec_id");
    assert.equal(job.id, ids[0]);
  });

  it("an unconfirmed start keeps the exec id", async () => {
    scene.startStatus = 503;
    scene.startBody = {
      error: "could not confirm the command started",
      code: "unavailable",
      exec_id: "abc-123",
    };
    const sb = await newSandbox();
    await assert.rejects(
      () => sb.start("./deploy.sh"),
      (e) => {
        assert.ok(e instanceof ExecStartUnconfirmed);
        assert.equal(e.execId, "abc-123");
        return true;
      },
    );
  });

  it("a plain 503 is NOT an unconfirmed start", async () => {
    // ⚠ Only a 503 that NAMES an exec means "it may be running". Dressing up
    // an ordinary "no host available" as a possibly-running command would send
    // callers hunting for a job that was never created.
    scene.startStatus = 503;
    scene.startBody = { detail: "no host available", code: "no_host_available" };
    const sb = await newSandbox();
    await assert.rejects(
      () => sb.start("x"),
      (e) => {
        assert.ok(!(e instanceof ExecStartUnconfirmed), "a plain 503 was reported as an unconfirmed start");
        return true;
      },
    );
  });

  it("an old image is reported as unsupported, not as a retryable fault", async () => {
    scene.startStatus = 501;
    scene.startBody = { detail: "async exec is not available on this guest agent" };
    const sb = await newSandbox();
    await assert.rejects(() => sb.start("x"), AsyncExecUnsupportedError);
  });

  it("reaped output is gone, not missing", async () => {
    scene.startStatus = 410;
    scene.startBody = { detail: "output no longer retained", code: "gone" };
    const sb = await newSandbox();
    await assert.rejects(
      () => sb.start("x"),
      (e) => e instanceof GoneError && !(e instanceof NotFoundError),
    );
  });
});

describe("waiting", () => {
  it("returns immediately when already terminal", async () => {
    scene.job = jobRec("succeeded", 0);
    const sb = await newSandbox();
    const j = await sb.job("x");
    assert.equal((await j.wait({ timeoutMs: 10 })).state, "succeeded");
  });

  it("a wait timeout does not stop the job", async () => {
    scene.job = jobRec("running");
    const sb = await newSandbox();
    const j = await sb.job("x");
    await assert.rejects(
      () => j.wait({ timeoutMs: 150, pollIntervalMs: 25 }),
      (e) => e instanceof JobTimeout && /still running/.test(e.message),
    );
  });
});
