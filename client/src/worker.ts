// The web worker. It owns the queue and does all the sending, so none of that work
// happens on the page's main thread, where a task may be timing things to the frame.
//
// It's a thin layer: messages from the page become calls on a Core, and what the Core
// has to say goes back as messages. See core.ts for the actual work.
//
// Messages from the page:  { call, method, args }       call is a number, for the reply
// Messages to the page:    { reply, value } or { reply, error: { code, message } }
//                          { notify: {...} }             progress, errors, run changes
//                          { log: { level, text } }      for the page's console and debugLog()

import { Core, type Locks, type Timing } from "./core.ts";
import { makeHttp } from "./http.ts";
import { Store } from "./store.ts";
import type { Call, FromWorker } from "./types.ts";

/** What the page tells the worker when it starts it. */
export interface WorkerSetup {
  timeoutMs?: number;
  /** Shorter waits, for tests. */
  timing?: Partial<Timing>;
}

// What the page may call. Anything else is refused.
const METHODS = new Set(["start", "resume", "add", "startMedia", "addMedia", "finishMedia", "finalize", "sent", "pending", "discardFailed", "nudge"]);

let core: Core | undefined;
let ready: Promise<void> | undefined;

self.addEventListener("message", async ({ data: { call, method, args } }: MessageEvent<Call>) => {
  try {
    if (method === "init") {
      ready ??= setUp((args[0] ?? {}) as WorkerSetup);
      await ready;
      return send({ reply: call, value: null });
    }
    await ready;
    if (!core || !METHODS.has(method)) throw new Error(`Unknown method ${method}.`);
    const value = await (core as any)[method](...args);
    send({ reply: call, value });
  } catch (error) {
    const { code, message } = error as { code?: string; message?: string };
    send({ reply: call, error: { code: code ?? "internal", message: String(message ?? error) } });
  }
});

// The browser noticing the network is back is a reason to try again right away.
self.addEventListener("online", () => core?.nudge());

async function setUp(options: WorkerSetup): Promise<void> {
  const store = await Store.open();
  core = new Core({
    store,
    http: makeHttp({ timeoutMs: options.timeoutMs }),
    locks: navigator.locks ?? everyoneGetsTheLock,
    notify: (notice) => send({ notify: notice }),
    log: (level, ...parts) => send({ log: { level, text: parts.map(describe).join(" ") } }),
    options: options.timing,
  });
  // Not awaited: finishing other pages' runs shouldn't hold up this page's start().
  core.begin().catch((error) => send({ log: { level: "error", text: `Sweep failed: ${describe(error)}` } }));
}

// For a browser without Web Locks. Two pages might then send the same run's events at
// once, which costs a duplicate request and nothing else: the server stores each event ID
// once. A run whose page is still open keeps saying so (see Core's heartbeat), so it still
// won't be mistaken for an abandoned one.
const everyoneGetsTheLock: Locks = {
  request: async (name, _options, callback) => callback({ name, mode: "exclusive" }),
};

function send(message: FromWorker): void {
  self.postMessage(message);
}

function describe(part: unknown): string {
  if (part instanceof Error) return part.stack ?? part.message;
  return typeof part === "string" ? part : JSON.stringify(part);
}
