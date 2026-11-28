# Runtime compatibility

Clients can load signed approvals for newly reviewed chain runtimes without a
software update. An approval binds the chain genesis, spec version, transaction
version, exact WASM hash, V15 metadata digest, upstream source commit and the
reviewed capabilities. Existing packaged qualifications remain usable.

Set `NOAH_RUNTIME_APPROVAL_KEY` to the trusted Ed25519 public key as 64 lowercase
hex digits. Set `NOAH_RUNTIME_APPROVALS` to the locally installed approval file.
Obtain the public key from the release operator through a trusted channel.
Treat the file and download URL as untrusted until signature verification passes.
Use the validator or isolated report runtime for imports that need PyCryptodome.
The public key is for runtime approval only and is independent of miner and
validator wallets.

Install a cached registry atomically from an approved source with the same key
configured on the client. This example reads a downloaded file.

```sh
python -m noah.chain.runtime_upgrade install \
  --path "$NOAH_RUNTIME_APPROVALS" \
  --key "$NOAH_RUNTIME_APPROVAL_KEY" \
  --file /path/to/downloaded-approvals
```

`--url` accepts an HTTPS endpoint without redirects. A failed fetch or a signed
document that drops an existing approval leaves the previous file in place.
Retain the file with historical scoring evidence so verifiers can still read
proofs from an earlier runtime. Rotate a compromised approval key through the
normal managed release and verify historical evidence before retiring its files.

Operators can exercise a prospective runtime with `runtime_upgrade rehearse`
on two finalized qualification readers. For an unapproved runtime provide
`--source` with the full reviewed source commit and `--wasm` with the exact
candidate bytes. The command uses disposable trust scoped to read-only proof
readers and composes unsigned calls. A production approval still requires
behavioral review and the operator's signature.

The first line is the lowercase hex signature. The signed bytes start on the
next line with `noah-runtime-approvals/1`, followed by tab-separated rows with
these fields in order. End every line with LF.

```text
genesis  spec  transaction  wasm_hash  metadata_sha256  source_commit  capabilities
```

Use `0x` for genesis and WASM hashes. Use lowercase hex without a prefix for
metadata and source commit. Sort rows by genesis and numeric spec. Sort
capabilities alphabetically with commas between them. Available capabilities
are `burn`, `ema`, `reports`, `token` and `weights`. EMA requires token approval.
