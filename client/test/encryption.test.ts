// End-to-end encryption, run in Node against a pretend Pig whose task gives a public key.

import assert from "node:assert/strict";
import { afterEach, describe, test } from "node:test";

import { Decrypter, Encrypter, generateX25519Identity, identityToRecipient } from "age-encryption";

import { encryptedSize, plaintextThatFits } from "../src/encrypt.ts";
import { FakePig, makeCore, STAMP, startRun } from "./helpers.ts";

type Made = Awaited<ReturnType<typeof makeCore>>;
const cores: Made[] = [];
afterEach(async () => {
  for (const { core, store } of cores.splice(0)) {
    await core.close();
    store.close();
  }
});

/** A pretend Pig whose stroop task encrypts, and the private key that opens it. */
async function encryptingPig(partSize = 1000): Promise<{ pig: FakePig; decrypt: (bytes: Uint8Array) => Promise<Uint8Array> }> {
  const identity = await generateX25519Identity();
  const pig = new FakePig();
  pig.tasks.stroop.public_key = await identityToRecipient(identity);
  pig.tasks.stroop.max_part_size = partSize;
  const decrypter = new Decrypter();
  decrypter.addIdentity(identity);
  return { pig, decrypt: (bytes) => decrypter.decrypt(bytes) };
}

async function setUp(pig: FakePig): Promise<Made> {
  const made = await makeCore({ pig });
  cores.push(made);
  return made;
}

function fromBase64(text: string): Uint8Array {
  return Uint8Array.from(atob(text), (character) => character.charCodeAt(0));
}

describe("a task that encrypts", () => {
  test("sends each event's data encrypted, the first event included", async () => {
    const { pig, decrypt } = await encryptingPig();
    const { core } = await setUp(pig);
    const run = await startRun(core);
    await core.add(run.id, { trial: 1, rt: 523 }, STAMP);
    await core.sent(run.id);

    const stored = pig.stored("run-1");
    assert.equal(typeof stored["1"], "string");
    const opened = JSON.parse(new TextDecoder().decode(await decrypt(fromBase64(stored["1"]))));
    assert.deepEqual(opened, { trial: 1, rt: 523, _client: STAMP });
    const first = JSON.parse(new TextDecoder().decode(await decrypt(fromBase64(stored["0"]))));
    assert.equal(first._client.event, "run_start");
  });

  test("sends media encrypted, in parts that fit once encrypted", async () => {
    const { pig, decrypt } = await encryptingPig(500);
    const { core } = await setUp(pig);
    const run = await startRun(core);
    const recording = new Uint8Array(1500).map((_, n) => n % 251);

    const eventId = await core.startMedia(run.id, { prompt: 3 }, STAMP);
    const parts = await core.addMedia(run.id, eventId, new Blob([recording]));
    await core.finishMedia(run.id, eventId);
    await core.sent(run.id);

    assert.ok(parts.length > 1);
    const item = pig.media("run-1", eventId)!;
    const opened: number[] = [];
    for (const number of parts) {
      const part = item.bytes.get(number)!;
      assert.ok(part.length <= 500, `part ${number} is ${part.length} bytes`);
      opened.push(...(await decrypt(part)));
    }
    assert.deepEqual(Uint8Array.from(opened), recording);
    const start = JSON.parse(new TextDecoder().decode(await decrypt(fromBase64(pig.stored("run-1")[eventId]))));
    assert.deepEqual(start, { prompt: 3, _client: STAMP });
  });
});

describe("encrypted sizes", () => {
  test("are what age makes", async () => {
    const recipient = await identityToRecipient(await generateX25519Identity());
    for (const size of [0, 1, 65535, 65536, 65537, 200000]) {
      const encrypter = new Encrypter();
      encrypter.addRecipient(recipient);
      assert.equal((await encrypter.encrypt(new Uint8Array(size))).length, encryptedSize(size));
    }
  });

  test("fill a part exactly, or as near as chunking allows", () => {
    for (const limit of [201, 500, 65536 + 200, 8 * 1024 * 1024]) {
      const fits = plaintextThatFits(limit);
      assert.ok(encryptedSize(fits) <= limit);
      assert.ok(encryptedSize(fits + 1) > limit);
    }
    assert.equal(plaintextThatFits(200), 0);
  });
});
