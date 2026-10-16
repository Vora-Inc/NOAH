// Canonical JSON writer. The whole point is that two implementations can be
// compared with diff rather than with a tolerance, so the serialization is
// fixed exactly:
//   - UTF-8, LF line endings, 2-space indent, trailing newline
//   - object keys sorted lexicographically at every depth
//   - arrays in schema-defined order (miners in input order)
//   - BigInt values serialized as decimal strings (quoted digits)
//   - booleans and small integers (u16 weights) native; no floats, no exponents
//
// This writes the text directly from a sorted intermediate tree that is
// tagged by kind ("obj" / "arr" / "leaf") -- it never assigns sorted keys
// onto a plain `{}` object at any point. That used to be exactly what this
// function did (build a plain object with `Object.keys(src).sort()` order,
// then hand it to `JSON.stringify`), which is broken for any object whose
// keys are integer-index-like strings (e.g. weightsU16's uids: "1".."36"+):
// the ECMAScript spec (10.4.5.15, "OrdinaryOwnPropertyKeys") mandates that a
// plain object's own integer-index string keys are ALWAYS enumerated in
// ascending numeric order by `Object.keys`/`JSON.stringify`, regardless of
// insertion order or any prior `.sort()` call. That silently defeated the
// lexicographic order this contract requires as soon as 10+ such keys
// existed together (numeric and lexicographic order first diverge at
// "10" vs "2"). A tagged node, unlike a bare array of `[key, value]` pairs,
// also cannot be confused with a schema array value that happens to itself
// hold two-element string-keyed tuples, since the "obj" tag is explicit
// rather than inferred from shape.

type Sorted =
  | { kind: "obj"; entries: [string, Sorted][] }
  | { kind: "arr"; items: Sorted[] }
  | { kind: "leaf"; value: unknown };

function sortValue(value: unknown): Sorted {
  if (Array.isArray(value)) return { kind: "arr", items: value.map(sortValue) };
  if (typeof value === "object" && value !== null) {
    const src = value as Record<string, unknown>;
    const entries: [string, Sorted][] = Object.keys(src)
      .sort()
      .map((k) => [k, sortValue(src[k])]);
    return { kind: "obj", entries };
  }
  return { kind: "leaf", value };
}

function writeJson(node: Sorted, depth: number): string {
  if (node.kind === "leaf") {
    const v = node.value;
    return typeof v === "bigint" ? JSON.stringify(v.toString()) : JSON.stringify(v);
  }
  const pad = "  ".repeat(depth + 1);
  const close = "  ".repeat(depth);
  if (node.kind === "obj") {
    if (node.entries.length === 0) return "{}";
    const items = node.entries.map(
      ([k, v]) => `${pad}${JSON.stringify(k)}: ${writeJson(v, depth + 1)}`,
    );
    return `{\n${items.join(",\n")}\n${close}}`;
  }
  // node.kind === "arr"
  if (node.items.length === 0) return "[]";
  const items = node.items.map((v) => `${pad}${writeJson(v, depth + 1)}`);
  return `[\n${items.join(",\n")}\n${close}]`;
}

export function canonicalJson(value: unknown): string {
  return writeJson(sortValue(value), 0) + "\n";
}
