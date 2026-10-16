# Miner wallet setup and protection

The [miner quickstart](MINER_QUICKSTART.md) assumes a Bittensor coldkey you can
unlock to sign and a hotkey it controls. You can use a wallet from another
subnet; the hotkey still needs its own Subnet 82 registration. Keep the coldkey
on a trusted signing computer. Noah does not require a continuously running
miner process or an exposed axon.

The commands here use the `btcli` supplied by the repository's
`.venv-validator` environment. These commands use the version 11.1.0 syntax.
Run `./scripts/install.sh` from the checkout first, then:

```sh
export PATH="$PWD/.venv-validator/bin:$PATH"
btcli --version
btcli wallet list
```

Use the help shown by your installed version if its syntax differs. Wallet
creation and backup do not contact the chain. Registration, staking, and Ark
contributions are separate signed transactions with separate costs.

## Use an existing wallet

Record the wallet name, hotkey name, public coldkey address, and public hotkey
address. Replace the example names with yours:

```sh
export NOAH_WALLET_NAME=YOUR_WALLET_NAME
export NOAH_HOTKEY_NAME=YOUR_HOTKEY_NAME
btcli --wallet "$NOAH_WALLET_NAME" --wallet-hotkey "$NOAH_HOTKEY_NAME" wallet show
```

Confirm you can unlock the coldkey and that you have recoverable backups.
If the coldkey has no hotkey yet, create one under the same wallet using
`btcli --wallet "$NOAH_WALLET_NAME" --wallet-hotkey "$NOAH_HOTKEY_NAME" wallet new-hotkey`.
Check the local names before accepting. Do not replace your coldkey merely to
join Noah.

## Create a wallet only if needed

Choose unused local names. This creates one coldkey and one hotkey and displays
separate recovery words. The coldkey is encrypted after a password prompt;
the hotkey file is stored unencrypted by this CLI version.

```sh
btcli --wallet noah-miner --wallet-hotkey default wallet create --n-words 24
btcli --wallet noah-miner --wallet-hotkey default wallet show
```

Never use `--overwrite` on keys you intend to keep. Write down both recovery
phrases offline, label which key each restores, and check the backup before
funding. Never paste a phrase, private key, or wallet password into a shell
command, INI file, chat, issue, or support request. For a restore, read your
installed CLI's `wallet regen-coldkey --help` and `wallet regen-hotkey --help`
and enter secrets only through a trusted prompt.

## Protect the signer

- Keep the coldkey and its backup away from an exposed server. Restrict access
  to `~/.bittensor/wallets/` and back up its files securely.
- These examples use the Noah deposit helper with a **local software wallet**.
  Never import hardware-wallet recovery words to make the examples work.
- An SS58 address does not identify mainnet by itself. The endpoint and genesis
  check in the [quickstart](MINER_QUICKSTART.md#1-install-and-choose-an-rpc)
  select the chain before any send.

Once you have the wallet names, both public addresses, and backups, return to
[the quickstart](MINER_QUICKSTART.md#2-have-a-signing-wallet).
