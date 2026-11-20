# Public runtime metadata fixtures

These deterministic gzip files contain public SCALE V15 runtime metadata, not
operator configuration or private chain data. Decompress with gzip; the SHA-256
below is of the uncompressed bytes. Gzip modification times are zero.

| Fixture | Bytes | SHA-256 | Exact runtime code hash (BLAKE2b-256) |
| --- | ---: | --- | --- |
| runtime483-v15.scale.gz | 411511 | eb3df3b6d0f2677d96e515d667259ec046cedd6059a4bdfc6848d44f5fd1b15a | 99856ec64a61eb8011999a803bd336979512275214a173fea583abd5c89952d2 |
| runtime492-v15.scale.gz | 440461 | a397a753ad67ba335fb02cd5c54ccb0c1149fbf981f3ed6d9df44d49ca5bf71d | 22786f72f280dc968f38a10627c085eaf3ea530dd3a1621a4776882851ac3d8e |

The explicit `Metadata_metadata_at_version(15)` API produced these bytes.
They must not be confused with the legacy V14 metadata digest. Official release
WASM identity and source were checked separately; a metadata digest alone does
not establish runtime execution semantics.

- [Official v483 release](https://github.com/RaoFoundation/subtensor/releases/tag/v483), source [db3fdeb98bc82c38b238bfac9f6a3303a3361ee4](https://github.com/RaoFoundation/subtensor/commit/db3fdeb98bc82c38b238bfac9f6a3303a3361ee4).
- [Official v492 release](https://github.com/RaoFoundation/subtensor/releases/tag/v492), source [541a50d38764acdce23578bd8ba54f80156ffb2a](https://github.com/RaoFoundation/subtensor/commit/541a50d38764acdce23578bd8ba54f80156ffb2a).

In both reviewed sources, `pallets/subtensor/src/coinbase/run_coinbase.rs`
selects burn for `Ok(Burn) | Err(_)` and recycling for `Ok(Recycle)`.
The metadata declares `SubtensorModule.RecycleOrBurn` as an Identity-keyed
`NetUid(u16)` map with Default `00`, Burn variant 0 and Recycle variant 1.
For example the subnet 82 key is
`0x658faa385070e074c85bf6b568cf055530823bc1353bfe6cb262413a5bd7a0235200`.

Tests use full real metadata rather than patching the admitted digest. They
mutate metadata, code identity, framing and raw responses to prove refusal.
No fixture records a live publication, realized burn or release qualification.
