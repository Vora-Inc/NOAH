# Terms used in Noah

> **Historical starter policy:** Mainnet used starter entry from UTC day 1324 (November 3, 2026), after cutoff block **9,421,694**. V2 replaced the starter pool on November 20. See [starter entry](STARTER_ENTRY.md) for historical verification.

| Term | Meaning |
|---|---|
| TAO | Bittensor's network token, used for fees and other network operations |
| NOAH / alpha | The token for Subnet 82; another subnet has its own alpha token |
| Coldkey | The account that owns funds and authorizes their transfer |
| Hotkey | An operational identity for a miner or validator |
| UID | A miner or validator's numbered seat in a subnet |
| Netuid | A subnet's number; Noah is Subnet 82 |
| Multisig | An account that requires approvals from several signers |
| Ark coldkey | The account receiving ownership of contributed stake |
| Ark Validator | The validator hotkey required as the delegation of qualifying Ark deposits |
| Noah Validator | The validator that reads records, scores miners, and publishes weights |
| Delegation | The validator hotkey under which a stake position is held |
| Starter entry | Historical Mainnet allocation with eligibility and lifetime limits |
| Preserved entry credit | Historical credit frozen at the activation cutoff for previously qualified miners; not a fee required from new miners |
| Eligible earnings | Mining earnings counted once they are spendable, including later unlocks |
| Target | The requested share of eligible earnings, fixed at 50% by launch policy |
| PCI | The contribution index: age-adjusted qualifying coverage divided by age-adjusted eligible pay |
| Weight | A miner's relative share supplied to Bittensor's reward calculation |
| Fixture | A saved set of scoring inputs that anyone can recalculate |
| Finalized | A transaction's block has reached chain finality |
| Genesis hash | The fingerprint of a chain's first block |
| Rao | The smallest amount used here: one billion rao per TAO or alpha token |

The Ark receiving coldkey, Ark Validator hotkey, and Noah Validator hotkey are
separate roles. Confirm the published address for each rather than relying on
a wallet's display name.
