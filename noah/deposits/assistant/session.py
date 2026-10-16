"""The guided flow: preview, run-once, status, reconcile.

Every outside thing arrives through :class:`Services`, so the whole flow runs
against fakes. Signing material appears only inside ``signer_factory``, which is
called after the miner has typed the confirmation and never before.

Safe-failure rules, in order of importance:

* The ledger records ``submitting`` BEFORE the transaction is handed to the chain.
  A crash after that point leaves the payment "maybe sent", and the next run
  reconciles against the chain before anything else can be sent.
* Transactions are signed with a short mortal era (``ERA_PERIOD`` blocks), so a
  transaction that never landed cannot land later. Only after the finalized chain
  is well past that window is an unfound attempt called ``abandoned``.
* Known limits, stated plainly. (N2) "Credited" is decided by a total comparison: an unrelated
  credit of at least the pending amount can mark a deposit credited early. (N3) Reconciling a
  deposit matches on your coldkey and miner hotkey inside its block window, so a manual deposit
  made in the same window can confuse its status; this fails safe (it holds or flags, it never
  sends again).
* Nothing retries by itself. A refusal, rejection or failure ends the run and
  says why.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from ...chain.sources import Refusal
from ...chain.ss58 import NotAnAddress, public_key_of
from ...runtime_events import bind as bind_event_context
from ...runtime_events import traced
from ..availability import deposit_ceiling
from ..chain import ceiling_from_chain
from ..deposit_submission import BytesConvertingSigner
from ..disclosures import REQUIRED_DISCLOSURES, acknowledge, disclosure_text
from . import ledger as L
from .amounts import (
    CUSTOM,
    FIXED,
    MAINTENANCE,
    PARTIAL,
    PERCENT,
    READY,
    DepositRefused,
    Limits,
    format_noah,
    propose,
)
from .assistant_tx import (
    EARNINGS,
    STAKED_ON_VALIDATOR,
    CannotCompose,
    compose,
    plan_deposit,
)
from .preview import render_summary
from .reconcile import finalized_height, find_deposit, head_height

DEFAULT_LABEL = "default"  # constant on purpose: a clock must never decide whether a repeat is new
ERA_PERIOD = 64
ABANDON_AFTER_BLOCKS = 2 * ERA_PERIOD + 20


@dataclass
class Services:
    connection: Any
    source: Any  # answers ceiling_from_chain's reads
    evidence_provider: Any  # .read() -> Evidence
    ledger: Any
    signer_factory: Callable = None  # () -> keypair-shaped; called only after confirmation
    ask: Callable = input
    say: Callable = print
    now: Callable = lambda: datetime.now(timezone.utc)
    notes: list = field(default_factory=list)
    proxy_submit: Callable = None
    proxy_check: Callable = None


def _shape(config):
    return EARNINGS if config.stake_position == "miner" else STAKED_ON_VALIDATOR


def _position_hotkey(config):
    return config.miner_hotkey if _shape(config) == EARNINGS else config.validator_hotkey


def _check_chain_identity(config, services):
    expected = getattr(config, "genesis_hash", None)
    actual = services.connection.get_block_hash(0)
    bind_event_context("miner", genesis_hash=actual)
    if expected and str(actual) != str(expected):
        raise DepositRefused(
            f"Connected chain's genesis ({actual}) is not the one in your settings ({expected}). "
            f"Refusing: this may be the wrong network.",
            "wrong_chain",
        )
    _check_mainnet_validator(config, actual)


def _check_mainnet_validator(config, actual_genesis):
    """On the mainnet genesis, whatever the settings call the network, the designated Ark
    Validator must be the one on record."""
    from ...core.chain import MAINNET
    from ...core.mainnet import ARK_VALIDATOR_HOTKEY

    if str(actual_genesis).lower() != MAINNET.lower():
        return
    typed = getattr(config, "validator_hotkey", None)
    try:
        same = bool(typed) and public_key_of(typed) == public_key_of(ARK_VALIDATOR_HOTKEY)
    except NotAnAddress:
        same = False
    if not same:
        raise DepositRefused(
            f"The connected chain is the live mainnet, and the designated Ark Validator "
            f"hotkey in your settings ({typed or 'not set'}) is not the designated Ark "
            f"Validator on record ({ARK_VALIDATOR_HOTKEY}). A deposit to any other validator "
            f"earns no mining credit. Nothing was sent. Correct designated_validator_hotkey "
            f"and run again.",
            "wrong_validator",
        )


def _read_flag(source, method, netuid):
    """The chain's effective True/False, or None unless it is a real bool.

    None, a non-bool, a raise or a missing method all give None, never True: the caller
    refuses on None.
    """
    try:
        value = getattr(source, method)(netuid)
    except Exception:  # an unreadable chain answer must never read as "enabled"
        return None
    return value if isinstance(value, bool) else None


def _check_transaction_can_succeed(config, services):
    """Refuse before any signature if the chain would revert the transfer.

    These are the checks the transaction itself needs (not registration checks):
    the EFFECTIVE ``SubtokenEnabled[netuid]`` and subnet ``TransferToggle[netuid]`` (stored value
    or the runtime's own default). Both shapes
    submit a ``transfer_stake`` variant, so both apply to both.
    """
    netuid = config.netuid
    bind_event_context("miner", netuid=netuid)
    consequence = (
        " The chain would revert the transaction after you sign it, and it may still charge a "
        "network fee. Nothing was signed or sent."
    )
    subtoken = _read_flag(services.source, "subtoken_enabled", netuid)
    if subtoken is None:
        raise DepositRefused(SUBTOKEN_UNREADABLE.format(netuid=netuid), "subtoken_unreadable")
    if subtoken is False:
        raise DepositRefused(
            SUBTOKEN_DISABLED.format(netuid=netuid) + consequence, "subtoken_disabled"
        )
    permitted = _read_flag(services.source, "transfer_toggle", netuid)
    if permitted is None:
        raise DepositRefused(TRANSFERS_UNREADABLE.format(netuid=netuid), "transfers_unreadable")
    if permitted is False:
        raise DepositRefused(
            TRANSFERS_NOT_PERMITTED.format(netuid=netuid) + consequence, "transfers_not_permitted"
        )


def _optional_evidence(services):
    try:
        return services.evidence_provider.read()
    except Refusal:
        return None


SUBTOKEN_DISABLED = (
    "SubtokenEnabled is false for subnet {netuid}, so the chain does not allow stake transfers "
    "on it yet."
)
SUBTOKEN_UNREADABLE = (
    "The SubtokenEnabled setting for subnet {netuid} could not be read as a clear true or false, "
    "so it cannot be confirmed that stake transfers are allowed. Nothing was signed or sent."
)
TRANSFERS_NOT_PERMITTED = (
    "Stake transfers are not permitted on subnet {netuid} (its TransferToggle is false)."
)
TRANSFERS_UNREADABLE = (
    "The TransferToggle setting for subnet {netuid} could not be read as a clear true or false, "
    "so it cannot be confirmed that stake transfers are permitted. Nothing was signed or sent."
)

NOTHING_STAKED_ON_VALIDATOR = (
    "Nothing is staked on the designated Ark Validator for this coldkey yet, so nothing can be "
    "sent in this shape."
)


_INTERNAL_NOTE = re.compile(r"\s*\(Placeholder, not approved wording\.[^)]*\)\s*$")


def _shown_disclosure(topic):
    """The disclosure sentence as the miner sees it, without internal spec references."""
    return _INTERNAL_NOTE.sub("", disclosure_text(topic))


def _read_inputs(config, services):
    return ceiling_from_chain(
        services.source,
        coldkey=config.coldkey,
        hotkey=_position_hotkey(config),
        netuid=config.netuid,
        # The designated Ark Validator may hold stake before it is registered on the
        # subnet (deposits are accepted before registration). The miner shape keeps
        # the strict registered-position read.
        allow_unregistered_position=_shape(config) == STAKED_ON_VALIDATOR,
    )


def _read_ceiling(config, services):
    return _ceiling_of(_read_inputs(config, services))


def _ceiling_of(inputs):
    return deposit_ceiling(
        position_stake=inputs.position_stake,
        position_collateral=inputs.position_collateral,
        coldkey_available=inputs.coldkey_available,
    )


def _pending_credit(config, services, evidence):
    """Rao already sent and final but not yet credited, to subtract from what is due.

    Maintenance: every such deposit counts (the credited total is cumulative and lags).
    Percent: only deposits for the same earnings period. A deposit whose credit figure
    is unknown cannot be counted, so the run refuses rather than guess.
    """
    if config.mode not in (PERCENT, MAINTENANCE):
        return 0
    total = 0
    for row in services.ledger.awaiting_credit(config.miner_hotkey):
        if row["credited_before_rao"] is None:
            raise DepositRefused(
                f"An earlier deposit of {format_noah(row['amount_rao'])} is final on chain but its "
                f"credit cannot be tracked (it was sent without an earnings figure). Not "
                f"calculating a new amount, to avoid paying twice. To fix it, tell the tool the "
                f"credited total you measured just BEFORE that deposit (from the published "
                f"records):  python -m noah.deposits.assistant --config YOUR_SETTINGS_FILE "
                f"resolve {row['intent_id'][:12]} --credited-before-rao NUMBER_OF_RAO",
                "pending_unknown",
            )
        if config.mode == PERCENT:
            details = json.loads(row["details"] or "{}")
            if details.get("period_start_day") != evidence.period_start_day:
                continue
        total += row["amount_rao"]
    return total


def calculate(config, services):
    """Read evidence and chain facts, return ``(proposal, available_rao, evidence, ceiling)``."""
    _check_chain_identity(config, services)
    _check_transaction_can_succeed(config, services)
    evidence = None
    if config.mode in (PERCENT, MAINTENANCE):
        evidence = services.evidence_provider.read()
    else:
        evidence = _optional_evidence(services)
    inputs = _read_inputs(config, services)
    ceiling = _ceiling_of(inputs)
    limits = Limits(
        available_rao=ceiling.amount,
        reserve_rao=config.reserve_rao,
        cap_rao=config.cap_rao,
        minimum_rao=config.minimum_rao,
    )
    try:
        proposal = propose(
            config.mode,
            limits=limits,
            amount_rao=config.amount_rao,
            percent_bp=config.percent_bp,
            evidence=evidence,
            pending_rao=_pending_credit(config, services, evidence),
        )
    except DepositRefused as refusal:
        if (
            refusal.code == "insufficient_funds"
            and _shape(config) == STAKED_ON_VALIDATOR
            and inputs.position_stake == 0
        ):
            raise DepositRefused(NOTHING_STAKED_ON_VALIDATOR, "insufficient_funds") from refusal
        if (
            refusal.code == "insufficient_funds"
            and _shape(config) == EARNINGS
            and inputs.position_collateral > 0
        ):
            raise DepositRefused(
                _locked_collateral_message(config, inputs, ceiling), "insufficient_funds"
            ) from refusal
        raise
    from .proxy_signing import is_proxy, preflight
    if is_proxy(config) and proposal.amount_rao > 0:
        plan = plan_deposit(_shape(config), ark_coldkey=config.ark_coldkey,
            miner_hotkey=config.miner_hotkey, validator_hotkey=config.validator_hotkey,
            netuid=config.netuid, amount_rao=proposal.amount_rao, ceiling=ceiling,
            acknowledgment=acknowledge(REQUIRED_DISCLOSURES))
        _, facts = preflight(services.connection, config, compose(services.connection, plan), 'deposit')
        from .proxy_signing import request, reject
        try:
            checked = (services.proxy_check or request)(config, action='deposit', intent_id='preview',
                amount_rao=proposal.amount_rao, operation='check')
        except Exception:
            reject('proxy signer public policy check unavailable; preview is not ready')
        if checked.get('status') != 'checked':
            reject('proxy signer public policy check refused; no secrets were requested')
        facts['runtime_permission_verified'] = True
        services.say('Proxy preflight: ' + json.dumps(facts, sort_keys=True))
    return proposal, ceiling.amount, evidence, ceiling


def _locked_collateral_message(config, inputs, ceiling):
    """Why nothing can be sent when part of the stake is locked registration collateral."""
    return (
        f"Nothing can be sent: stake locked as registration collateral cannot be used for a "
        f"deposit.\n"
        f"  Total stake on this miner: {format_noah(inputs.position_stake)}\n"
        f"  Locked as registration collateral: {format_noah(inputs.position_collateral)}\n"
        f"  Available to deposit: {format_noah(ceiling.amount)}\n"
        f"  Held back as your reserve: {format_noah(config.reserve_rao)}\n"
        f"  To deposit, you need spendable NOAH (not locked) of at least the amount you "
        f"deposit plus the {format_noah(config.reserve_rao)} reserve, and TAO in the coldkey "
        f"to pay the transaction fee."
    )


@traced("miner", "miner.preview", fresh=True)
def preview(config, services):
    """Show the proposal. Sends nothing. Records only a 'calculated' note."""
    try:
        reconcile_open(config, services)  # verified chain reads; no transaction
        proposal, available, evidence, _ = calculate(config, services)
    except Refusal as refusal:
        services.say(f"No deposit can be calculated.\n  Why: {refusal}")
        services.ledger.record_calculation(
            config.miner_hotkey,
            {"headline": f"refused: {refusal}", "status": "refused"},
            now=services.now(),
        )
        return {"status": "refused", "reason": str(refusal), "code": getattr(refusal, "code", None)}
    services.say(
        render_summary(
            config,
            proposal,
            available_rao=available,
            evidence=evidence,
            shape_text=describe_shape(config),
        )
    )
    services.ledger.record_calculation(
        config.miner_hotkey,
        {
            "headline": f"{proposal.status}: {format_noah(proposal.amount_rao)}",
            "status": proposal.status,
            "amount_rao": proposal.amount_rao,
        },
        now=services.now(),
    )
    return {"status": proposal.status, "amount_rao": proposal.amount_rao}


def describe_shape(config):
    return (
        "one transfer_stake_and_hotkey from your miner hotkey's earnings to the Ark, re-delegated "
        "to the Ark Validator in the same step"
        if _shape(config) == EARNINGS
        else "a batch of transfer_stake (stake already on the Ark Validator) plus an on-chain note "
        "naming your miner hotkey"
    )


def _fingerprint(config, proposal, evidence, label, services):
    base = f"{config.network}|{config.coldkey}|{config.miner_hotkey}|{config.mode}"
    if config.mode in (PERCENT, MAINTENANCE):
        return f"{base}|through:{evidence.through_day}"
    return f"{base}|{proposal.amount_rao}|label:{label or DEFAULT_LABEL}"


def _same_key(a, b):
    try:
        return public_key_of(a) == public_key_of(b)
    except NotAnAddress:
        return False


def confirmation_phrase(amount_rao):
    return f"SEND {format_noah(amount_rao)}"


@traced("miner", "miner.submission", fresh=True)
def run_once(config, services, *, label=None):
    """The whole manual path. Returns a small result dict; never retries."""
    try:
        with services.ledger.execution_lock():
            services.ledger.check_direct_execution()
            return _run_once_locked(config, services, label=label)
    except Refusal as refusal:
        services.say(f"No deposit was started.\n  Why: {refusal}")
        return {"status": "refused", "reason": str(refusal), "code": getattr(refusal, "code", None)}


def _run_once_locked(config, services, *, label=None):
    say, led = services.say, services.ledger
    say("Checking earlier deposits against the chain first.")
    try:
        reconcile_open(config, services)
        proposal, available, evidence, ceiling = calculate(config, services)
    except Refusal as refusal:
        say(f"No deposit was started.\n  Why: {refusal}")
        return {"status": "refused", "reason": str(refusal), "code": getattr(refusal, "code", None)}

    say(
        render_summary(
            config,
            proposal,
            available_rao=available,
            evidence=evidence,
            shape_text=describe_shape(config),
        )
    )
    led.record_calculation(
        config.miner_hotkey,
        {
            "headline": f"{proposal.status}: {format_noah(proposal.amount_rao)}",
            "status": proposal.status,
            "amount_rao": proposal.amount_rao,
        },
        now=services.now(),
    )
    if proposal.status not in (READY, PARTIAL):
        return {"status": proposal.status}

    credited_before = getattr(evidence, "credited_contributions_rao", None)
    if credited_before is not None:
        # Earlier final-but-uncredited deposits will raise the credited total before this one's
        # credit does, so they belong in this deposit's starting point.
        credited_before += sum(r["amount_rao"] for r in led.awaiting_credit(config.miner_hotkey))
    try:
        intent_id = led.create_intent(
            fingerprint=_fingerprint(config, proposal, evidence, label, services),
            miner_hotkey=config.miner_hotkey,
            mode=config.mode,
            amount_rao=proposal.amount_rao,
            shape=_shape(config),
            credited_before_rao=credited_before,
            now=services.now(),
            details={
                "wanted_rao": proposal.wanted_rao,
                "through_day": getattr(evidence, "through_day", None),
                "period_start_day": getattr(evidence, "period_start_day", None),
            },
        )
    except (L.DuplicateDeposit, L.OpenIntentExists) as blocked:
        hint = ""
        if isinstance(blocked, L.DuplicateDeposit) and config.mode in (CUSTOM, FIXED):
            hint = (
                " If you really want to send the same amount again, run it with a new label, "
                "for example `run-once --label second`."
            )
        say(f"Nothing was sent.\n  Why: {blocked}{hint}")
        return {"status": "blocked", "reason": str(blocked) + hint}

    say("")
    say("Before you approve, read these notices about your contribution.")
    for topic in sorted(REQUIRED_DISCLOSURES):
        say(f"  - {_shown_disclosure(topic)}")
    from .. import disclosures  # read at call time, so the live flag decides

    if disclosures.WORDING_IS_PLACEHOLDER:
        say("  Draft wording, not yet approved.")
    phrase = confirmation_phrase(proposal.amount_rao)
    answer = services.ask(f"To approve, type exactly:  {phrase}\n> ")
    if answer.strip() != phrase:
        led.transition(intent_id, L.DECLINED, now=services.now(), note="not approved by the miner")
        say("Not approved. Nothing was sent.")
        return {"status": "declined", "intent_id": intent_id}
    try:
        led.transition(intent_id, L.APPROVED, now=services.now())
    except L.IllegalTransition as err:
        say(f"This preview was replaced by a newer run. Nothing was sent. ({err})")
        return {"status": "superseded", "intent_id": intent_id}
    acknowledgment = acknowledge(REQUIRED_DISCLOSURES)  # the typed phrase accepts the shown topics

    def fail(note, state=L.FAILED):
        led.transition(intent_id, state, now=services.now(), note=note)
        say(f"Not sent.\n  Why: {note}")
        return {"status": state, "intent_id": intent_id, "reason": note}

    # Fresh read right before building, so the ceiling is the chain's current word.
    try:
        ceiling = _read_ceiling(config, services)
        plan = plan_deposit(
            _shape(config),
            ark_coldkey=config.ark_coldkey,
            miner_hotkey=config.miner_hotkey,
            validator_hotkey=config.validator_hotkey,
            netuid=config.netuid,
            amount_rao=proposal.amount_rao,
            ceiling=ceiling,
            acknowledgment=acknowledgment,
        )
        if _shape(config) == STAKED_ON_VALIDATOR:
            owner = services.connection.query("SubtensorModule", "Owner", [config.miner_hotkey])
            owner = getattr(owner, "value", owner)
            if not _same_key(str(owner), config.coldkey):
                raise CannotCompose(
                    "Your coldkey does not currently own the miner hotkey, so the chain reader "
                    "would not credit this deposit to you. Nothing is built."
                )
    except Refusal as refusal:
        return fail(f"The transaction could not be built safely: {refusal}")

    from .proxy_signing import is_proxy, request
    if is_proxy(config):
        # The signer reconstructs the call from its trusted policy and independently
        # checks owner, miner, limits, runtime, delegation, reserve and fee balance.
        led.transition(intent_id, L.SUBMITTING, now=services.now(), start_block=head_height(services.connection))
        try:
            result = (services.proxy_submit or request)(config, action='deposit',
                intent_id=intent_id, amount_rao=proposal.amount_rao)
        except (Exception, Refusal):
            result = {'status': 'uncertain'}
        if result['status'] in ('failed', 'refused'):
            return fail('Proxy signer refused or finalized an inner dispatch failure; inspect signer journal.')
        if result['status'] != 'finalized':
            led.transition(intent_id, L.UNCERTAIN, now=services.now(), note='proxy outcome uncertain; no automatic resubmission')
            return {'status': L.UNCERTAIN, 'intent_id': intent_id}
        led.transition(intent_id, L.SUBMITTED, now=services.now(),
            extrinsic_hash=result.get('extrinsic_hash'), block_hash=result.get('block_hash'),
            block_number=result.get('block_number'))
        state = reconcile_intent(config, services, led.get(intent_id))
        return {'status': state, 'intent_id': intent_id}

    if services.signer_factory is None:
        return fail("No signer is configured.")
    say(
        "Opening your wallet to sign. If it asks for your wallet password, "
        "that approval is yours to give."
    )
    try:
        signer = services.signer_factory()
    except Exception as err:  # wallet errors can echo secrets; keep only the kind
        return fail(f"Your wallet could not be opened ({type(err).__name__}).")
    if not _same_key(getattr(signer, "ss58_address", ""), config.coldkey):
        return fail("The wallet that opened is not the coldkey named in your settings.")

    conn = services.connection
    try:
        start_block = head_height(conn)
    except Exception as err:
        return fail(f"The chain's head could not be read ({type(err).__name__}).")
    led.transition(intent_id, L.SUBMITTING, now=services.now(), start_block=start_block)
    try:
        call = compose(conn, plan)
        signed = conn.create_signed_extrinsic(
            call=call, keypair=BytesConvertingSigner(signer), era={"period": ERA_PERIOD}
        )
    except Exception as err:
        return fail(
            f"Signing was refused or failed ({type(err).__name__}). "
            f"Nothing was sent."
        )
    try:
        receipt = conn.submit_extrinsic(
            signed, wait_for_inclusion=True, wait_for_finalization=False
        )
    except Exception as err:
        led.transition(
            intent_id,
            L.UNCERTAIN,
            now=services.now(),
            note=f"submission raised {type(err).__name__}; it may or may not have been sent",
        )
        say(
            "The submission failed in a way that does not tell us whether it was sent. It will be "
            "checked against the chain; no new deposit starts until then."
        )
        return {"status": L.UNCERTAIN, "intent_id": intent_id}
    success = getattr(receipt, "is_success", None)
    if success is False:
        return fail(
            f"The chain included it but reported failure: "
            f"{getattr(receipt, 'error_message', None)!r}. No deposit happened; a network fee may "
            f"have been charged."
        )
    if success is not True:
        led.transition(
            intent_id,
            L.UNCERTAIN,
            now=services.now(),
            note="the chain's receipt did not say clearly whether it succeeded; checking the chain",
        )
        say(
            "The chain's receipt was unclear. It will be checked against the chain; "
            "no new deposit starts until then."
        )
        return {"status": L.UNCERTAIN, "intent_id": intent_id}
    block_hash = getattr(receipt, "block_hash", None)
    block_number = None
    try:
        block_number = conn.get_block_number(block_hash) if block_hash is not None else None
    except Exception:
        pass
    led.transition(
        intent_id,
        L.SUBMITTED,
        now=services.now(),
        extrinsic_hash=None
        if getattr(receipt, "extrinsic_hash", None) is None
        else str(receipt.extrinsic_hash),
        block_hash=None if block_hash is None else str(block_hash),
        block_number=block_number,
    )
    say("Submitted. That is not yet proof of credit; checking finality and the chain's own record.")
    try:
        state = reconcile_intent(config, services, led.get(intent_id))
    except Exception as err:  # the deposit is recorded; only the follow-up check failed
        state = led.get(intent_id)["state"]
        say(
            f"The follow-up check could not reach the chain ({type(err).__name__}). The deposit "
            f"is recorded as '{state.replace('_', ' ')}'; run `status` later to check it."
        )
        return {"status": state, "intent_id": intent_id}
    say(f"Status now: {state.replace('_', ' ')}.")
    return {"status": state, "intent_id": intent_id}


def resolve_credit(config, services, intent_prefix, value_rao):
    """Human step: the miner supplies the credited total measured BEFORE a deposit that was
    sent without an earnings figure, so its credit can be tracked. Never guessed by the tool.

    Refused unless the deposit is final and untracked, the number is a whole non-negative rao
    that is not above today's credited total (credit only grows) and not below an earlier
    deposit's starting figure, and the miner types the confirmation. The value is recorded as
    miner-supplied in the ledger.
    """
    _check_chain_identity(config, services)
    say, led = services.say, services.ledger
    if isinstance(value_rao, bool) or not isinstance(value_rao, int) or value_rao < 0:
        raise DepositRefused("The credited total must be a whole number of rao, zero or more.")
    rows = [
        r for r in led.for_miner(config.miner_hotkey) if r["intent_id"].startswith(intent_prefix)
    ]
    if len(rows) != 1:
        raise DepositRefused(f"{len(rows)} deposits match {intent_prefix!r}; give the exact id.")
    row = rows[0]
    if row["state"] != L.FINALIZED:
        raise DepositRefused(
            f"That deposit is '{row['state']}', so there is nothing to resolve. Only a final "
            f"deposit whose credit is not yet tracked can take a figure."
        )
    if row["credited_before_rao"] is not None:
        raise DepositRefused("That deposit already has a credit figure; it cannot be replaced.")
    evidence = services.evidence_provider.read()  # a refusal here stops the step
    current = getattr(evidence, "credited_contributions_rao", None)
    if current is None:
        raise DepositRefused("The evidence has no credited total, so the figure cannot be checked.")
    if value_rao > current:
        raise DepositRefused(
            f"{format_noah(value_rao)} is more than the credited total shown now "
            f"({format_noah(current)}). Credit only grows, so it cannot have been higher before."
        )
    floor = max(
        (
            r["credited_before_rao"]
            for r in led.for_miner(config.miner_hotkey)
            if r["credited_before_rao"] is not None and r["created_at"] <= row["created_at"]
        ),
        default=0,
    )
    if value_rao < floor:
        raise DepositRefused(
            f"{format_noah(value_rao)} is lower than the credited total recorded for an earlier "
            f"deposit ({format_noah(floor)}). Credit only grows."
        )
    say(
        f"You are telling the tool that, just before the {format_noah(row['amount_rao'])} deposit "
        f"{row['intent_id'][:12]} ({row['created_at']}), the credited total was "
        f"{format_noah(value_rao)} ({value_rao} rao).\nThe tool cannot verify this. Too low a "
        f"figure can mark the deposit credited too early; use the published records."
    )
    phrase = f"CONFIRM {row['intent_id'][:12]} {value_rao}"
    if services.ask(f"To confirm, type exactly:  {phrase}\n> ").strip() != phrase:
        say("Not confirmed. Nothing was changed.")
        return {"status": "declined"}
    led.set_credited_before(row["intent_id"], value_rao, now=services.now())
    state = reconcile_intent(config, services, led.get(row["intent_id"]))
    say(f"Recorded as supplied by you. Status now: {state.replace('_', ' ')}.")
    return {"status": state, "intent_id": row["intent_id"]}


def reconcile_open(config, services):
    """Bring every unresolved ledger entry up to date with the chain."""
    _check_chain_identity(config, services)
    for row in services.ledger.for_miner(config.miner_hotkey):
        if row["state"] in (L.APPROVED, L.SUBMITTING, L.SUBMITTED, L.UNCERTAIN, L.FINALIZED):
            try:
                reconcile_intent(config, services, row)
            except Refusal as err:
                services.say(f"Could not check deposit {row['intent_id'][:8]} yet: {err}")
            except Exception as err:
                services.say(
                    f"Could not check deposit {row['intent_id'][:8]} yet "
                    f"({type(err).__name__}); it stays as it was."
                )


@traced("miner", "miner.reconciliation")
def reconcile_intent(config, services, intent):
    """Advance one intent as far as the chain proves. Returns its resulting state."""
    _check_chain_identity(config, services)
    led, conn, now = services.ledger, services.connection, services.now
    intent_id, state = intent["intent_id"], intent["state"]

    def search(from_block, to_block):
        return find_deposit(
            conn,
            ark_coldkey=config.ark_coldkey,
            validator_hotkey=config.validator_hotkey,
            coldkey=config.coldkey,
            miner_hotkey=config.miner_hotkey,
            expected_netuid=config.netuid,
            amount_rao=intent["amount_rao"],
            from_block=from_block,
            to_block=to_block,
        )

    if state == L.APPROVED:
        # Typed approval but never reached "submitting": nothing was ever handed to the chain.
        led.transition(
            intent_id, L.DECLINED, now=now(), note="interrupted before anything was sent"
        )
        return L.DECLINED

    if state in (L.SUBMITTING, L.UNCERTAIN):
        start = intent["start_block"] if intent["start_block"] is not None else 0
        found = search(start, head_height(conn))
        if found is None:
            if finalized_height(conn) >= start + ABANDON_AFTER_BLOCKS:
                led.transition(
                    intent_id,
                    L.ABANDONED,
                    now=now(),
                    note="not found on chain after the transaction's validity window ended",
                )
                return L.ABANDONED
            if state == L.SUBMITTING:
                led.transition(
                    intent_id,
                    L.UNCERTAIN,
                    now=now(),
                    note="interrupted while sending; waiting to see if it lands",
                )
            return L.UNCERTAIN
        led.transition(
            intent_id,
            L.SUBMITTED,
            now=now(),
            block_number=found.deposit.block,
            block_hash=found.deposit.block_hash,
        )
        intent, state = led.get(intent_id), L.SUBMITTED

    if state == L.SUBMITTED:
        block = intent["block_number"]
        if block is None:
            found = search(intent["start_block"] or 0, head_height(conn))
            if found is None:
                return L.SUBMITTED
            block = found.deposit.block
        if finalized_height(conn) < block:
            return L.SUBMITTED  # not final yet; ask again later
        if (
            intent["block_hash"] is not None
            and str(conn.get_block_hash(block)) != intent["block_hash"]
        ):
            led.transition(
                intent_id,
                L.UNCERTAIN,
                now=now(),
                note="the recorded block is no longer on the final chain; re-checking",
            )
            return L.UNCERTAIN
        found = search(block, block)
        if found is None:
            led.transition(
                intent_id,
                L.UNCERTAIN,
                now=now(),
                block_number=block,
                note="the receipt said success but no deposit is visible in that block",
            )
            return L.UNCERTAIN
        if not found.qualifies:
            led.transition(
                intent_id,
                L.UNQUALIFIED,
                now=now(),
                block_number=block,
                note=f"landed but would not be credited: {found.reason}",
            )
            return L.UNQUALIFIED
        led.transition(intent_id, L.FINALIZED, now=now(), block_number=block)
        intent, state = led.get(intent_id), L.FINALIZED

    if state == L.FINALIZED:
        before = intent["credited_before_rao"]
        if before is None:
            return L.FINALIZED  # no earlier credit figure, so credit cannot be confirmed
        evidence = _optional_evidence(services)
        credited = getattr(evidence, "credited_contributions_rao", None)
        if credited is not None and credited >= before + intent["amount_rao"]:
            led.transition(intent_id, L.CREDITED, now=now())
            return L.CREDITED
    return state
