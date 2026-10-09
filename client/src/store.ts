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

import type { FailedOp, NewOp, Op, RunRecord, TaskSettings } from "./types.ts";

const DATABASE = "psych-ingestor";
const VERSION = 1;

type StoreName = "settings" | "runs" | "ops" | "failed";

export class Store {
  db: IDBDatabase;

  constructor(db: IDBDatabase) {
    this.db = db;
  }

  /** Open the database, creating it the first time. */
  static async open(factory: IDBFactory = globalThis.indexedDB): Promise<Store> {
    const request = factory.open(DATABASE, VERSION);
    request.onupgradeneeded = () => {
      const db = request.result;
      db.createObjectStore("settings", { keyPath: ["server", "task"] });
      db.createObjectStore("runs", { keyPath: "id" });
      const ops = db.createObjectStore("ops", { keyPath: "seq", autoIncrement: true });
      ops.createIndex("by_run", ["run", "seq"]);
      db.createObjectStore("failed", { keyPath: "seq" });
    };
    return new Store(await settle<IDBDatabase>(request));
  }

  close() {
    this.db.close();
  }

  // ---------------------------------------------------------------- settings

  async settings(server: string, task: string): Promise<TaskSettings | undefined> {
    const record = await this.#get<{ settings: TaskSettings }>("settings", [server, task]);
    return record?.settings;
  }

  async saveSettings(server: string, task: string, settings: TaskSettings): Promise<void> {
    await this.#put("settings", { server, task, settings, saved_at: Date.now() });
  }

  // -------------------------------------------------------------------- runs

  run(id: string): Promise<RunRecord | undefined> {
    return this.#get<RunRecord>("runs", id);
  }

  allRuns(): Promise<RunRecord[]> {
    return this.#transaction(["runs"], "readonly", (t) =>
      settle<RunRecord[]>(t.objectStore("runs").getAll()),
    );
  }

  saveRun(run: RunRecord): Promise<void> {
    return this.#put("runs", run);
  }

  /**
   * Change a run record in place. `change` gets the current record and returns nothing;
   * the read and the write happen in one transaction, so two changes can't interleave.
   * Resolves with the changed record, or undefined if there's none.
   */
  updateRun(id: string, change: (run: RunRecord) => void): Promise<RunRecord | undefined> {
    return this.#transaction(["runs"], "readwrite", async (t) => {
      const runs = t.objectStore("runs");
      const run = await settle<RunRecord | undefined>(runs.get(id));
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
  deleteRun(id: string): Promise<void> {
    return this.#transaction(["runs", "ops"], "readwrite", async (t) => {
      t.objectStore("runs").delete(id);
      const keys = await settle<IDBValidKey[]>(
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
   * `build` gets the event ID (or undefined) and returns the op to store. Resolves with
   * the stored op, with its `seq`.
   */
  queue(
    runId: string,
    build: (eventId: string | undefined) => NewOp,
    { withEventId = false } = {},
  ): Promise<Op> {
    return this.#transaction(["runs", "ops"], "readwrite", async (t) => {
      const runs = t.objectStore("runs");
      const run = await settle<RunRecord | undefined>(runs.get(runId));
      if (run === undefined) throw new Error(`There's no run ${runId} on this device.`);
      let eventId: string | undefined;
      if (withEventId) {
        eventId = String(run.next_event_id);
        run.next_event_id += 1;
      }
      run.last_active = Date.now();
      runs.put(run);
      const op = { ...build(eventId), run: runId };
      const seq = await settle<number>(t.objectStore("ops").add(op));
      return { ...op, seq };
    });
  }

  /** A run's ops in the order they were queued. */
  ops(runId: string, limit?: number): Promise<Op[]> {
    return this.#transaction(["ops"], "readonly", (t) =>
      settle<Op[]>(t.objectStore("ops").index("by_run").getAll(runRange(runId), limit)),
    );
  }

  /** Every op for every run. */
  allOps(): Promise<Op[]> {
    return this.#transaction(["ops"], "readonly", (t) =>
      settle<Op[]>(t.objectStore("ops").getAll()),
    );
  }

  /** Delete ops by `seq`, in one transaction. */
  deleteOps(seqs: number[]): Promise<void> {
    return this.#transaction(["ops"], "readwrite", async (t) => {
      for (const seq of seqs) t.objectStore("ops").delete(seq);
    });
  }

  /**
   * Move ops the server refused for good out of the queue and into `failed`, with the
   * server's reason. They stay there until someone calls `discardFailed()`.
   */
  failOps(failures: { seq: number; reason: string }[]): Promise<void> {
    return this.#transaction(["ops", "failed"], "readwrite", async (t) => {
      const ops = t.objectStore("ops");
      for (const { seq, reason } of failures) {
        const op = await settle<Op | undefined>(ops.get(seq));
        if (op === undefined) continue;
        ops.delete(seq);
        t.objectStore("failed").put({ ...op, reason, failed_at: Date.now() });
      }
    });
  }

  /** Every op the server refused for good. */
  allFailed(): Promise<FailedOp[]> {
    return this.#transaction(["failed"], "readonly", (t) =>
      settle<FailedOp[]>(t.objectStore("failed").getAll()),
    );
  }

  /** Throw away every failed op. The only way anything in `failed` goes away. */
  discardFailed(): Promise<void> {
    return this.#transaction(["failed"], "readwrite", async (t) => {
      t.objectStore("failed").clear();
    });
  }

  /**
   * Put ops in front of everything else queued for a run. Used when a run's server run
   * has expired: the new server run has to be started before anything else is sent.
   * IndexedDB keys only ever grow, so "in front" means re-adding everything after them.
   */
  prepend(runId: string, newOps: NewOp[]): Promise<void> {
    return this.#transaction(["ops"], "readwrite", async (t) => {
      const store = t.objectStore("ops");
      const existing = await settle<Op[]>(store.index("by_run").getAll(runRange(runId)));
      for (const op of existing) store.delete(op.seq);
      for (const op of [...newOps, ...existing]) {
        const copy: NewOp & { run: string; seq?: number } = { ...op, run: runId };
        delete copy.seq;
        await settle(store.add(copy));
      }
    });
  }

  // ------------------------------------------------------------------ helpers

  #get<T>(storeName: StoreName, key: IDBValidKey): Promise<T | undefined> {
    return this.#transaction([storeName], "readonly", (t) =>
      settle<T | undefined>(t.objectStore(storeName).get(key)),
    );
  }

  #put(storeName: StoreName, value: unknown): Promise<void> {
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
  #transaction<T>(
    storeNames: StoreName[],
    mode: IDBTransactionMode,
    work: (t: IDBTransaction) => Promise<T>,
  ): Promise<T> {
    return new Promise((resolve, reject) => {
      const t = this.db.transaction(storeNames, mode, { durability: "strict" });
      let result: T;
      let failure: unknown;
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

function runRange(runId: string): IDBKeyRange {
  return IDBKeyRange.bound([runId, -Infinity], [runId, Infinity]);
}

function settle<T>(request: IDBRequest): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result as T);
    request.onerror = () => reject(request.error);
  });
}
