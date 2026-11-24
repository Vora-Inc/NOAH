# Points mode selection

> Mainnet uses V2.1 (`points-204040-v6`, policy/6) from November 24, 2026 at 00:00 UTC (scoring day 1345). V2 (`points-204040-v5`, policy/5) remains the policy for its earlier results.

Mainnet uses `points-204040-v6` (V2.1): see [the full ordinary scoring rules](ORDINARY_SCORING.md).
The default remains `legacy`; installing code or selecting a parameter profile
alone does not activate a validator mode. Mode, verified migration/pricing policy,
profile and publication state must agree.

Previous v2/v3 rules are documented only for historical reproduction in
[legacy points mode](LEGACY_POINTS_MODE.md). V4 EMA accounting is preserved by
V5; the ordinary onboarding and points rules change, not the receipt pricing policy.
For validator setup use [validator setup](VALIDATOR_QUICKSTART.md).
