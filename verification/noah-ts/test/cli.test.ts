// End to end: the fixture runner over the local smoke fixtures, checking the
// canonical output bytes the differential depends on.

import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..");
const cli = join(root, "src", "cli.ts");
const smoke = join(root, "fixtures", "local", "smoke-steady.json");

function run(args: string[]) {
  return spawnSync(process.execPath, [cli, ...args], { encoding: "utf8" });
}

test("run-fixture writes canonical noah-score-output/6", () => {
  const out = mkdtempSync(join(tmpdir(), "noah-ts-cli-"));
  const res = run(["run-fixture", smoke, out]);
  assert.equal(res.status, 0, res.stderr);

  // The output filename is the fixture name plus .score.json.
  const text = readFileSync(join(out, "smoke-steady.score.json"), "utf8");
  assert.ok(text.endsWith("}\n"), "trailing newline required");
  assert.ok(!text.includes("\r"), "LF only");

  const doc = JSON.parse(text);
  assert.equal(doc.schema, "noah-score-output/6");
  assert.equal(doc.fixture, "smoke-steady");
  assert.equal(doc.evalDay, "40");
  // #193 codex review: every miner row carries its own seat identity now.
  for (const m of doc.miners as Array<{ uid: number | null }>) {
    assert.ok(m.uid === null || typeof m.uid === "number", "uid must be a number or null");
  }

  // Keys sorted lexicographically at every depth.
  const checkSorted = (v: unknown, path: string): void => {
    if (Array.isArray(v)) {
      v.forEach((x, i) => checkSorted(x, `${path}[${i}]`));
    } else if (typeof v === "object" && v !== null) {
      const keys = Object.keys(v);
      assert.deepEqual(keys, [...keys].sort(), `unsorted keys at ${path}`);
      for (const k of keys) checkSorted((v as Record<string, unknown>)[k], `${path}.${k}`);
    }
  };
  checkSorted(doc, "$");

  // Weight vector sums to exactly 65535 over active UIDs.
  const sum = Object.values(doc.weightsU16 as Record<string, number>).reduce((a, b) => a + b, 0);
  assert.equal(sum, 65535);

  // Deterministic: a second run produces identical bytes.
  const out2 = mkdtempSync(join(tmpdir(), "noah-ts-cli-"));
  const res2 = run(["run-fixture", smoke, out2]);
  assert.equal(res2.status, 0, res2.stderr);
  assert.equal(readFileSync(join(out2, "smoke-steady.score.json"), "utf8"), text);
});

test("a fixture with an unknown field is rejected, exit 1", () => {
  const dir = mkdtempSync(join(tmpdir(), "noah-ts-cli-bad-"));
  const bad = JSON.parse(readFileSync(smoke, "utf8"));
  bad.surprise = true;
  const badPath = join(dir, "bad.json");
  writeFileSync(badPath, JSON.stringify(bad), "utf8");
  const res = run(["run-fixture", badPath, dir]);
  assert.equal(res.status, 1);
  assert.match(res.stderr, /unknown field "surprise"/);
});

test("usage error without run-fixture subcommand", () => {
  const res = run(["--fixtures", "x"]);
  assert.equal(res.status, 2);
});
