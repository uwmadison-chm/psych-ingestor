// Media items, and the order calls are handled in, run in Node against a pretend Pig.

import assert from "node:assert/strict";
import { afterEach, describe, test } from "node:test";

import { FakePig, makeCore, STAMP, startRun } from "./helpers.ts";

type Made = Awaited<ReturnType<typeof makeCore>>;
const cores: Made[] = [];
async function setUp(options: Parameters<typeof makeCore>[0]): Promise<Made> {
  const made = await makeCore(options);
  cores.push(made);
  return made;
}
afterEach(async () => {
  for (const { core, store } of cores.splice(0)) {
    await core.close();
    store.close();
  }
});

/** A pretend Pig whose stroop task takes media, in parts of at most `partSize` bytes. */
function mediaPig(partSize = 1000): FakePig {
  const pig = new FakePig();
  pig.tasks.stroop.max_part_size = partSize;
  return pig;
}

const AUDIO = { content_type: "audio/webm", prompt: 3 };

describe("a media item", () => {
  test("is an event, with its parts stored in order and then finished", async () => {
    const pig = mediaPig();
    const { core } = await setUp({ pig });
    const run = await startRun(core);

    const eventId = await core.startMedia(run.id, AUDIO, STAMP);
    assert.equal(eventId, "1");
    assert.deepEqual(await core.addMedia(run.id, eventId, new Blob(["one "])), [1]);
    assert.deepEqual(await core.addMedia(run.id, eventId, new Blob(["two "])), [2]);
    assert.deepEqual(await core.addMedia(run.id, eventId, new Blob(["three"])), [3]);
    await core.finishMedia(run.id, eventId);
    await core.sent(run.id);

    assert.deepEqual(pig.stored("run-1")["1"], { ...AUDIO, _client: STAMP });
    const item = pig.media("run-1", "1")!;
    assert.equal(pig.joined(item), "one two three");
    assert.equal(item.finished, true);
  });

  test("is refused when the task doesn't take media", async () => {
    const { core } = await setUp({ pig: new FakePig() });
    const run = await startRun(core);
    await assert.rejects(core.startMedia(run.id, AUDIO, STAMP), { code: "media-off" });
  });

  test("cuts a blob bigger than the largest part into several, in order", async () => {
    const pig = mediaPig(4);
    const { core } = await setUp({ pig });
    const run = await startRun(core);
    const eventId = await core.startMedia(run.id, AUDIO, STAMP);
    assert.deepEqual(await core.addMedia(run.id, eventId, new Blob(["abcdefghij"])), [1, 2, 3]);
    await core.finishMedia(run.id, eventId);
    await core.sent(run.id);
    const item = pig.media("run-1", "1")!;
    assert.deepEqual([...item.parts.values()].sort(), ["abcd", "efgh", "ij"]);
    assert.equal(pig.joined(item), "abcdefghij");
    assert.equal(item.finished, true);
  });

  test("skips an empty blob", async () => {
    const pig = mediaPig();
    const { core } = await setUp({ pig });
    const run = await startRun(core);
    const eventId = await core.startMedia(run.id, AUDIO, STAMP);
    assert.deepEqual(await core.addMedia(run.id, eventId, new Blob([])), []);
  });

  test("numbers parts in the order they were added, even when nobody waits", async () => {
    const pig = mediaPig();
    const { core } = await setUp({ pig });
    const run = await startRun(core);
    const eventId = await core.startMedia(run.id, AUDIO, STAMP);
    const letters = "abcdefghijklmnopqrst".split("");
    const numbered = await Promise.all(letters.map((letter) => core.addMedia(run.id, eventId, new Blob([letter]))));
    assert.deepEqual(numbered.flat(), letters.map((_, i) => i + 1));
    await core.finishMedia(run.id, eventId);
    await core.sent(run.id);
    assert.equal(pig.joined(pig.media("run-1", "1")!), letters.join(""));
  });

  test("won't take parts once it's finished", async () => {
    const { core } = await setUp({ pig: mediaPig() });
    const run = await startRun(core);
    const eventId = await core.startMedia(run.id, AUDIO, STAMP);
    await core.finishMedia(run.id, eventId);
    await assert.rejects(core.addMedia(run.id, eventId, new Blob(["late"])), { code: "finished" });
  });

  test("goes up before a finalize queued after it", async () => {
    const pig = mediaPig();
    const { core } = await setUp({ pig });
    const run = await startRun(core);
    const eventId = await core.startMedia(run.id, AUDIO, STAMP);
    core.addMedia(run.id, eventId, new Blob(["all of it"]));
    core.finishMedia(run.id, eventId);
    core.finalize(run.id);
    await core.sent(run.id);
    assert.equal(pig.joined(pig.media("run-1", "1")!), "all of it");
    assert.equal(pig.run("run-1").status, "finalizing");
  });

  test("recorded offline goes up once back online", async () => {
    const pig = mediaPig();
    const { core } = await setUp({ pig });
    await startRun(core); // online once, so the settings are saved
    pig.offline = true;
    const run = await startRun(core);
    const eventId = await core.startMedia(run.id, AUDIO, STAMP);
    await core.addMedia(run.id, eventId, new Blob(["offline "]));
    await core.addMedia(run.id, eventId, new Blob(["recording"]));
    await core.finishMedia(run.id, eventId);
    const pending = await core.pending({ run: run.id });
    assert.equal(pending.events, 2); // the run's first event, and the media item's
    assert.ok(pending.bytes > "offline recording".length);

    pig.offline = false;
    core.nudge();
    await core.sent(run.id);
    const item = pig.media("run-2", "1")!;
    assert.equal(pig.joined(item), "offline recording");
    assert.equal(item.finished, true);
  });

  test("refused for good is kept aside and reported, and the run carries on", async () => {
    const pig = mediaPig();
    const { core, notes } = await setUp({ pig });
    const run = await startRun(core);
    const eventId = await core.startMedia(run.id, AUDIO, STAMP);
    await core.sent(run.id);
    // The server already has a different part 1, so ours collides.
    pig.media("run-1", "1")!.parts.set(1, "something else");

    await core.addMedia(run.id, eventId, new Blob(["ours"]));
    await core.addMedia(run.id, eventId, new Blob(["more"]));
    await core.finishMedia(run.id, eventId);
    await core.add(run.id, { trial: 1 }, STAMP);
    await core.sent(run.id);

    assert.equal(pig.stored("run-1")["2"].trial, 1);
    assert.ok(notes.some((n) => n.type === "error" && n.code === "media-refused"));
    // Both parts and the finish are kept on the device.
    assert.equal((await core.pending({ run: run.id })).failed, 3);
  });

  test("whose start is too big is refused when it's started", async () => {
    const { core } = await setUp({ pig: mediaPig() });
    const run = await startRun(core);
    await assert.rejects(core.startMedia(run.id, { notes: "x".repeat(2000) }, STAMP), { code: "too-big" });
    assert.equal(await core.add(run.id, { trial: 1 }, STAMP), "1"); // The ID wasn't used up.
  });

  test("that has failed keeps parts added later aside too, without sending them", async () => {
    const pig = mediaPig();
    const { core } = await setUp({ pig });
    const run = await startRun(core);
    const eventId = await core.startMedia(run.id, AUDIO, STAMP);
    await core.sent(run.id);
    pig.media("run-1", "1")!.parts.set(1, "something else");
    await core.addMedia(run.id, eventId, new Blob(["ours"]));
    await core.sent(run.id);
    const partsSent = pig.sent("part").length;

    await core.addMedia(run.id, eventId, new Blob(["later"]));
    await core.sent(run.id);
    assert.equal(pig.sent("part").length, partsSent);
    assert.equal((await core.pending({ run: run.id })).failed, 2);
  });
});

describe("a run that expires partway through a recording", () => {
  test("sends the rest to a new server run, under the same part numbers, unfinished", async () => {
    const pig = mediaPig();
    const { core } = await setUp({ pig });
    const run = await startRun(core);
    const eventId = await core.startMedia(run.id, AUDIO, STAMP);
    await core.addMedia(run.id, eventId, new Blob(["first "]));
    await core.sent(run.id);

    pig.expire("run-1");
    await core.addMedia(run.id, eventId, new Blob(["second"]));
    await core.finishMedia(run.id, eventId);
    await core.sent(run.id);

    const before = pig.media("run-1", "1")!;
    const after = pig.media("run-2", "1")!;
    assert.deepEqual([...before.parts.keys()], [1]);
    assert.deepEqual([...after.parts.keys()], [2]);
    assert.equal(pig.joined(before) + pig.joined(after), "first second");
    assert.equal(before.finished, false);
    assert.equal(after.finished, false);
    assert.equal(pig.stored("run-2")["0"]._client.continues_run, "run-1");
    assert.deepEqual(pig.stored("run-2")["1"], pig.stored("run-1")["1"]);
  });

  test("sends all of it to the new server run, finished, if no part had gone yet", async () => {
    const pig = mediaPig();
    const { core } = await setUp({ pig });
    const run = await startRun(core);
    const eventId = await core.startMedia(run.id, AUDIO, STAMP);
    await core.sent(run.id);

    pig.expire("run-1");
    await core.addMedia(run.id, eventId, new Blob(["whole"]));
    await core.finishMedia(run.id, eventId);
    await core.sent(run.id);

    const after = pig.media("run-2", "1")!;
    assert.equal(pig.joined(after), "whole");
    assert.equal(after.finished, true);
  });
});

describe("order", () => {
  test("events get IDs in the order they were added, even when nobody waits", async () => {
    const { core } = await setUp({ pig: new FakePig() });
    const run = await startRun(core);
    const ids = await Promise.all(Array.from({ length: 20 }, (_, i) => core.add(run.id, { trial: i }, STAMP)));
    assert.deepEqual(ids, Array.from({ length: 20 }, (_, i) => String(i + 1)));
  });

  test("sent() waits for an add() made just before it that nobody waited for", async () => {
    const pig = new FakePig();
    const { core } = await setUp({ pig });
    const run = await startRun(core);
    await core.sent(run.id);
    core.add(run.id, { trial: 1 }, STAMP);
    await core.sent(run.id);
    assert.equal(pig.stored("run-1")["1"].trial, 1);
  });
});
