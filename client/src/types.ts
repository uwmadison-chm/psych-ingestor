// The shapes of things the page, the worker, and IndexedDB pass between them.

/** What a run is started with: names to values, all strings. */
export type TaskParameters = Record<string, string>;

/** Two times, taken on the page when something happened. */
export interface Stamp {
  /** The participant's clock, with its UTC offset. */
  wall_time: string;
  /** `performance.now()`, or null when the time wasn't taken on a page. */
  performance_now: number | null;
}

/** A task's settings, as GET /task/{task_code} returns them. */
export interface TaskSettings {
  task_code: string;
  open: boolean;
  parameters: string[];
  expires_after_sec: number;
  max_event_size_bytes: number;
  media: { max_part_size_bytes: number } | null;
}

/** What the page knows about itself, for a run's first event. */
export interface ClientInfo {
  user_agent?: string;
  languages?: string[];
  timezone?: string;
  screen?: { width: number; height: number; pixel_ratio: number };
  window?: { width: number; height: number };
}

/** Why a run can't go on. */
export interface Failure {
  code: string;
  message: string;
}

/** One run, as IndexedDB keeps it. */
export interface RunRecord {
  id: string;
  server: string;
  task: string;
  parameters: TaskParameters;
  settings: TaskSettings;
  client_info: ClientInfo;
  server_run_id: string | null;
  run_number: number | null;
  server_started_at: number | null;
  next_event_id: number;
  finalize_when_abandoned: boolean;
  finalize_queued: boolean;
  closed: boolean;
  failed: Failure | null;
  created_at: number;
  last_active: number;
  /** The run's media items, by their event ID. */
  media: Record<string, MediaRecord>;
}

/** One media item, as a run's record keeps it. */
export interface MediaRecord {
  event_id: string;
  /** The server's media ID, or null until the server has started the item. */
  server_media_id: number | null;
  next_part: number;
  finish_queued: boolean;
  /** The server refused part of it for good; what's left of it is in `failed`. */
  failed: boolean;
  /** The start, as JSON, for starting it again in a new server run. */
  start_json: string;
  /**
   * Its parts went to more than one server run, because one expired partway through.
   * Then neither server run holds all of it, and it isn't finished in either.
   */
  split: boolean;
}

/** Something that has to reach the server, before IndexedDB has numbered it. */
export type NewOp =
  | { kind: "start" }
  | { kind: "finalize" }
  | { kind: "event"; event_id: string; json: string; bytes: number }
  | { kind: "media-start"; event_id: string; json: string; bytes: number }
  | { kind: "media-part"; event_id: string; part: number; blob: Blob; bytes: number }
  | { kind: "media-finish"; event_id: string; parts: number };

/** A queued op: which run it's for, and its place in the queue. */
export type Op = NewOp & { run: string; seq: number };

export type EventOp = Extract<Op, { kind: "event" }>;
export type MediaStartOp = Extract<Op, { kind: "media-start" }>;
export type PartOp = Extract<Op, { kind: "media-part" }>;
export type MediaFinishOp = Extract<Op, { kind: "media-finish" }>;

/** An op the server refused for good, and why. */
export type FailedOp = Op & { reason: string; failed_at: number };

/** How much is waiting to be sent. */
export interface Pending {
  events: number;
  bytes: number;
  /** Events and media parts the server refused for good. Kept, never sent. See discardFailed(). */
  failed: number;
  /** Runs with anything left to send. */
  runs: number;
}

export type RunState = "open" | "finalizing" | "closed" | "failed" | "done";

/** What the page gets to know about a run. */
export interface RunSummary {
  id: string;
  task: string;
  runId: string | null;
  runNumber: number | null;
  state: RunState;
  failed: Failure | null;
}

/** What the worker tells the page about, unasked. */
export type Notice =
  | { type: "run"; run: RunSummary }
  | { type: "progress"; run: string; pending: Pending }
  | { type: "error"; run: string; code: string; message: string; events?: string[] };

export type LogLevel = "debug" | "info" | "warn" | "error";

/** A message from the page to the worker. `call` numbers it, for the reply. */
export interface Call {
  call: number;
  method: string;
  args: unknown[];
}

/** A message from the worker to the page. */
export type FromWorker =
  | { reply: number; value: unknown }
  | { reply: number; error: Failure }
  | { notify: Notice }
  | { log: { level: LogLevel; text: string } };
