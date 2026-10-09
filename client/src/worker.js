// The web worker. It owns the queue and does all the sending, so none of that work
// happens on the page's main thread, where a task may be timing things to the frame.
//
// It's a thin layer: messages from the page become calls on a Core, and what the Core
// has to say goes back as messages. See core.js for the actual work.
//
// Messages from the page:  { call, method, args }       call is a number, for the reply
// Messages to the page:    { reply, value } or { reply, error: { code, message } }
//                          { notify: {...} }             progress, errors, run changes
//                          { log: { level, text } }      for the page's debug log

import { Core } from "./core.js";
import { makeHttp } from "./http.js";
import { Store } from "./store.js";

// What the page may call. Anything else is refused.
const METHODS = new Set(["start", "resume", "add", "finalize", "sent", "pending", "discardFailed", "nudge"]);

let core;
let ready;

self.addEventListener("message", async ({ data: { call, method, args } }) => {
  try {
    if (method === "init") {
      ready ??= setUp(args[0] ?? {});
      await ready;
      return respond(call, { value: null });
    }
    await ready;
    if (!METHODS.has(method)) throw new Error(`Unknown method ${method}.`);
    const value = await core[method](...args);
    respond(call, { value });
  } catch (error) {
    respond(call, { error: { code: error.code ?? "internal", message: String(error.message ?? error) } });
  }
});

// The browser noticing the network is back is a reason to try again right away.
self.addEventListener("online", () => core?.nudge());

async function setUp(options) {
  const store = await Store.open();
  core = new Core({
    store,
    http: makeHttp({ timeoutMs: options.timeoutMs }),
    locks: navigator.locks ?? everyoneGetsTheLock,
    notify: (message) => self.postMessage({ notify: message }),
    log: (level, ...parts) => self.postMessage({ log: { level, text: parts.map(describe).join(" ") } }),
    options: options.core,
  });
  // Not awaited: finishing other pages' runs shouldn't hold up this page's start().
  core.begin().catch((error) => self.postMessage({ log: { level: "error", text: `Sweep failed: ${describe(error)}` } }));
}

// For a browser without Web Locks. Two pages might then send the same run's events at
// once, which costs a duplicate request and nothing else: the server stores each event ID
// once. A run whose page is still open keeps saying so (see Core's heartbeat), so it still
// won't be mistaken for an abandoned one.
const everyoneGetsTheLock = {
  request: async (_name, _options, callback) => callback({}),
};

function respond(call, message) {
  if (call !== undefined) self.postMessage({ reply: call, ...message });
}

function describe(part) {
  if (part instanceof Error) return part.stack ?? part.message;
  return typeof part === "string" ? part : JSON.stringify(part);
}
