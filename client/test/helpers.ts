// A pretend Pig, pretend Web Locks, and a way to build a Core around them, for tests
// that run in Node. The pretend Pig follows docs/api.md; the browser tests in e2e/ run
// against the real one.

import { IDBFactory } from "fake-indexeddb";
import "fake-indexeddb/auto"; // IDBKeyRange and friends, as globals

import { Core, type Locks, type Timing } from "../src/core.ts";
import { makeHttp } from "../src/http.ts";
import { Store } from "../src/store.ts";
import type { Notice, Stamp, TaskParameters } from "../src/types.ts";

export const SERVER = "https://pig.test";

interface FakeTask {
  parameters: string[];
  open: boolean;
  max_event_size_bytes: number;
  /** The largest media part, for a task that takes media. */
  max_part_size?: number;
}

interface FakeMedia {
  event_id: string;
  /** Part number → its bytes, as text. */
  parts: Map<number, string>;
  finished: boolean;
}

interface FakeRun {
  status: string;
  events: Map<string, string>;
  parameters: TaskParameters;
  /** Media ID → item. */
  media: Map<number, FakeMedia>;
}

interface Request {
  method: string;
  path: string;
  body?: string;
}

type Answer = { status: number; body: unknown };

export class FakePig {
  tasks: Record<string, FakeTask>;
  runs = new Map<string, FakeRun>();
  requests: Request[] = [];
  offline = false;
  /** Answers to give instead of the real one, consumed in order. */
  upcoming: (Answer | "network")[] = [];
  /** A function that may answer a request itself: return a Response, or nothing. */
  intercept: ((method: string, path: string, body?: string) => Response | undefined) | null = null;

  constructor() {
    this.tasks = {
      stroop: { parameters: ["participant_id", "session"], open: true, max_event_size_bytes: 1024 },
    };
    this.fetch = this.fetch.bind(this);
  }

  async fetch(url: string | URL | globalThis.Request, init: RequestInit = {}): Promise<Response> {
    const { pathname } = new URL(String(url));
    const method = init.method ?? "GET";
    const body = init.body instanceof Blob ? await init.body.text() : (init.body as string | undefined);
    this.requests.push({ method, path: pathname, body });
    if (this.offline) throw new TypeError("Failed to fetch");
    const intercepted = this.intercept?.(method, pathname, body);
    if (intercepted) return intercepted;
    const forced = this.upcoming.shift();
    if (forced === "network") throw new TypeError("Failed to fetch");
    if (forced) return json(forced.status, forced.body);
    const answer = this.#route(method, pathname.split("/").filter(Boolean), body);
    return json(answer.status, answer.body);
  }

  /** The events the server holds for a run, as { id: data }. */
  stored(runId: string): Record<string, any> {
    return Object.fromEntries([...this.run(runId).events].map(([id, text]) => [id, JSON.parse(text)]));
  }

  /** A run's media item, by the event ID it was started with. */
  media(runId: string, eventId: string): FakeMedia | undefined {
    return [...this.run(runId).media.values()].find((item) => item.event_id === eventId);
  }

  /** A media item's parts, joined in part order, the way `cat` would. */
  joined(item: FakeMedia): string {
    return [...item.parts.keys()]
      .sort((a, b) => a - b)
      .map((n) => item.parts.get(n))
      .join("");
  }

  /** Requests of one kind, like "events" or "start". */
  sent(kind: string): Request[] {
    return this.requests.filter((r) => kind === requestKind(r));
  }

  /** A run the test knows is there. */
  run(runId: string): FakeRun {
    const record = this.runs.get(runId);
    if (!record) throw new Error(`The pretend Pig has no run ${runId}.`);
    return record;
  }

  expire(runId: string): void {
    this.run(runId).status = "expired";
  }

  /** The media requests: start, a part, finish. `rest` is what follows /media. */
  #media(method: string, task: FakeTask, record: FakeRun, rest: string[], body?: string): Answer {
    if (!task.max_part_size) return { status: 404, body: { message: "This task isn't set up to take media." } };
    const closed = () => ({ status: 409, body: { status: record.status, errors: { run: { message: "Closed.", can_retry: true } } } });

    if (rest.length === 0) {
      const { event_id, data } = JSON.parse(body!);
      if (record.status !== "in_progress") return closed();
      const text = JSON.stringify(data);
      const existing = [...record.media.entries()].find(([, item]) => item.event_id === event_id);
      if (existing && record.events.get(event_id) === text) {
        return { status: 200, body: { media_id: existing[0], max_part_size: task.max_part_size } };
      }
      if (record.events.has(event_id)) {
        return { status: 422, body: { status: "in_progress", errors: { [event_id]: { message: "Already have a different one.", can_retry: false } } } };
      }
      const mediaId = record.media.size + 1;
      record.events.set(event_id, text);
      record.media.set(mediaId, { event_id, parts: new Map(), finished: false });
      return { status: 201, body: { media_id: mediaId, max_part_size: task.max_part_size } };
    }

    const item = record.media.get(Number(rest[0]));
    if (!item) return { status: 404, body: { message: "There's no such media item." } };
    const summary = () => ({ status: record.status, media: { stored: [...item.parts.keys()].sort((a, b) => a - b), finished: item.finished } });

    if (rest[1] === "finish") {
      if (record.status !== "in_progress") return closed();
      const { parts } = JSON.parse(body!);
      const held = [...item.parts.keys()];
      if (held.length !== parts || held.some((n) => n > parts)) {
        return { status: 422, body: { ...summary(), errors: { media: { message: "Wrong count.", can_retry: false } } } };
      }
      item.finished = true;
      return { status: 200, body: summary() };
    }

    const part = Number(rest[1]);
    if (record.status !== "in_progress") return closed();
    if (item.finished) return { status: 409, body: { ...summary(), errors: { [part]: { message: "Finished.", can_retry: false } } } };
    if ((body ?? "").length > task.max_part_size) return { status: 413, body: { message: "Too big." } };
    const known = item.parts.get(part);
    if (known !== undefined && known !== body) {
      return { status: 422, body: { ...summary(), errors: { [part]: { message: "A different part.", can_retry: false } } } };
    }
    item.parts.set(part, body ?? "");
    return { status: known === undefined ? 201 : 200, body: summary() };
  }

  #route(method: string, parts: string[], body?: string): Answer {
    const [, code, run, runId, action] = parts; // task, {code}, run, {run_id}, finalize
    const task = this.tasks[code];
    if (!task) return { status: 404, body: { message: `There's no task called '${code}'.` } };

    if (method === "GET" && parts.length === 2) {
      return {
        status: 200,
        body: {
          task_code: code,
          open: task.open,
          parameters: task.parameters,
          expires_after_sec: 86400,
          max_event_size_bytes: task.max_event_size_bytes,
          media: task.max_part_size ? { max_part_size_bytes: task.max_part_size } : null,
        },
      };
    }
    if (method === "POST" && run === "run" && runId === undefined) {
      if (!task.open) return { status: 409, body: { message: "The task isn't accepting new data right now." } };
      const parameters = JSON.parse(body!);
      const missing = task.parameters.filter((p) => !(p in parameters));
      if (missing.length) return { status: 422, body: { message: `Missing ${missing}.` } };
      const id = `run-${this.runs.size + 1}`;
      this.runs.set(id, { status: "in_progress", events: new Map(), parameters, media: new Map() });
      return { status: 201, body: { run_id: id, run_number: this.runs.size } };
    }

    const record = this.runs.get(runId!);
    if (!record) return { status: 404, body: { message: "There's no such run." } };
    const stored = () => [...record.events.keys()];

    if (action === "media") return this.#media(method, task, record, parts.slice(5), body);

    if (action === "finalize") {
      if (record.status === "in_progress") record.status = "finalizing";
      if (record.status === "finalizing") return { status: 200, body: { status: "finalizing", stored: stored() } };
      return { status: 409, body: { status: record.status, stored: stored(), errors: { run: { message: "Expired.", can_retry: false } } } };
    }

    const submitted: Record<string, { data: unknown }> = JSON.parse(body!);
    if (record.status !== "in_progress") {
      const errors = Object.fromEntries(Object.keys(submitted).map((id) => [id, { message: "Closed.", can_retry: true }]));
      return { status: 409, body: { status: record.status, stored: stored(), errors } };
    }
    const errors: Record<string, { message: string; can_retry: boolean }> = {};
    let wrote = false;
    for (const [id, event] of Object.entries(submitted)) {
      const text = JSON.stringify(event.data);
      if (text.length > task.max_event_size_bytes) {
        errors[id] = { message: "Too big.", can_retry: false };
      } else if (record.events.has(id)) {
        if (record.events.get(id) !== text) errors[id] = { message: "Already have a different one.", can_retry: false };
      } else {
        record.events.set(id, text);
        wrote = true;
      }
    }
    if (Object.keys(errors).length) return { status: 422, body: { status: "in_progress", stored: stored(), errors } };
    return { status: wrote ? 201 : 200, body: { status: "in_progress", stored: stored() } };
  }
}

function requestKind({ method, path }: Request): string {
  const parts = path.split("/").filter(Boolean);
  if (method === "GET") return "settings";
  if (method === "PUT") return "part";
  if (parts[4] === "media") return parts.length === 5 ? "media-start" : "media-finish";
  if (parts.length === 3) return "start";
  if (parts.length === 5) return "finalize";
  return "events";
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

/**
 * Web Locks, enough of them: exclusive locks, ifAvailable, and waiting with a signal.
 * Shared between Cores to stand for several pages in one browser.
 */
export class FakeLocks implements Locks {
  holders = new Set<string>();
  waiting = new Map<string, (() => void)[]>();

  request(name: string, options: LockOptions, callback: (lock: Lock | null) => unknown): Promise<unknown> {
    return new Promise((resolve, reject) => {
      const grant = () => {
        this.holders.add(name);
        Promise.resolve(callback({ name, mode: "exclusive" }))
          .then(resolve, reject)
          .finally(() => this.#release(name));
      };
      if (!this.holders.has(name)) return grant();
      if (options.ifAvailable) {
        return Promise.resolve(callback(null)).then(resolve, reject);
      }
      const queue = this.waiting.get(name) ?? [];
      queue.push(grant);
      this.waiting.set(name, queue);
      options.signal?.addEventListener("abort", () => {
        const i = queue.indexOf(grant);
        if (i >= 0) queue.splice(i, 1);
        reject(options.signal?.reason);
      });
    });
  }

  #release(name: string): void {
    this.holders.delete(name);
    const next = this.waiting.get(name)?.shift();
    if (next) next();
  }
}

/** A Core with fast retries, over its own database unless one is given. */
export async function makeCore({
  pig,
  locks = new FakeLocks(),
  factory = new IDBFactory(),
  options = {},
}: {
  pig: FakePig;
  locks?: FakeLocks;
  factory?: IDBFactory;
  options?: Partial<Timing>;
}) {
  const store = await Store.open(factory);
  const notes: Notice[] = [];
  const core = new Core({
    store,
    http: makeHttp({ fetch: pig.fetch, timeoutMs: 1000 }),
    locks,
    notify: (message) => notes.push(message),
    options: { firstRetryMs: 5, maxRetryMs: 20, heartbeatMs: 50, sweepMs: 60_000, abandonedAfterMs: 100, ...options },
  });
  return { core, store, notes, locks, factory };
}

export const STAMP: Stamp = { wall_time: "2026-10-09T12:00:00.000-05:00", performance_now: 1234.5 };
export const PARAMETERS = { participant_id: "10351", session: "baseline" };

/** Start a run on the pretend server with the usual parameters. */
export function startRun(
  core: Core,
  extra: { server?: string; task?: string; parameters?: TaskParameters; finalizeWhenAbandoned?: boolean } = {},
) {
  return core.start({
    server: SERVER,
    task: "stroop",
    parameters: PARAMETERS,
    clientInfo: { user_agent: "test" },
    stamp: STAMP,
    ...extra,
  });
}

/** Wait until `check()` is true, or fail after a while. */
export async function eventually(
  check: () => boolean | Promise<boolean>,
  { timeoutMs = 2000, what = "condition" } = {},
): Promise<void> {
  const until = Date.now() + timeoutMs;
  while (Date.now() < until) {
    if (await check()) return;
    await new Promise((r) => setTimeout(r, 5));
  }
  throw new Error(`Timed out waiting for ${what}.`);
}
