// A pretend Pig, pretend Web Locks, and a way to build a Core around them, for tests
// that run in Node. The pretend Pig follows docs/api.md; the browser tests in e2e/ run
// against the real one.

import { IDBFactory } from "fake-indexeddb";
import "fake-indexeddb/auto"; // IDBKeyRange and friends, as globals

import { Core } from "../src/core.js";
import { makeHttp } from "../src/http.js";
import { Store } from "../src/store.js";

export const SERVER = "https://pig.test";

export class FakePig {
  constructor() {
    this.tasks = {
      stroop: { parameters: ["participant_id", "session"], open: true, max_event_size_bytes: 1024 },
    };
    this.runs = new Map();
    this.requests = [];
    this.offline = false;
    /** Answers to give instead of the real one, consumed in order: { status, body } or "network" */
    this.upcoming = [];
    /** A function that may answer a request itself: return a Response, or nothing. */
    this.intercept = null;
    this.fetch = this.fetch.bind(this);
  }

  async fetch(url, init = {}) {
    const { pathname } = new URL(url);
    const method = init.method ?? "GET";
    this.requests.push({ method, path: pathname, body: init.body });
    if (this.offline) throw new TypeError("Failed to fetch");
    const intercepted = this.intercept?.(method, pathname, init.body);
    if (intercepted) return intercepted;
    const forced = this.upcoming.shift();
    if (forced === "network") throw new TypeError("Failed to fetch");
    if (forced) return json(forced.status, forced.body);
    const answer = this.#route(method, pathname.split("/").filter(Boolean), init.body);
    return json(answer.status, answer.body);
  }

  /** The events the server holds for a run, as { id: data }. */
  stored(runId) {
    return Object.fromEntries([...this.runs.get(runId).events].map(([id, text]) => [id, JSON.parse(text)]));
  }

  /** Requests of one kind, like "events" or "start". */
  sent(kind) {
    return this.requests.filter((r) => kind === requestKind(r));
  }

  expire(runId) {
    this.runs.get(runId).status = "expired";
  }

  #route(method, parts, body) {
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
          media: null,
        },
      };
    }
    if (method === "POST" && run === "run" && runId === undefined) {
      if (!task.open) return { status: 409, body: { message: "The task isn't accepting new runs right now." } };
      const parameters = JSON.parse(body);
      const missing = task.parameters.filter((p) => !(p in parameters));
      if (missing.length) return { status: 422, body: { message: `Missing ${missing}.` } };
      const id = `run-${this.runs.size + 1}`;
      this.runs.set(id, { status: "in_progress", events: new Map(), parameters });
      return { status: 201, body: { run_id: id, run_number: this.runs.size } };
    }

    const record = this.runs.get(runId);
    if (!record) return { status: 404, body: { message: "There's no such run." } };
    const stored = () => [...record.events.keys()];

    if (action === "finalize") {
      if (record.status === "in_progress") record.status = "finalizing";
      if (record.status === "finalizing") return { status: 200, body: { status: "finalizing", stored: stored() } };
      return { status: 409, body: { status: record.status, stored: stored(), errors: { run: { message: "Expired.", can_retry: false } } } };
    }

    const submitted = JSON.parse(body);
    if (record.status !== "in_progress") {
      const errors = Object.fromEntries(Object.keys(submitted).map((id) => [id, { message: "Closed.", can_retry: true }]));
      return { status: 409, body: { status: record.status, stored: stored(), errors } };
    }
    const errors = {};
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

function requestKind({ method, path }) {
  const parts = path.split("/").filter(Boolean);
  if (method === "GET") return "settings";
  if (parts.length === 3) return "start";
  if (parts.length === 5) return "finalize";
  return "events";
}

function json(status, body) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

/**
 * Web Locks, enough of them: exclusive locks, ifAvailable, and waiting with a signal.
 * Shared between Cores to stand for several pages in one browser.
 */
export class FakeLocks {
  constructor() {
    this.holders = new Map(); // name → true while held
    this.waiting = new Map(); // name → [grant]
  }

  request(name, options, callback) {
    return new Promise((resolve, reject) => {
      const grant = () => {
        this.holders.set(name, true);
        Promise.resolve(callback({ name }))
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
        reject(options.signal.reason);
      });
    });
  }

  #release(name) {
    this.holders.delete(name);
    const next = this.waiting.get(name)?.shift();
    if (next) next();
  }
}

/** A Core with fast retries, over its own database unless one is given. */
export async function makeCore({ pig, locks = new FakeLocks(), factory = new IDBFactory(), options = {} } = {}) {
  const store = await Store.open(factory);
  const notes = [];
  const core = new Core({
    store,
    http: makeHttp({ fetch: pig.fetch, timeoutMs: 1000 }),
    locks,
    notify: (message) => notes.push(message),
    options: { firstRetryMs: 5, maxRetryMs: 20, heartbeatMs: 50, sweepMs: 60_000, abandonedAfterMs: 100, ...options },
  });
  return { core, store, notes, locks, factory };
}

export const STAMP = { wall_time: "2026-10-09T12:00:00.000-05:00", performance_now: 1234.5 };
export const PARAMETERS = { participant_id: "10351", session: "baseline" };

/** Start a run on the pretend server with the usual parameters. */
export function startRun(core, extra = {}) {
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
export async function eventually(check, { timeoutMs = 2000, what = "condition" } = {}) {
  const until = Date.now() + timeoutMs;
  while (Date.now() < until) {
    if (await check()) return;
    await new Promise((r) => setTimeout(r, 5));
  }
  throw new Error(`Timed out waiting for ${what}.`);
}
