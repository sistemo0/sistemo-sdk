/**
 * Sistemo — run AI agents and untrusted code in real isolated microVMs.
 *
 * ```ts
 * import { Sandbox } from "@sistemo/sdk";
 *
 * const sb = await Sandbox.create();              // reads SISTEMO_API_KEY
 * const res = await sb.run("python3 -c 'print(2 + 2)'");
 * console.log(res.stdout, res.exitCode);
 * await sb.close();
 * ```
 */

const DEFAULT_BASE_URL = "https://api.sistemo.io";

// Sent on every request from non-browser runtimes (Node/Bun/Deno). A real
// User-Agent is required: the production WAF (Cloudflare) blocks generic
// library UAs with a 403. Browsers ignore attempts to set User-Agent and send
// their own (which the WAF allows), so this is a no-op there.
const USER_AGENT = "@sistemo/sdk/0.1.2";

const RETRY_STATUSES = new Set([408, 429, 500, 502, 503, 504]);

export class SistemoError extends Error {}

export class APIError extends SistemoError {
  readonly status: number;
  readonly detail: string;
  readonly code?: string;
  /**
   * The parsed response body, when there was one.
   *
   * Some errors carry a field the caller genuinely needs — a failed `start()`
   * returns the `exec_id` of a command that may well be running, and throwing
   * that away would leave it unaddressable.
   */
  readonly body: Record<string, unknown>;
  constructor(status: number, detail: string, code?: string, body?: Record<string, unknown>) {
    super(`[${status}] ${detail}${code ? ` (${code})` : ""}`);
    this.name = "APIError";
    this.status = status;
    this.detail = detail;
    this.code = code;
    this.body = body ?? {};
  }
}

/** 401/403 — key missing, invalid, revoked, or a read-only key on a mutation. */
export class AuthError extends APIError {}
/**
 * 403 `quota_exceeded` — an account limit was reached.
 *
 * Deliberately NOT an `AuthError`, even though both arrive as 403. They need
 * opposite handling, and conflating them sends callers to the wrong place:
 *
 * - `AuthError` — the credential cannot do this. Retrying never helps.
 * - `QuotaExceededError` — the credential is fine and the request is valid.
 *   **Retrying succeeds once the resource is freed** (stop or destroy a machine,
 *   delete a volume) or the limit is raised.
 *
 * `detail` names the dimension that bound and how much is in use; `GET /v1/quotas`
 * reports every limit alongside current usage. A message mentioning "fleet limit"
 * is a fleet-wide ceiling rather than your plan, which topping up will not lift.
 */
export class QuotaExceededError extends APIError {}

/** 404 — resource not found (or not yours). */
export class NotFoundError extends APIError {}
/** 429 — rate limited. */
export class RateLimitError extends APIError {}

/**
 * 410 — it existed and is no longer retained.
 *
 * Deliberately NOT a `NotFoundError`. Telling a caller their job never existed,
 * when in fact its record or output aged out, invites them to re-run work that
 * already ran.
 */
export class GoneError extends APIError {}

/**
 * 501 — this machine's image predates async exec.
 *
 * **Do not retry.** The in-guest agent is baked into the image, so the answer
 * cannot change until the image is rebuilt (or the agent is updated in place).
 * Deliberately distinct from a 502/503, which mean "ask again".
 */
export class AsyncExecUnsupportedError extends APIError {}

/**
 * 503 — we could not confirm the command started, and it **may be running**.
 *
 * This is not a failure. The most dangerous thing a caller can do here is treat
 * it as one and start the command again. `execId` is the handle:
 *
 * ```ts
 * try {
 *   job = await sb.start("./deploy.sh");
 * } catch (e) {
 *   if (e instanceof ExecStartUnconfirmed) job = await sb.job(e.execId);
 * }
 * ```
 */
export class ExecStartUnconfirmed extends APIError {
  get execId(): string {
    return String(this.body.exec_id ?? "");
  }
}

function apiError(
  status: number,
  detail: string,
  code?: string,
  body?: Record<string, unknown>,
): APIError {
  // 403 is two different things, told apart by `code` — never by the status.
  if (status === 403 && code === "quota_exceeded") return new QuotaExceededError(status, detail, code, body);
  if (status === 401 || status === 403) return new AuthError(status, detail, code, body);
  if (status === 404) return new NotFoundError(status, detail, code, body);
  if (status === 410) return new GoneError(status, detail, code, body);
  if (status === 429) return new RateLimitError(status, detail, code, body);
  if (status === 501) return new AsyncExecUnsupportedError(status, detail, code, body);
  // ⚠ Only a 503 that NAMES an exec is the unconfirmed-start case. A plain 503
  // (no host available, say) is an ordinary retryable error and must not be
  // dressed up as a command that might be running.
  if (status === 503 && body && body.exec_id) return new ExecStartUnconfirmed(status, detail, code, body);
  return new APIError(status, detail, code, body);
}

function newIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `idem-${Date.now()}-${Math.random().toString(36).slice(2, 12)}`;
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

export interface ClientOptions {
  apiKey?: string;
  baseUrl?: string;
  /** Per-request timeout in ms (default 120000 — create blocks until boot). */
  timeoutMs?: number;
  /** Extra attempts after the first (default 2 → up to 3 tries). */
  maxRetries?: number;
  /** Base backoff in ms (default 400). */
  retryBackoffMs?: number;
  /** Cap on backoff in ms (default 8000). */
  retryMaxBackoffMs?: number;
}

export interface RequestOptions {
  headers?: Record<string, string>;
  /** Enables safe HTTP retries for mutating methods. */
  idempotencyKey?: string;
  /** Override client maxRetries for this call. */
  maxRetries?: number;
}

/** Low-level API client. Most users want {@link Sandbox} instead. */
export class Client {
  readonly baseUrl: string;
  private readonly apiKey: string;
  private readonly timeoutMs: number;
  private readonly maxRetries: number;
  private readonly retryBackoffMs: number;
  private readonly retryMaxBackoffMs: number;

  constructor(opts: ClientOptions = {}) {
    const g = globalThis as { process?: { env?: Record<string, string | undefined> } };
    const env = g.process?.env ?? {};
    const apiKey = opts.apiKey ?? env.SISTEMO_API_KEY;
    if (!apiKey) {
      throw new SistemoError(
        "no API key — pass { apiKey } or set the SISTEMO_API_KEY environment variable",
      );
    }
    this.apiKey = apiKey;
    this.baseUrl = (opts.baseUrl ?? env.SISTEMO_BASE_URL ?? DEFAULT_BASE_URL).replace(/\/$/, "");
    this.timeoutMs = opts.timeoutMs ?? 120_000;
    this.maxRetries = opts.maxRetries ?? 2;
    this.retryBackoffMs = opts.retryBackoffMs ?? 400;
    this.retryMaxBackoffMs = opts.retryMaxBackoffMs ?? 8_000;
  }

  async request<T>(
    method: string,
    path: string,
    body?: unknown,
    opts: RequestOptions = {},
  ): Promise<T> {
    const retries = opts.maxRetries ?? this.maxRetries;
    const methodU = method.toUpperCase();
    let lastErr: unknown;

    for (let attempt = 0; attempt <= retries; attempt++) {
      try {
        return await this.once<T>(methodU, path, body, opts);
      } catch (e) {
        lastErr = e;
        const can =
          attempt < retries &&
          (e instanceof APIError
            ? this.shouldRetryHttp(methodU, e.status, opts.idempotencyKey)
            : e instanceof SistemoError); // connection / abort
        if (!can) throw e;
        await this.backoff(attempt, e instanceof APIError ? e : undefined);
      }
    }
    throw lastErr;
  }

  private async once<T>(
    method: string,
    path: string,
    body: unknown | undefined,
    opts: RequestOptions,
  ): Promise<T> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    let res: Response;
    try {
      const headers: Record<string, string> = {
        Authorization: `Bearer ${this.apiKey}`,
        Accept: "application/json",
        "User-Agent": USER_AGENT,
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
        ...(opts.headers ?? {}),
      };
      if (opts.idempotencyKey) {
        headers["Idempotency-Key"] = opts.idempotencyKey;
      }
      res = await fetch(this.baseUrl + path, {
        method,
        headers,
        body: body !== undefined ? JSON.stringify(body) : undefined,
        signal: controller.signal,
      });
    } catch (e) {
      throw new SistemoError(`connection error: ${(e as Error).message}`);
    } finally {
      clearTimeout(timer);
    }

    const text = await res.text();
    if (!res.ok) {
      let detail = "request failed";
      let code: string | undefined;
      let body: Record<string, unknown> | undefined;
      try {
        const parsed = JSON.parse(text);
        // Some errors use `error` rather than `detail` (an unconfirmed exec
        // start is the live example), so fall back rather than reporting
        // "request failed" over a message that was right there.
        detail = parsed.detail ?? parsed.error ?? detail;
        code = parsed.code;
        if (parsed && typeof parsed === "object") body = parsed as Record<string, unknown>;
      } catch {
        /* non-JSON error body */
      }
      throw apiError(res.status, detail, code, body);
    }
    return (text ? JSON.parse(text) : {}) as T;
  }

  private shouldRetryHttp(
    method: string,
    status: number,
    idempotencyKey?: string,
  ): boolean {
    if (!RETRY_STATUSES.has(status)) return false;
    if (method === "GET" || method === "HEAD" || method === "OPTIONS" || method === "DELETE") {
      return true;
    }
    return Boolean(idempotencyKey);
  }

  private async backoff(attempt: number, err?: APIError): Promise<void> {
    const base = Math.min(
      this.retryMaxBackoffMs,
      this.retryBackoffMs * 2 ** attempt,
    );
    let delay = base * (0.5 + Math.random());
    if (err?.status === 429) delay = Math.max(delay, base);
    await sleep(delay);
  }

  // --- volumes (incl. detachable root volumes) ---------------------------

  /** List the account's volumes (root and data). */
  async listVolumes(): Promise<{ volumes: Volume[] }> {
    const out = await this.request<{ volumes?: Volume[] | null }>("GET", "/v1/volumes");
    return { volumes: out.volumes ?? [] };
  }

  /** Create a standalone data volume. */
  createVolume(name: string, sizeGb: number): Promise<Volume> {
    return this.request(
      "POST",
      "/v1/volumes",
      { name, size_gb: sizeGb },
      { idempotencyKey: newIdempotencyKey() },
    );
  }

  /** Attach a volume (root or data) to a STOPPED machine. */
  attachVolume(machineId: string, volumeId: string): Promise<{ status: string }> {
    return this.request(
      "POST",
      `/v1/machines/${machineId}/volume/attach`,
      { volume_id: volumeId },
      { idempotencyKey: newIdempotencyKey() },
    );
  }

  /**
   * Detach a volume from a STOPPED machine. A detached root volume's disk is
   * preserved and can be re-attached or booted onto a new sandbox.
   */
  detachVolume(machineId: string, volumeId: string): Promise<{ status: string }> {
    return this.request(
      "POST",
      `/v1/machines/${machineId}/volume/detach`,
      { volume_id: volumeId },
      { idempotencyKey: newIdempotencyKey() },
    );
  }

  /** Delete a detached volume. */
  deleteVolume(volumeId: string): Promise<{ status: string }> {
    return this.request("DELETE", `/v1/volumes/${volumeId}`);
  }
}

export interface Volume {
  id: string;
  name: string;
  size_gb: number;
  role: string;
  state: string;
  machine_id?: string;
}

export interface ExecResult {
  exitCode: number;
  stdout: string;
  stderr: string;
  truncated?: boolean;
}

export interface SandboxOptions extends ClientOptions {
  client?: Client;
  name?: string;
  vcpus?: number;
  memoryMb?: number;
  rootfsSizeGb?: number;
  stack?: string;
  metadata?: Record<string, unknown>;
  /** Boot from an existing (detached/preserved) root volume instead of a fresh
   * template clone. Size/stack come from the volume. */
  rootVolumeId?: string;
  /** Optional fixed Idempotency-Key for create (default: random UUID). */
  idempotencyKey?: string;
}

/** An isolated microVM you can run code in. Create with {@link Sandbox.create}. */
export class Sandbox {
  readonly id: string;
  state: string;
  private readonly client: Client;

  private constructor(client: Client, id: string, state: string) {
    this.client = client;
    this.id = id;
    this.state = state;
  }

  /** Provision a new microVM (resolves once it has booted). */
  static async create(opts: SandboxOptions = {}): Promise<Sandbox> {
    const client = opts.client ?? new Client(opts);
    const body: Record<string, unknown> = {
      name: opts.name ?? "",
      vcpus: opts.vcpus ?? 1,
      memory_mb: opts.memoryMb ?? 1024,
      rootfs_size_gb: opts.rootfsSizeGb ?? 10,
      stack: opts.stack ?? "base",
      metadata: opts.metadata ?? {},
    };
    if (opts.rootVolumeId) body.root_volume_id = opts.rootVolumeId;
    const key = opts.idempotencyKey ?? newIdempotencyKey();
    const machine = await client.request<{ id?: string; state?: string }>(
      "POST",
      "/v1/machines",
      body,
      { idempotencyKey: key },
    );
    if (!machine?.id) {
      throw new APIError(
        502,
        "create machine response missing id — refusing to continue",
        "invalid_create_response",
      );
    }
    return new Sandbox(client, machine.id, machine.state ?? "");
  }

  /**
   * Run a shell command/script inside the sandbox and return its output.
   *
   * This is start + wait on a guest job (`POST /execs`). Default timeout 120s,
   * ceiling 24 hours. A proxy never holds the connection for the whole command.
   */
  async run(script: string, timeoutSec = 120): Promise<ExecResult> {
    const execId = newIdempotencyKey();
    let job: Job;
    try {
      job = await this.start(script, timeoutSec, { execId });
    } catch (e) {
      if (e instanceof ExecStartUnconfirmed) {
        job = await this.job(execId);
      } else {
        throw e;
      }
    }
    await job.wait();
    const dec = new TextDecoder("utf-8");
    return {
      exitCode: job.exitCode ?? -1,
      stdout: dec.decode(await job.output("stdout")),
      stderr: dec.decode(await job.output("stderr")),
      truncated: job.truncated,
    };
  }

  /**
   * Start a command and return a handle without waiting.
   *
   * {@link run} is start + wait — use this to stream, cancel, or reconnect.
   * Same guest job, same timeout ceiling (24 hours).
   *
   * ⚠ You mint `execId` (a UUID is generated if you omit it). If the start
   * response is lost, poll that id rather than starting again. Re-sending the
   * same id is a 409, not a second run.
   *
   * ⚠ On {@link ExecStartUnconfirmed} the command **may be running**. The error
   * carries `execId`; poll it with {@link job} rather than starting over.
   */
  async start(
    script: string,
    timeoutSec = 120,
    opts: { execId?: string } = {},
  ): Promise<Job> {
    const execId = opts.execId ?? newIdempotencyKey();
    try {
      const rec = await this.client.request<JobRecord>(
        "POST",
        `/v1/machines/${this.id}/execs`,
        { exec_id: execId, script, timeout_sec: timeoutSec },
      );
      return new Job(this.client, this.id, rec);
    } catch (e) {
      if (e instanceof APIError && e.status === 409) {
        try {
          const rec = await this.client.request<JobRecord>(
            "GET",
            `/v1/machines/${this.id}/execs/${execId}`,
          );
          return new Job(this.client, this.id, rec);
        } catch (inner) {
          if (inner instanceof NotFoundError) throw e;
          throw inner;
        }
      }
      throw e;
    }
  }

  /** Re-attach to a job by id — after a crash, or after an unconfirmed start. */
  async job(execId: string): Promise<Job> {
    const rec = await this.client.request<JobRecord>(
      "GET",
      `/v1/machines/${this.id}/execs/${execId}`,
    );
    return new Job(this.client, this.id, rec);
  }

  /** List this sandbox's async jobs, newest first. */
  async jobs(limit = 50): Promise<Job[]> {
    const out = await this.client.request<{ execs?: JobRecord[] }>(
      "GET",
      `/v1/machines/${this.id}/execs?limit=${limit}`,
    );
    return (out.execs ?? []).map((r) => new Job(this.client, this.id, r));
  }

  /** Destroy the sandbox (and its disk unless `preserveStorage`). */
  async close(preserveStorage = false): Promise<void> {
    const q = preserveStorage ? "?preserve_storage=true" : "";
    await this.client.request("DELETE", `/v1/machines/${this.id}${q}`);
  }
}

// ── Async exec ────────────────────────────────────────────────────────

/** States that mean nothing further will happen. */
const TERMINAL = new Set(["succeeded", "failed", "cancelled", "expired", "lost"]);

interface JobRecord {
  exec_id?: string;
  machine_id?: string;
  state?: string;
  exit_code?: number | null;
  stdout_bytes?: number;
  stderr_bytes?: number;
  truncated?: boolean;
  started_at?: string | null;
  finished_at?: string | null;
}

/**
 * `wait()`/`stream()` gave up waiting.
 *
 * ⚠ The job is **still running** — this is your client's patience running out,
 * not the command's. The handle stays valid; poll it again later.
 */
export class JobTimeout extends SistemoError {}

/**
 * A handle on a command running inside a sandbox.
 *
 * The handle outlives the process that created it, so a crashed client can
 * reconnect with `sb.job(execId)` or list what is running with `sb.jobs()`.
 */
export class Job {
  id = "";
  machineId = "";
  state = "";
  /**
   * ⚠ `null` until known, and null FOREVER when `state` is `"lost"`. Never
   * coerce this to 0 — a zero here reads as "the command succeeded".
   */
  exitCode: number | null = null;
  stdoutBytes = 0;
  stderrBytes = 0;
  truncated = false;
  startedAt: string | null = null;
  finishedAt: string | null = null;

  constructor(
    private readonly client: Client,
    private readonly sandboxId: string,
    rec: JobRecord,
  ) {
    this.apply(rec);
  }

  private apply(rec: JobRecord): this {
    this.id = rec.exec_id ?? "";
    this.machineId = rec.machine_id ?? "";
    this.state = rec.state ?? "";
    this.exitCode = rec.exit_code ?? null;
    this.stdoutBytes = rec.stdout_bytes ?? 0;
    this.stderrBytes = rec.stderr_bytes ?? 0;
    this.truncated = rec.truncated ?? false;
    this.startedAt = rec.started_at ?? null;
    this.finishedAt = rec.finished_at ?? null;
    return this;
  }

  /** Whether the job reached a terminal state. */
  get done(): boolean {
    return TERMINAL.has(this.state);
  }

  /**
   * `true` only for a command that finished and exited 0.
   *
   * ⚠ A `lost` job is never ok, because we cannot say that it worked — and a
   * property that guessed would be worse than no property.
   */
  get ok(): boolean {
    return this.state === "succeeded" && this.exitCode === 0;
  }

  /**
   * The machine could not account for this job.
   *
   * ⚠ Not the same as failed. `failed` means it ran and returned non-zero;
   * `lost` means we can say neither that it ran nor that it did not.
   */
  get lost(): boolean {
    return this.state === "lost";
  }

  /** Re-read the job's state from the API. */
  async refresh(): Promise<this> {
    return this.apply(
      await this.client.request<JobRecord>(
        "GET",
        `/v1/machines/${this.sandboxId}/execs/${this.id}`,
      ),
    );
  }

  /**
   * Resolve once the job reaches a terminal state.
   *
   * The waiting happens **client-side**: each poll is a fast request, so no
   * proxy between you and the API is ever holding a long connection.
   *
   * Rejects with {@link JobTimeout} if `timeoutMs` elapses first — ⚠ which does
   * not stop the job. Call {@link cancel} if that is what you meant.
   */
  async wait(opts: { timeoutMs?: number; pollIntervalMs?: number } = {}): Promise<this> {
    const poll = opts.pollIntervalMs ?? 1000;
    const deadline = opts.timeoutMs === undefined ? undefined : Date.now() + opts.timeoutMs;
    for (;;) {
      if (this.done) return this;
      if (deadline !== undefined && Date.now() >= deadline) {
        throw new JobTimeout(
          `job ${this.id} is still ${this.state} after ${opts.timeoutMs}ms ` +
            `(it is still running; poll it again or cancel it)`,
        );
      }
      await sleep(poll);
      await this.refresh();
    }
  }

  /**
   * Stop the job: SIGTERM, a short grace period, then SIGKILL.
   *
   * Cancelling a job that already finished is a no-op, so a retried cancel is
   * safe.
   */
  async cancel(): Promise<this> {
    return this.apply(
      await this.client.request<JobRecord>(
        "DELETE",
        `/v1/machines/${this.sandboxId}/execs/${this.id}`,
      ),
    );
  }

  /**
   * Yield raw output bytes as they are produced, until the job ends.
   *
   * ⚠ Chunks split on BYTE boundaries and one can end mid-character. Use
   * {@link stream} unless you actually want bytes.
   */
  async *streamBytes(
    opts: { stream?: "stdout" | "stderr"; pollIntervalMs?: number; timeoutMs?: number; limit?: number } = {},
  ): AsyncGenerator<Uint8Array> {
    const which = opts.stream ?? "stdout";
    const poll = opts.pollIntervalMs ?? 500;
    const limit = opts.limit ?? 65536;
    const deadline = opts.timeoutMs === undefined ? undefined : Date.now() + opts.timeoutMs;
    let offset = 0;
    for (;;) {
      const page = await this.client.request<{
        next_offset?: number;
        data?: string;
        eof?: boolean;
      }>(
        "GET",
        `/v1/machines/${this.sandboxId}/execs/${this.id}/output` +
          `?stream=${which}&offset=${offset}&limit=${limit}`,
      );
      offset = page.next_offset ?? offset;
      const data = page.data ? base64ToBytes(page.data) : new Uint8Array(0);
      if (data.length > 0) {
        yield data;
        // ⚠ Loop straight back without sleeping. A producer faster than the
        // poll interval would otherwise be read at one page per interval,
        // turning a 30-second build log into minutes of trickle for no reason.
        continue;
      }
      // ⚠ Stop on eof, NEVER on an empty page. An empty page from a running job
      // means "nothing new yet"; treating it as the end truncates the output of
      // every job that pauses to think.
      if (page.eof) return;
      if (deadline !== undefined && Date.now() >= deadline) {
        throw new JobTimeout(`job ${this.id} produced no further output within ${opts.timeoutMs}ms`);
      }
      await sleep(poll);
    }
  }

  /**
   * Yield output as text as it is produced.
   *
   * ⚠ Decoding is INCREMENTAL (`TextDecoder` with `stream: true`). A page can
   * end in the middle of a multi-byte character, and decoding each page on its
   * own would corrupt every character that straddles a boundary — which, at a
   * 64 KiB page size, is rare enough to survive testing and common enough to
   * corrupt real logs.
   */
  async *stream(
    opts: { stream?: "stdout" | "stderr"; pollIntervalMs?: number; timeoutMs?: number } = {},
  ): AsyncGenerator<string> {
    const decoder = new TextDecoder("utf-8");
    for await (const chunk of this.streamBytes(opts)) {
      const text = decoder.decode(chunk, { stream: true });
      if (text) yield text;
    }
    // Flush whatever partial sequence is left, so a truncated final character
    // surfaces as U+FFFD rather than silently disappearing.
    const tail = decoder.decode();
    if (tail) yield tail;
  }

  /** Read everything written to a stream so far, without waiting. */
  async output(which: "stdout" | "stderr" = "stdout"): Promise<Uint8Array> {
    const parts: Uint8Array[] = [];
    let offset = 0;
    for (;;) {
      const page = await this.client.request<{ next_offset?: number; data?: string }>(
        "GET",
        `/v1/machines/${this.sandboxId}/execs/${this.id}/output` +
          `?stream=${which}&offset=${offset}&limit=65536`,
      );
      const data = page.data ? base64ToBytes(page.data) : new Uint8Array(0);
      if (data.length > 0) parts.push(data);
      const next = page.next_offset ?? offset;
      if (data.length === 0 || next <= offset) break;
      offset = next;
    }
    const total = parts.reduce((n, p) => n + p.length, 0);
    const out = new Uint8Array(total);
    let at = 0;
    for (const p of parts) {
      out.set(p, at);
      at += p.length;
    }
    return out;
  }

  /** {@link output} decoded as text. */
  async logs(which: "stdout" | "stderr" = "stdout"): Promise<string> {
    return new TextDecoder("utf-8").decode(await this.output(which));
  }
}

/**
 * Decode base64 without assuming a runtime.
 *
 * ⚠ `atob` produces a string of char codes, not bytes — mapping through
 * charCodeAt is what keeps binary output intact. Buffer is preferred where it
 * exists because it skips that round trip entirely.
 */
function base64ToBytes(b64: string): Uint8Array {
  const g = globalThis as { Buffer?: { from(s: string, enc: string): Uint8Array }; atob?: (s: string) => string };
  if (typeof g.Buffer !== "undefined") return new Uint8Array(g.Buffer.from(b64, "base64"));
  if (typeof g.atob === "function") {
    const bin = g.atob(b64);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }
  throw new SistemoError("no base64 decoder available in this runtime");
}
