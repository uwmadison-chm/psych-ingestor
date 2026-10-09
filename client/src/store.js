// The queue, kept in IndexedDB so it survives a closed tab, a reload, or a dead battery.
//
// Three object stores:
//
//   settings  One task's settings, as GET /task/{code} last returned them, keyed by
//             server and task code. Used when the device is offline.
//   runs      One record per run this browser started. The record outlives any one
//             page: a run can be resumed on the next page, or finished by a later visit.
//   ops       What still has to reach the server, in order: start the run, events,
//             finalize. Each op is deleted only once the server has confirmed it.
//   failed    Ops the server refused for good (`can_retry: false`). Moved out of `ops`
//             so they don't block what's behind them, and kept, because dropping a
//             participant's data silently is the one thing the client must not do.
//             Only `discardFailed()` removes them.
//
// Ops are numbered by IndexedDB itself (`seq`), so their order is the order they were
// added. The sender reads a run's ops in that order and never skips ahead.
//
// IndexedDB's own API is callbacks and events. Everything here wraps it in promises and
// does nothing else, so the rest of the client can read as ordinary async code.

const DATABASE = "psych-ingestor";
const VERSION = 1;

export class Store {
  /** @param {IDBDatabase} db */
  constructor(db) {
    this.db = db;
  }

  /**
   * Open the database, creating it the first time.
   * @param {IDBFactory} [factory]
   * @returns {Promise<Store>}
   */
  static async open(factory = globalThis.indexedDB) {
    const request = factory.open(DATABASE, VERSION);
    request.onupgradeneeded = () => {
      const db = request.result;
      db.createObjectStore("settings", { keyPath: ["server", "task"] });
      db.createObjectStore("runs", { keyPath: "id" });
      const ops = db.createObjectStore("ops", { keyPath: "seq", autoIncrement: true });
      ops.createIndex("by_run", ["run", "seq"]);
      db.createObjectStore("failed", { keyPath: "seq" });
    };
    return new Store(await settle(request));
  }

  close() {
    this.db.close();
  }

  // ---------------------------------------------------------------- settings

  /** @returns {Promise<object | undefined>} */
  async settings(server, task) {
    const record = await this.#get("settings", [server, task]);
    return record?.settings;
  }

  async saveSettings(server, task, settings) {
    await this.#put("settings", { server, task, settings, saved_at: Date.now() });
  }

  // -------------------------------------------------------------------- runs

  /** @returns {Promise<object | undefined>} */
  run(id) {
    return this.#get("runs", id);
  }

  /** @returns {Promise<object[]>} */
  allRuns() {
    return this.#transaction(["runs"], "readonly", (t) =>
      settle(t.objectStore("runs").getAll()),
    );
  }

  saveRun(run) {
    return this.#put("runs", run);
  }

  /**
   * Change a run record in place. `change` gets the current record and returns nothing;
   * the read and the write happen in one transaction, so two changes can't interleave.
   * @returns {Promise<object | undefined>} the changed record, or undefined if there's none
   */
  updateRun(id, change) {
    return this.#transaction(["runs"], "readwrite", async (t) => {
      const runs = t.objectStore("runs");
      const run = await settle(runs.get(id));
      if (run === undefined) return undefined;
      change(run);
      runs.put(run);
      return run;
    });
  }

  /**
   * Remove a run and everything still queued for it. Failed ops are kept: they're
   * only ever removed by `discardFailed()`.
   */
  deleteRun(id) {
    return this.#transaction(["runs", "ops"], "readwrite", async (t) => {
      t.objectStore("runs").delete(id);
      const keys = await settle(
        t.objectStore("ops").index("by_run").getAllKeys(runRange(id)),
      );
      for (const key of keys) t.objectStore("ops").delete(key);
    });
  }

  // --------------------------------------------------------------------- ops

  /**
   * Queue an op for a run. If `withEventId` is true, the run's event counter is read,
   * given to the op as its `event_id`, and advanced, all in the same transaction as the
   * op is written. So an event ID is never handed out twice, and never handed out for
   * an event that wasn't stored.
   *
   * `build` gets the event ID (or undefined) and returns the op to store.
   * @returns {Promise<object>} the stored op, with its `seq`
   */
  queue(runId, build, { withEventId = false } = {}) {
    return this.#transaction(["runs", "ops"], "readwrite", async (t) => {
      const runs = t.objectStore("runs");
      const run = await settle(runs.get(runId));
      if (run === undefined) throw new Error(`There's no run ${runId} on this device.`);
      let eventId;
      if (withEventId) {
        eventId = String(run.next_event_id);
        run.next_event_id += 1;
      }
      run.last_active = Date.now();
      runs.put(run);
      const op = { ...build(eventId), run: runId };
      op.seq = await settle(t.objectStore("ops").add(op));
      return op;
    });
  }

  /**
   * A run's ops in the order they were queued.
   * @returns {Promise<object[]>}
   */
  ops(runId, limit) {
    return this.#transaction(["ops"], "readonly", (t) =>
      settle(t.objectStore("ops").index("by_run").getAll(runRange(runId), limit)),
    );
  }

  /** @returns {Promise<object[]>} every op for every run */
  allOps() {
    return this.#transaction(["ops"], "readonly", (t) =>
      settle(t.objectStore("ops").getAll()),
    );
  }

  /** Delete ops by `seq`, in one transaction. */
  deleteOps(seqs) {
    return this.#transaction(["ops"], "readwrite", async (t) => {
      for (const seq of seqs) t.objectStore("ops").delete(seq);
    });
  }

  /**
   * Move ops the server refused for good out of the queue and into `failed`, with the
   * server's reason. They stay there until someone calls `discardFailed()`.
   */
  failOps(failures) {
    return this.#transaction(["ops", "failed"], "readwrite", async (t) => {
      const ops = t.objectStore("ops");
      for (const { seq, reason } of failures) {
        const op = await settle(ops.get(seq));
        if (op === undefined) continue;
        ops.delete(seq);
        t.objectStore("failed").put({ ...op, reason, failed_at: Date.now() });
      }
    });
  }

  /** @returns {Promise<object[]>} every op the server refused for good */
  allFailed() {
    return this.#transaction(["failed"], "readonly", (t) =>
      settle(t.objectStore("failed").getAll()),
    );
  }

  /** Throw away every failed op. The only way anything in `failed` goes away. */
  discardFailed() {
    return this.#transaction(["failed"], "readwrite", async (t) => {
      t.objectStore("failed").clear();
    });
  }

  /**
   * Put ops in front of everything else queued for a run. Used when a run's server run
   * has expired: the new server run has to be started before anything else is sent.
   * IndexedDB keys only ever grow, so "in front" means re-adding everything after them.
   */
  prepend(runId, newOps) {
    return this.#transaction(["ops"], "readwrite", async (t) => {
      const store = t.objectStore("ops");
      const existing = await settle(store.index("by_run").getAll(runRange(runId)));
      for (const op of existing) store.delete(op.seq);
      for (const op of [...newOps, ...existing]) {
        const copy = { ...op, run: runId };
        delete copy.seq;
        await settle(store.add(copy));
      }
    });
  }

  // ------------------------------------------------------------------ helpers

  #get(storeName, key) {
    return this.#transaction([storeName], "readonly", (t) =>
      settle(t.objectStore(storeName).get(key)),
    );
  }

  #put(storeName, value) {
    return this.#transaction([storeName], "readwrite", async (t) => {
      t.objectStore(storeName).put(value);
    });
  }

  /**
   * Run `work` in a transaction, and resolve with what it returned once the transaction
   * has committed. Resolving only after commit is what makes "it's in the queue" true:
   * a write isn't durable until then.
   *
   * `work` may await requests made on this transaction, but nothing else. Waiting on
   * anything outside IndexedDB lets the transaction commit early.
   */
  #transaction(storeNames, mode, work) {
    return new Promise((resolve, reject) => {
      const t = this.db.transaction(storeNames, mode, { durability: "strict" });
      let result;
      let failure;
      t.oncomplete = () => (failure ? reject(failure) : resolve(result));
      t.onabort = () => reject(failure ?? t.error ?? new Error("Transaction aborted."));
      t.onerror = () => {}; // onabort follows and reports it
      work(t).then(
        (value) => {
          result = value;
        },
        (error) => {
          failure = error;
          try {
            t.abort();
          } catch {
            // Already finished; oncomplete or onabort will report.
          }
        },
      );
    });
  }
}

function runRange(runId) {
  return IDBKeyRange.bound([runId, -Infinity], [runId, Infinity]);
}

/** @param {IDBRequest | IDBOpenDBRequest} request */
function settle(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}
