"""Manual, miner-controlled Ark deposit assistant.

Amounts are whole rao of the subnet alpha. The miner reviews the proposal and
explicitly confirms it before a locally supplied wallet signs the transaction.
Signing and receipt errors must be inspected without exposing wallet material."""
