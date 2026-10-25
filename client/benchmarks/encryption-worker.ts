// The encryption benchmark's worker: times the client's own encryption functions, in a
// worker, as the client runs them. See encryption.html.
//
// Events go through encryptedData, which is JSON, encryption, and base64, all of which the
// client does for every event. Media parts go through encryptBytes, which is encryption
// alone. The smallest and largest events together say how much of an event's cost is the
// call and how much is the bytes.

import { generateX25519Identity, identityToRecipient } from "age-encryption";

import { encryptBytes, encryptedData } from "../src/encrypt.ts";

const TRIAL = {
  trial: 12,
  rt: 523,
  stimulus: "red",
  response: "blue",
  correct: false,
  _client: { t: "2026-10-24T22:14:40.123Z" },
};

/** About 700 KB of JSON: near the most that fits in a task's default 1 MiB event limit,
 * once it's encrypted and base64 makes it a third bigger. */
function largeEvent(): unknown {
  const trials = [];
  while (JSON.stringify(trials).length < 700_000) trials.push({ ...TRIAL, trial: trials.length });
  return { trials };
}

function bytes(size: number): Uint8Array {
  return new Uint8Array(size).map((_, n) => n % 251);
}

async function time(run: () => Promise<unknown>, count: number): Promise<number> {
  // Warm up first, so the first test doesn't also pay for the browser compiling the code.
  for (let i = 0; i < Math.max(1, count / 10); i++) await run();
  const start = performance.now();
  for (let i = 0; i < count; i++) await run();
  return (performance.now() - start) / count;
}

self.onmessage = async () => {
  try {
    const publicKey = await identityToRecipient(await generateX25519Identity());
    const large = largeEvent();
    const one = bytes(1024 * 1024);
    const eight = bytes(8 * 1024 * 1024);
    const tests: { id: string; size: number; count: number; run: () => Promise<unknown> }[] = [
      { id: "smallest", size: JSON.stringify({}).length, count: 500, run: () => encryptedData(publicKey, {}) },
      { id: "typical", size: JSON.stringify(TRIAL).length, count: 500, run: () => encryptedData(publicKey, TRIAL) },
      { id: "large", size: JSON.stringify(large).length, count: 5, run: () => encryptedData(publicKey, large) },
      { id: "part1", size: one.length, count: 5, run: () => encryptBytes(publicKey, one) },
      { id: "part8", size: eight.length, count: 2, run: () => encryptBytes(publicKey, eight) },
    ];
    for (const test of tests) {
      self.postMessage({ started: test.id });
      const ms = await time(test.run, test.count);
      self.postMessage({ finished: test.id, size: test.size, ms });
    }
    self.postMessage({ done: true });
  } catch (error) {
    self.postMessage({ error: String(error) });
  }
};
