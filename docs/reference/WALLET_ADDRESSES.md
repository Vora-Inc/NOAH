# Noah mainnet deposit addresses

These shared addresses identify the Ark deposit recipient and required delegation
destination on Bittensor mainnet, subnet82. They are not a validator operator's
scoring identity or publication proxy.

| Role | Address |
|---|---|
| Ark receiving coldkey | `5EL1nNebm9e3iKBz3G8vELKYqYVWN4iyda8a79esydKBsba3` |
| Required Ark Validator hotkey | `5DLQGc9DoabXWGp4s4vktHyGEA3itg8twResPfDZ4qw8RZJV` |

Validator operators must configure the scoring identity and dedicated signing
proxy authorized for their deployment. Never put keys or recovery material in
a configuration example or commit a completed environment file. See the
[validator guide](../VALIDATOR_QUICKSTART.md) for this edition's startup restrictions.

## Existing Noah deployment identities

The following public references describe Noah's deployment, not a publishing
allowlist. Independent operators configure their own registered scoring identity
and its dedicated authorized proxy. The application checks those configured
identities; it does not require the owner hotkey below. The chain must still grant
a publishing permit. Under Null only the elected highest-stake UID holds one.
The shared Ark deposit addresses above remain fixed for all operators.

| Role | Address |
|---|---|
| Noah scoring identity (`NOAH_EXPECTED_SIGNER_ADDRESS`) | `5Hn6PHyjx2rLY3EXCP1xK7XHUquRo17fyuyM1HuZrAWb8y3G` |
| Subnet owner multisig | `5GGrRdUatgDLiJxnytdWfYCwsAb1Cyo4U5Dw1HmLiAcbWj88` |
| Ark Validator controlling multisig | `5Gj3HZJdLC1hASrn32NR4mttimJ8UPb63ftJATUraNMuWohX` |

## On-chain Ark reference

Noah records the Ark receiving address in the `additional` field of
`SubtensorModule.SubnetIdentitiesV3[82]`. This is a general subnet profile field
that Noah interprets as its receiving-address reference, not a native Ark
registration or a transfer of funds. The exact field contents must be the
receiving address alone, with no label or JSON wrapper.

The reference must be present for each scored period. A later name or profile
edit must preserve it. The scoring adapter requires the configured address to
match this record at the scoring period's end. Historical periods without that
record are refused; publishing it later does not change earlier blocks.
Startup must account for that boundary.

Explorer display is separate: this field is readable from the chain, but this
repository does not promise an “Ark address” label on Taostats. The public
website and contribution guide must identify the recipient explicitly.
