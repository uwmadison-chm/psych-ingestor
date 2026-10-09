// The part of the client a task talks to. It runs on the page; the real work happens in
// a web worker (worker.js), so adding an event costs the page one postMessage.
//
//   const run = await pig.start({ server: "https://pig.yourlab.edu", task: "stroop" });
//   run.add({ type: "trial", word: "GREEN", rt: 843 });    // returns at once
//   await run.add({ type: "block_end" });                  // resolves once it's queued
//   await run.finalize();
//   await run.sent();                                      // everything is on the server
//
// See README.md for the whole story.

import { PigError, wallTime } from "./shared.js";
import { CLIENT_VERSION } from "./version.js";

export { CLIENT_VERSION as version, PigError };

const DEBUG_KEY = "psych-ingestor:debug";
const LOG_LINES = 500;

// Where this file was loaded from, so the worker can be found next to it. Read now,
// while it's true: document.currentScript is only set while a <script> first runs.
const LOADED_FROM =
  (typeof document !== "undefined" && document.currentScript?.src) || import.meta.url;

/**
 * Everything the client tells you about, whichever run it's about: `error`, `progress`,
 * and `run` (a run's server run ID or state changed). Each run also has its own.
 */
export const events = new EventTarget();

let connection;

/**
 * Set the client up. You don't have to call this: start() and the others do it the
 * first time. Call it yourself to change where the worker is loaded from, or to turn on
 * debug logging, before anything else happens.
 *
 * @param {object} [options]
 * @param {string | URL} [options.workerUrl]  where pig-worker.js is. Defaults to next to this file.
 * @param {boolean} [options.debug]           log what the client does to the console
 */
export function connect(options = {}) {
  connection ??= new Connection(options);
  return connection;
}

/**
 * Start a run.
 *
 * @param {object} options
 * @param {string} options.server      Pig's address, like "https://pig.yourlab.edu"
 * @param {string} options.task        your task code
 * @param {Record<string, string>} [options.parameters]  defaults to the page's link parameters
 * @param {boolean} [options.finalizeWhenAbandoned]  for tasks with no natural end; see README
 * @returns {Promise<Run>}
 */
export async function start({ server, task, parameters, finalizeWhenAbandoned = false, ...options }) {
  if (!server || !task) throw new PigError("bad-call", "start() needs a server and a task.");
  const stamp = timeStamp();
  const c = connect(options);
  const summary = await c.call("start", {
    server,
    task,
    parameters: parameters ?? linkParameters(),
    finalizeWhenAbandoned,
    clientInfo: clientInfo(),
    stamp,
  });
  return c.runFor(summary);
}

/**
 * Pick up a run started on an earlier page. Keep `run.id` somewhere that survives the
 * page change (sessionStorage is the usual place) and pass it here.
 * @returns {Promise<Run>}
 */
export async function resume(id, options = {}) {
  const c = connect(options);
  return c.runFor(await c.call("resume", id));
}

/**
 * How much is waiting to be sent: `{ events, bytes, failed, runs }`. With no argument,
 * for everything on this device; or pass `{ task }` or `{ run }` (a run's id).
 */
export function pending(which = {}) {
  return connect().call("pending", which);
}

/**
 * Throw away every event the server refused for good. Those events are kept until you
 * call this, and counted as `failed` in pending().
 */
export function discardFailed() {
  return connect().call("discardFailed");
}

/** The client's recent log lines, newest last, whether or not debug logging is on. */
export function debugLog() {
  return [...(connection?.logLines ?? [])];
}

/** One run. Get one from start() or resume(). */
export class Run extends EventTarget {
  #connection;

  constructor(connection, summary) {
    super();
    this.#connection = connection;
    /** This run's ID on this device. Keep it to resume() the run on another page. */
    this.id = summary.id;
    this.task = summary.task;
    this.update(summary);
  }

  /** @internal */
  update(summary) {
    /** The server's run ID, or null until the server run has started. */
    this.runId = summary.runId;
    /** Which time this is for this participant, or null until the server run has started. */
    this.runNumber = summary.runNumber;
    /** "open", "finalizing", "closed", "failed", or "done" */
    this.state = summary.state;
  }

  /**
   * Queue an event. Returns at once; the promise resolves with the event's ID once it's
   * safely stored on this device, and rejects if it couldn't be. Await it if you want to
   * know; don't if you'd rather not wait. A failure is also reported as an `error`
   * event on the run either way.
   *
   * @param {object} data  anything that can be turned into JSON, except a field called _client
   * @returns {Promise<string>}
   */
  add(data) {
    const stamp = timeStamp();
    if (data !== null && typeof data === "object" && Object.hasOwn(data, "_client")) {
      throw new PigError("bad-event", "_client is filled in by the client. Use another name for your field.");
    }
    const queued = this.#connection.call("add", this.id, data, stamp);
    queued.catch((error) => this.#connection.report({ type: "error", run: this.id, code: error.code, message: error.message }));
    return queued;
  }

  /** Finalize the run, after everything already added. Resolves once that's queued. */
  finalize() {
    return this.#connection.call("finalize", this.id);
  }

  /**
   * Resolves once everything queued for this run has reached the server. Waits as long
   * as that takes, including while offline.
   */
  sent() {
    return this.#connection.call("sent", this.id);
  }

  /** How much of this run is still waiting to be sent: `{ events, bytes, failed }`. */
  pending() {
    return this.#connection.call("pending", { run: this.id });
  }
}

// ------------------------------------------------------------------ plumbing

class Connection {
  constructor({ workerUrl, debug, timing } = {}) {
    this.debug = debug ?? readDebugSetting();
    this.logLines = [];
    this.calls = new Map();
    this.nextCall = 1;
    /** Runs this page has a Run object for: id → Run */
    this.runs = new Map();

    const url = workerUrl ?? new URL("./pig-worker.js", LOADED_FROM);
    this.worker = new Worker(url);
    this.worker.addEventListener("message", ({ data }) => this.#receive(data));
    this.worker.addEventListener("error", (event) => {
      this.#log("error", `The client's worker failed to load or crashed: ${event.message ?? "no details"} (${url})`);
      for (const { reject } of this.calls.values()) {
        reject(new PigError("worker", `The client's worker isn't running. Is pig-worker.js at ${url}?`));
      }
      this.calls.clear();
    });

    // Times when it's worth trying to send right away, rather than waiting out a retry.
    if (typeof window !== "undefined") {
      window.addEventListener("online", () => this.call("nudge"));
      document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "hidden") this.call("nudge");
      });
    }

    // `timing` is for tests: it shortens the client's waits so they finish in seconds.
    this.ready = this.call("init", { core: timing });
    this.#log("info", `Psych Ingestor client ${CLIENT_VERSION}, worker at ${url}.`);
  }

  call(method, ...args) {
    const call = this.nextCall++;
    const done = new Promise((resolve, reject) => this.calls.set(call, { resolve, reject }));
    this.worker.postMessage({ call, method, args });
    return done;
  }

  runFor(summary) {
    let run = this.runs.get(summary.id);
    if (run) run.update(summary);
    else {
      run = new Run(this, summary);
      this.runs.set(summary.id, run);
    }
    return run;
  }

  /** Tell the task about something, on the run it's about and on `events`. */
  report(message) {
    const target = this.runs.get(message.type === "run" ? message.run.id : message.run);
    const detail = { ...message };
    delete detail.type;
    target?.dispatchEvent(new CustomEvent(message.type, { detail }));
    events.dispatchEvent(new CustomEvent(message.type, { detail }));
    if (message.type === "error") this.#log("error", `Run ${message.run}: ${message.message}`);
  }

  #receive(data) {
    if (data.reply !== undefined) {
      const pending = this.calls.get(data.reply);
      if (!pending) return;
      this.calls.delete(data.reply);
      if (data.error) pending.reject(new PigError(data.error.code, data.error.message));
      else pending.resolve(data.value);
    } else if (data.notify) {
      if (data.notify.type === "run") this.runs.get(data.notify.run.id)?.update(data.notify.run);
      this.report(data.notify);
    } else if (data.log) {
      this.#log(data.log.level, data.log.text);
    }
  }

  #log(level, text) {
    const line = `${new Date().toISOString()} [${level}] ${text}`;
    this.logLines.push(line);
    if (this.logLines.length > LOG_LINES) this.logLines.shift();
    if (this.debug || level === "error") {
      const write = level === "error" ? console.error : level === "warn" ? console.warn : console.debug;
      write(`[psych-ingestor] ${text}`);
    }
  }
}

function timeStamp() {
  return { wall_time: wallTime(), performance_now: performance.now() };
}

/** All of the page's link parameters. Pig ignores (and records) ones it doesn't need. */
function linkParameters() {
  if (typeof location === "undefined") return {};
  return Object.fromEntries(new URLSearchParams(location.search));
}

/** What a run's first event says about where it ran. Nothing that identifies anyone. */
function clientInfo() {
  const info = {};
  if (typeof navigator !== "undefined") {
    info.user_agent = navigator.userAgent;
    info.languages = [...(navigator.languages ?? [])];
  }
  try {
    info.timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  } catch {
    // Not every browser says.
  }
  if (typeof screen !== "undefined") {
    info.screen = { width: screen.width, height: screen.height, pixel_ratio: globalThis.devicePixelRatio ?? 1 };
  }
  if (typeof window !== "undefined") {
    info.window = { width: window.innerWidth, height: window.innerHeight };
  }
  return info;
}

function readDebugSetting() {
  try {
    return globalThis.localStorage?.getItem(DEBUG_KEY) === "true";
  } catch {
    return false; // Storage blocked.
  }
}
