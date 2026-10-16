# Security policy

## Report a vulnerability privately

For a vulnerability in this repository, use GitHub **private
vulnerability reporting**: select **Security → Report a vulnerability** on
this repository. Do not open a public issue with an undisclosed vulnerability,
exploit details, credentials, or wallet material.

A report should identify the affected commit, behavior, likely impact, and a
small reproduction using test identities. Never send a recovery phrase,
private key, API key, or funded wallet file.

## Supported version

Production operators should retain the exact code commit and dependency
versions they deploy. Security fixes should identify the affected versions.

## Keys and endpoints

Miners control their own signing keys. The deposit helper supports a local
software wallet; it does not sign through a hardware wallet, multisig,
extension, or miner proxy. Do not import a hardware wallet's recovery words to
make it work.

The validator can publish through a separate operational proxy key. Protect
that key and its recovery copy. The validator owner's hardware wallet seed and
funds-controlling coldkeys do not belong on the validator host. Keep RPC
credentials outside the repository and ordinary configuration backups.

Local tests and secret scanning reduce risk but do not establish that every
chain input or deployed service is correct, or that every file is safe to
publish. Review the repository's reachable history, branches, tags, and
attachments before changing visibility.
