// The version the client records in every run has to be the one it's published as.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { CLIENT_VERSION } from "../src/version.ts";

test("src/version.ts matches package.json", () => {
  const { version } = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8"));
  assert.equal(CLIENT_VERSION, version);
});
