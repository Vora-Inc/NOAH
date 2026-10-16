// CLI: node src/cli.ts run-fixture <input.json> <outdir>
//
// Reads one `noah-fixture/6` input, scores it, and writes
// <outdir>/<fixture name>.score.json as canonical JSON: schema
// `noah-score-output/6`, sorted keys, two-space indent, trailing newline. That
// exact serialization is what lets the differential compare this output
// against the Python implementation's byte for byte.

import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { canonicalJson } from "./canonical.ts";
import { FixtureError, loadFixture } from "./fixture.ts";
import { resultToJson, scoreFixture } from "./score.ts";

function usage(): never {
  console.error("usage: node src/cli.ts run-fixture <input.json> <outdir>");
  process.exit(2);
  throw new Error("unreachable");
}

const args = process.argv.slice(2);
if (args.length !== 3 || args[0] !== "run-fixture") usage();
const [, inputPath, outDir] = args;

let text: string;
try {
  text = readFileSync(inputPath, "utf8");
} catch (e) {
  console.error(`cannot read ${inputPath}: ${(e as Error).message}`);
  process.exit(1);
  throw new Error("unreachable");
}

let out: string;
let summary: string;
let outName: string;
try {
  const fixture = loadFixture(text);
  const result = scoreFixture(fixture);
  out = canonicalJson(resultToJson(result));
  const weightsSet = result.weightsU16.size > 0;
  summary = `${fixture.name}: miners=${result.miners.length} evalDay=${result.evalDay} weightsSet=${weightsSet}`;
  // The output filename is the fixture NAME plus .score.json.
  outName = `${fixture.name}.score.json`;
} catch (e) {
  // A stable, machine-readable "refused[CODE]:" prefix when the refusal
  // carries one (FixtureError.code -- see that class's own docstring): the
  // twin proof's own refusal-fixture check greps for this exact prefix
  // from both implementations and compares the code, never the free-text
  // message that follows it. A refusal with no code (any other thrown
  // error, or a FixtureError raised without one) prints the ordinary form
  // unchanged.
  const code = e instanceof FixtureError ? e.code : undefined;
  const message = (e as Error).message;
  console.error(code ? `refused[${code}]: ${inputPath}: ${message}` : `${inputPath}: ${message}`);
  process.exit(1);
  throw new Error("unreachable");
}

mkdirSync(outDir, { recursive: true });
const outPath = join(outDir, outName);
writeFileSync(outPath, out, "utf8");
console.log(`${summary} -> ${outPath}`);
