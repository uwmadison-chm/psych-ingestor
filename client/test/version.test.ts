// The version the client records in every run has to be the one it's published as, and
// the client and the server share a version number. `just version` sets all three.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { CLIENT_VERSION } from "../src/version.ts";

test("src/version.ts matches package.json", () => {
  const { version } = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8"));
  assert.equal(CLIENT_VERSION, version);
});

test("package.json matches the server's pyproject.toml", () => {
  const { version } = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8"));
  const pyproject = readFileSync(new URL("../../pyproject.toml", import.meta.url), "utf8");
  assert.equal(pyproject.match(/^version = "(.*)"$/m)?.[1], version);
});
