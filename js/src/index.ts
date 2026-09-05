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
const USER_AGENT = "@sistemo/sdk/0.1.1";

const RETRY_STATUSES = new Set([408, 429, 500, 502, 503, 504]);

export class SistemoError extends Error {}

export class APIError extends SistemoError {
  readonly status: number;
  readonly detail: string;
  readonly code?: string;
  constructor(status: number, detail: string, code?: string) {
    super(`[${status}] ${detail}${code ? ` (${code})` : ""}`);
    this.name = "APIError";
    this.status = status;
    this.detail = detail;
    this.code = code;
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

function apiError(status: number, detail: string, code?: string): APIError {
  // 403 is two different things, told apart by `code` — never by the status.
  if (status === 403 && code === "quota_exceeded") return new QuotaExceededError(status, detail, code);
  if (status === 401 || status === 403) return new AuthError(status, detail, code);
  if (status === 404) return new NotFoundError(status, detail, code);
  if (status === 429) return new RateLimitError(status, detail, code);
  return new APIError(status, detail, code);
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
      try {
        const parsed = JSON.parse(text);
        detail = parsed.detail ?? detail;
        code = parsed.code;
      } catch {
        /* non-JSON error body */
      }
      throw apiError(res.status, detail, code);
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

  /** Run a shell command/script inside the sandbox. */
  async run(script: string, timeoutSec = 30): Promise<ExecResult> {
    // No Idempotency-Key: HTTP errors are not retried (would re-run the script).
    // Connection failures still use the client's connection-retry path only when
    // they throw SistemoError before a response — see Client.request.
    const out = await this.client.request<{
      exit_code?: number;
      stdout?: string;
      stderr?: string;
    }>("POST", `/v1/machines/${this.id}/exec`, { script, timeout_sec: timeoutSec });
    return {
      exitCode: out.exit_code ?? -1,
      stdout: out.stdout ?? "",
      stderr: out.stderr ?? "",
    };
  }

  /** Destroy the sandbox (and its disk unless `preserveStorage`). */
  async close(preserveStorage = false): Promise<void> {
    const q = preserveStorage ? "?preserve_storage=true" : "";
    await this.client.request("DELETE", `/v1/machines/${this.id}${q}`);
  }
}
