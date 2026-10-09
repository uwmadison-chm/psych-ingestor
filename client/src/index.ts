// The part of the client a task talks to. It runs on the page; the real work happens in
// a web worker (worker.ts), so adding an event costs the page one postMessage.
//
//   const run = await pig.start("https://pig.yourlab.edu", "stroop", { participant_id: "10351" });
//   run.add({ type: "trial", word: "GREEN", rt: 843 });    // returns at once
//   await run.add({ type: "block_end" });                  // resolves once it's queued
//   await run.finalize();
//   await run.sent();                                      // everything is on the server
//
// See README.md for the whole story.

import type { Timing } from "./core.ts";
import { PigError, wallTime } from "./shared.ts";
import type { ClientInfo, FromWorker, LogLevel, Notice, Pending, RunState, RunSummary, Stamp, TaskParameters } from "./types.ts";
import { CLIENT_VERSION } from "./version.ts";
import type { WorkerSetup } from "./worker.ts";

export { CLIENT_VERSION as version, PigError };
export type { Pending, RunState, TaskParameters };

const LOG_LINES = 500;

// Where this file was loaded from, so the worker can be found next to it. Read now,
// while it's true: document.currentScript is only set while a <script> first runs.
const LOADED_FROM =
  (typeof document !== "undefined" && (document.currentScript as HTMLScriptElement | null)?.src) || import.meta.url;

/**
 * Everything the client tells you about, whichever run it's about: `error`, `progress`,
 * and `run` (a run's server run ID or state changed). Each run also has its own.
 */
export const events = new EventTarget();

export interface ConnectOptions {
  /** Where pig-worker.js is. Defaults to next to this file. */
  workerUrl?: string | URL;
  /** Shorter waits than usual. For the client's own tests. */
  timing?: Partial<Timing>;
}

export interface StartOptions {
  /** For tasks with no natural end. See README.md. */
  finalizeWhenAbandoned?: boolean;
}

/** Anything with a query string: a URL, a string, or `window.location`. */
export type URLike = URL | Location | string;

let connection: Connection | undefined;

/**
 * Set the client up. You don't have to call this: start() and the others do it the
 * first time. Call it yourself, before anything else, to load the worker from somewhere
 * other than next to this file.
 */
export function connect(options: ConnectOptions = {}): void {
  connection ??= new Connection(options);
}

/**
 * Whether this browser can run the client. Logs what's missing, if anything. Worth
 * checking before the participant starts, so you can tell them to use another browser
 * rather than lose their data.
 */
export async function supported(): Promise<boolean> {
  const missing = [];
  if (typeof indexedDB === "undefined") missing.push("IndexedDB");
  if (typeof Worker === "undefined") missing.push("web workers");
  if (typeof crypto === "undefined" || typeof crypto.randomUUID !== "function") {
    missing.push("crypto.randomUUID (the page has to be served over https)");
  }
  if (!missing.includes("IndexedDB") && !(await canOpenIndexedDB())) {
    missing.push("IndexedDB storage (blocked, or a private window in an older browser)");
  }
  for (const what of missing) console.warn(`[psych-ingestor] This browser can't run the client: no ${what}.`);
  return missing.length === 0;
}

/**
 * Start a run of `task` for the participant `parameters` describe.
 *
 * `server` is Pig's address, like "https://pig.yourlab.edu". `parameters` is every name
 * and value the task should start the run with, like { participant_id: "10351" }; Pig
 * records any it doesn't need. To take them from the page's address, use startForURL().
 */
export async function start(
  server: string,
  task: string,
  parameters: TaskParameters,
  options: StartOptions = {},
): Promise<Run> {
  const stamp = timeStamp();
  if (typeof server !== "string" || !server || typeof task !== "string" || !task) {
    throw new PigError("bad-call", "start() needs Pig's address and a task code, like start(\"https://pig.yourlab.edu\", \"stroop\", parameters).");
  }
  checkParameterTypes(parameters);
  const c = connected();
  const summary = (await c.call("start", {
    server,
    task,
    parameters: { ...parameters },
    finalizeWhenAbandoned: options.finalizeWhenAbandoned ?? false,
    clientInfo: clientInfo(),
    stamp,
  })) as RunSummary;
  return c.runFor(summary);
}

/**
 * Start a run with the parameters in a URL's query string: every one of them, as it
 * is. Usually `startForURL(server, task, window.location)`.
 */
export function startForURL(server: string, task: string, url: URLike, options: StartOptions = {}): Promise<Run> {
  let search: string;
  try {
    search = typeof url === "string" ? new URL(url, globalThis.location?.href).search : url.search;
  } catch {
    return Promise.reject(new PigError("bad-call", `startForURL() couldn't read ${JSON.stringify(url)} as a URL.`));
  }
  return start(server, task, Object.fromEntries(new URLSearchParams(search)), options);
}

/**
 * Pick up a run started on an earlier page. Keep `run.id` somewhere that survives the
 * page change (sessionStorage is the usual place) and pass it here.
 */
export async function resume(id: string): Promise<Run> {
  const c = connected();
  return c.runFor((await c.call("resume", id)) as RunSummary);
}

/**
 * How much is waiting to be sent. With no argument, for everything on this device; or
 * pass `{ task }` or `{ run }` (a run's id).
 */
export function pending(which: { task?: string; run?: string } = {}): Promise<Pending> {
  return connected().call("pending", which) as Promise<Pending>;
}

/**
 * Throw away every event the server refused for good. Those events are kept until you
 * call this, and counted as `failed` in pending().
 */
export async function discardFailed(): Promise<void> {
  await connected().call("discardFailed");
}

/** The client's recent log lines, newest last. */
export function debugLog(): string[] {
  return [...(connection?.logLines ?? [])];
}

/** One run. Get one from start(), startForURL(), or resume(). */
export class Run extends EventTarget {
  #connection: Connection;
  /** This run's ID on this device. Keep it to resume() the run on another page. */
  readonly id: string;
  readonly task: string;
  /** The server's run ID, or null until the server run has started. */
  runId: string | null = null;
  /** Which time this is for this participant, or null until the server run has started. */
  runNumber: number | null = null;
  state: RunState = "open";

  constructor(connection: Connection, summary: RunSummary) {
    super();
    this.#connection = connection;
    this.id = summary.id;
    this.task = summary.task;
    this.update(summary);
  }

  /** @internal */
  update(summary: RunSummary): void {
    this.runId = summary.runId;
    this.runNumber = summary.runNumber;
    this.state = summary.state;
  }

  /**
   * Queue an event. Returns at once; the promise resolves with the event's ID once it's
   * safely stored on this device, and rejects if it couldn't be. Await it if you want to
   * know; don't if you'd rather not wait. A failure is also reported as an `error`
   * event on the run either way.
   *
   * `data` is anything that can be turned into JSON, except a field called _client.
   */
  add(data: Record<string, unknown>): Promise<string> {
    const stamp = timeStamp();
    if (data !== null && typeof data === "object" && Object.hasOwn(data, "_client")) {
      throw new PigError("bad-event", "_client is filled in by the client. Use another name for your field.");
    }
    const queued = this.#connection.call("add", this.id, data, stamp) as Promise<string>;
    queued.catch((error: PigError) => this.#connection.report(errorNotice(this.id, error)));
    return queued;
  }

  /**
   * Start a media item: a recording, an image, or any other file. It's an event like
   * any other, with `data` stored the same way, and bytes attached with the item's
   * add(). Record the content type in `data`; nothing else knows how to play it.
   * Resolves once the event is stored on this device. Refused if the task isn't set
   * up to take media.
   */
  async startMedia(data: Record<string, unknown>): Promise<Media> {
    const stamp = timeStamp();
    const eventId = (await this.#connection.call("startMedia", this.id, data, stamp)) as string;
    return new Media(this.#connection, this.id, eventId);
  }

  /**
   * Record from a MediaRecorder into a new media item. Starts the recorder itself, with
   * `timeslice` (milliseconds between blobs), and stamps the item with the moment the
   * recorder says it started, on the same clock as every event's `_client`. Sends each
   * blob as it comes, and finishes the item when the recorder stops; stopping it is up
   * to you. `data` is stored as the item's event, with `content_type` filled in from
   * the recorder unless you give one. Resolves once the item's event is stored.
   */
  async record(recorder: MediaRecorder, data: Record<string, unknown> = {}, { timeslice = 5000 } = {}): Promise<Media> {
    if (recorder.state !== "inactive") {
      throw new PigError("bad-call", "record() starts the recorder itself, so give it one that isn't recording yet.");
    }
    // Blobs can arrive before the item exists. They wait here, in order.
    const waiting: Blob[] = [];
    let item: Media | undefined;
    let stopped = false;
    const onData = (event: BlobEvent) => {
      if (item) item.add(event.data);
      else waiting.push(event.data);
    };
    const onStop = () => {
      // The browser always delivers the last blob before `stop`.
      recorder.removeEventListener("dataavailable", onData);
      stopped = true;
      item?.finish().catch((error: PigError) => this.#connection.report(errorNotice(this.id, error)));
    };
    recorder.addEventListener("dataavailable", onData);
    recorder.addEventListener("stop", onStop, { once: true });

    let started: Event;
    try {
      started = await new Promise<Event>((resolve, reject) => {
        recorder.addEventListener("start", resolve, { once: true });
        recorder.addEventListener("error", (event) => reject((event as ErrorEvent).error ?? event), { once: true });
        recorder.start(timeslice);
      });
    } catch (error) {
      recorder.removeEventListener("dataavailable", onData);
      recorder.removeEventListener("stop", onStop);
      throw new PigError("recorder", `The recorder didn't start: ${(error as Error)?.message ?? "no details"}`);
    }
    // An event's timeStamp is on the performance.now() clock.
    const stamp: Stamp = { wall_time: wallTime(), performance_now: started.timeStamp };

    let eventId: string;
    try {
      eventId = (await this.#connection.call(
        "startMedia",
        this.id,
        { content_type: recorder.mimeType, ...data },
        stamp,
      )) as string;
    } catch (error) {
      recorder.removeEventListener("dataavailable", onData);
      recorder.removeEventListener("stop", onStop);
      if (recorder.state !== "inactive") recorder.stop();
      throw error;
    }
    item = new Media(this.#connection, this.id, eventId);
    for (const blob of waiting.splice(0)) item.add(blob);
    if (stopped) item.finish().catch((error: PigError) => this.#connection.report(errorNotice(this.id, error)));
    return item;
  }

  /** Finalize the run, after everything already added. Resolves once that's queued. */
  async finalize(): Promise<void> {
    await this.#connection.call("finalize", this.id);
  }

  /**
   * Resolves once everything queued for this run has reached the server. Waits as long
   * as that takes, including while offline.
   */
  async sent(): Promise<void> {
    await this.#connection.call("sent", this.id);
  }

  /** How much of this run is still waiting to be sent. */
  pending(): Promise<Pending> {
    return this.#connection.call("pending", { run: this.id }) as Promise<Pending>;
  }
}

/** One media item. Get one from run.startMedia() or run.record(). */
export class Media {
  #connection: Connection;
  #run: string;
  /** The item's event ID, the one its start was stored under. */
  readonly eventId: string;

  constructor(connection: Connection, run: string, eventId: string) {
    this.#connection = connection;
    this.#run = run;
    this.eventId = eventId;
  }

  /**
   * Queue some of the item's bytes. Returns at once, like run.add(); the promise
   * resolves once they're stored on this device, with the part numbers they were
   * given. Parts are numbered in the order you call this, so the stored parts join
   * back together in that order. A blob too big for one part becomes several.
   */
  add(blob: Blob): Promise<number[]> {
    const queued = this.#connection.call("addMedia", this.#run, this.eventId, blob) as Promise<number[]>;
    queued.catch((error: PigError) => this.#connection.report(errorNotice(this.#run, error)));
    return queued;
  }

  /** Say the item is complete. Resolves once that's queued, after everything added. */
  async finish(): Promise<void> {
    await this.#connection.call("finishMedia", this.#run, this.eventId);
  }
}

// ------------------------------------------------------------------ plumbing

/** The page's end of the worker. */
class Connection {
  logLines: string[] = [];
  calls = new Map<number, { resolve: (value: unknown) => void; reject: (error: PigError) => void }>();
  nextCall = 1;
  /** Runs this page has a Run object for: id → Run */
  runs = new Map<string, Run>();
  worker: Worker;
  ready: Promise<unknown>;

  constructor({ workerUrl, timing }: ConnectOptions) {
    const url = workerUrl ?? new URL("./pig-worker.js", LOADED_FROM);
    this.worker = new Worker(url);
    this.worker.addEventListener("message", ({ data }: MessageEvent<FromWorker>) => this.#receive(data));
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

    const setup: WorkerSetup = { timing };
    this.ready = this.call("init", setup);
    this.#log("info", `Psych Ingestor client ${CLIENT_VERSION}, worker at ${url}.`);
  }

  call(method: string, ...args: unknown[]): Promise<unknown> {
    const call = this.nextCall++;
    const done = new Promise((resolve, reject) => this.calls.set(call, { resolve, reject }));
    this.worker.postMessage({ call, method, args });
    return done;
  }

  runFor(summary: RunSummary): Run {
    let run = this.runs.get(summary.id);
    if (run) run.update(summary);
    else {
      run = new Run(this, summary);
      this.runs.set(summary.id, run);
    }
    return run;
  }

  /** Tell the task about something, on the run it's about and on `events`. */
  report(notice: Notice): void {
    const target = this.runs.get(notice.type === "run" ? notice.run.id : notice.run);
    const { type, ...detail } = notice;
    target?.dispatchEvent(new CustomEvent(type, { detail }));
    events.dispatchEvent(new CustomEvent(type, { detail }));
    if (notice.type === "error") this.#log("error", `Run ${notice.run}: ${notice.message}`);
  }

  #receive(data: FromWorker): void {
    if ("reply" in data) {
      const pending = this.calls.get(data.reply);
      if (!pending) return;
      this.calls.delete(data.reply);
      if ("error" in data) pending.reject(new PigError(data.error.code, data.error.message));
      else pending.resolve(data.value);
    } else if ("notify" in data) {
      if (data.notify.type === "run") this.runs.get(data.notify.run.id)?.update(data.notify.run);
      this.report(data.notify);
    } else {
      this.#log(data.log.level, data.log.text);
    }
  }

  /** Everything goes to the console, and to debugLog() for later. */
  #log(level: LogLevel, text: string): void {
    this.logLines.push(`${new Date().toISOString()} [${level}] ${text}`);
    if (this.logLines.length > LOG_LINES) this.logLines.shift();
    const write = level === "error" ? console.error : level === "warn" ? console.warn : console.debug;
    write(`[psych-ingestor] ${text}`);
  }
}

function connected(): Connection {
  connection ??= new Connection({});
  return connection;
}

/** A failed call, as an `error` event for the run it was about. */
function errorNotice(run: string, error: PigError): Notice {
  return { type: "error", run, code: error.code, message: error.message };
}

function timeStamp(): Stamp {
  return { wall_time: wallTime(), performance_now: performance.now() };
}

/** Catches the easy mistakes on the page, so the error points at the task's own code. */
function checkParameterTypes(parameters: unknown): asserts parameters is TaskParameters {
  if (parameters === null || typeof parameters !== "object" || Array.isArray(parameters)) {
    throw new PigError(
      "bad-call",
      "start() needs the run's parameters as an object, like { participant_id: \"10351\" }. To take them from the page's address, use startForURL().",
    );
  }
  for (const [name, value] of Object.entries(parameters)) {
    if (typeof value !== "string") {
      throw new PigError("bad-call", `Parameter values have to be strings, and ${name} is ${JSON.stringify(value)}.`);
    }
  }
}

/** Whether IndexedDB will actually open. A private window can have it, and refuse. */
function canOpenIndexedDB(): Promise<boolean> {
  const name = "psych-ingestor:probe";
  return new Promise((resolve) => {
    try {
      const request = indexedDB.open(name);
      request.onsuccess = () => {
        request.result.close();
        indexedDB.deleteDatabase(name);
        resolve(true);
      };
      request.onerror = () => resolve(false);
    } catch {
      resolve(false);
    }
  });
}

/** What a run's first event says about where it ran. Nothing that identifies anyone. */
function clientInfo(): ClientInfo {
  const info: ClientInfo = {};
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
