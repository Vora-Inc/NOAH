# Synthetic raw report integration

`../../raw_export_fixture.py` generates an entirely synthetic chain response set.
`../../test_raw_export_integration.py` exercises its raw bytes through the actual
SDK, collector, calculation, roster reader, finality checks, paired adjudicator,
private replay snapshot, default exporter and optional website consumer.

Only the JSON-RPC transport is substituted. No calculation, decoded SDK block or
event, roster, finality proof, verdict, resolver or verifier result is mocked.
All accounts, state, header chains, timestamps, payloads, storage and runtime API
responses are generated for this fixture. The signed-extrinsic encoding uses an
explicit all-zero signature and a public-only key: no signing or broadcast occurs.

The fixture uses Finney's genesis **as a protocol-domain test value from its
creation**. It does not rewrite real Testnet receipts. Its SN82, spec490 and
`points-v20` labels are test inputs that exercise the unchanged production shape
guards, not claims about an observed Mainnet round. Header roots and signatures
are synthetic. RPC replay proves internal consistency under the reader trust
model; it does not prove cryptographic consensus or actual runtime API behavior.

The only reused artifact is the pinned SCALE V14 **runtime metadata schema** in
`runtime490-metadata.hex.gz`, associated with runtime490. It supplies decoding
types, pallet declarations and runtime constants; no RPC receipts, account state, signed transaction or historical chain
observations are copied into these synthetic responses. The original acquisition
commit is unavailable, so the following byte identities identify a test fixture,
not a qualified release or independently authenticated chain state.

| Representation | Bytes | SHA-256 |
| --- | ---: | --- |
| Deterministic gzip | 144896 | `2fbef3d4b114bfafdef825802a81527c180c6a442b8f367486ff380098d73cd4` |
| Expanded hexadecimal text | 792418 | `36f5b6c6d7ce0d56e3a91582cd7951c4c0c931a9ccb43c9cf8da97777d7ceb3c` |
| Decoded SCALE metadata | 396208 | `a76cbe520927d0e232533b84bef160e029b0cd8acf782de458d2d2008500bebc` |

The fake activation receipt says `SYNTHETIC RAW-SCALE INTEGRATION ONLY; NOT MAINNET
OR ACTIVATION PROOF`. It and every generated public JSON/receipt live exclusively
under pytest's temporary directory. Nothing enters the website's approved report
index. Never publish these generated reports or treat the fixture receipt as
operator approval.

Run after installing the report environment:

```sh
python -m pytest noah/reports/tests/test_raw_export_integration.py -q
```

The optional website consumer is not distributed here. Without a compatible
external checkout selected by `NOAH_REPORT_WEBSITE_ROOT`, or without Node, only that consumer test skips; the default provider/exporter tests still run. Qualification records
must distinguish that skip. The successful integrated run covers:

- Two reader sessions, raw SCALE timestamp/commit/reveal and success events,
  exact owner/registration roster, complete cutoff and real engine arithmetic.
- The RPC advertises `state_getStorageAt`, exercising the installed SDK's actual
  alias selection in collection and raw replay. Roster/engine reads also use
  `state_getStorage`; conflicting values under the two spellings are rejected
  even with recomputed response and package digests.
- Default offline adjudication and immutable private export receipt, followed
  by acceptance of the actual generated JSON by the website consumer.
- A changed final verdict with a freshly rehashed inventory is rejected.
- A changed raw commitment block with recomputed response/inventory hashes is
  rejected when it cannot reproduce the retained final verdict.
- An altered cutoff claim is rejected against raw timestamp storage.
- Reader disagreement remains unresolved, preserves the miner weight and
  refuses public export without creating an approval/completion artifact.

Stable successful report ID:
`ee0d98f0f30521f24e9456d0a58632a05c811bb1baac0d035d6f2bc6776a285e`.
Filename: `2026-11-15-sn82-ee0d98f0f305.json`.
Outputs: `42307` ppm, `5000000000000` rao, `11111112000000` rao.

This closes the synthetic raw provider-to-exporter integration gap. Actual
paired-reader Testnet qualification, deployed resource limits/recovery, runtime
compatibility and the first separately approved Mainnet publication remain
distinct requirements.
