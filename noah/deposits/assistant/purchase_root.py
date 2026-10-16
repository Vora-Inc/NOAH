"""Read-only guards for fixed root purchases on the reviewed runtime 478 path.

These checks supplement the normal purchase adapter's runtime, funds, delegation,
collateral and exact transaction checks. They do not simulate every dispatch
failure (for example basket migration, recipient capacity or a state race).
No key loading, signing, arbitrary calls or transaction submission lives here.
"""
from dataclasses import replace
import re

from ...chain.sources import Refusal
from ...chain.ss58 import public_key_of
from .amounts import DepositRefused
from .mixed import refuse, test_gate as private_gate
from .proxy_signing import same, uint, unwrap

ROOT_PRICE_RAO = 1_000_000_000
REVIEWED_SPEC_VERSION = 478


def root_gate(config, settings):
    """Retain private-test/fixed/miner-identity gates without inferring proxies."""
    private_gate(replace(config, signing={'mode': 'direct'}), settings)
    if type(settings.foreign_netuid) is not int or settings.foreign_netuid != 0:
        refuse('root guard requires explicit subnet zero')
    if type(settings.spec_version) is not int or settings.spec_version != REVIEWED_SPEC_VERSION:
        refuse('root purchase requires the reviewed runtime version')
    if not isinstance(settings.runtime_code_hash, str) or not re.fullmatch(
            r'0x[0-9a-f]{64}', settings.runtime_code_hash):
        refuse('root purchase requires an explicit reviewed runtime code hash')
    if not 0 <= uint(settings.max_slippage_bp, 'slippage bound') <= 100:
        refuse('root purchase slippage exceeds the supported bound')
    uint(settings.max_tao_rao, 'fixed root purchase cap', 1)
    for address in (config.coldkey, config.miner_hotkey, config.ark_coldkey,
                    settings.foreign_ark_hotkey):
        try:
            public_key_of(address)
        except (Exception, Refusal):
            refuse('root purchase requires explicit valid owner, miner and recipient addresses')
    if same(config.coldkey, config.ark_coldkey):
        refuse('root delivery must transfer ownership to the configured recipient')


def root_quote(price, simulation, requested_rao):
    """Validate a runtime quote; root is exact 1:1, including zero swap fees."""
    amount = uint(requested_rao, 'fixed root purchase cap', 1)
    if uint(unwrap(price), 'root price', 1) != ROOT_PRICE_RAO:
        refuse('root price is not the reviewed one-to-one price')
    simulation = unwrap(simulation)
    expected = {'tao_amount': amount, 'alpha_amount': amount, 'tao_fee': 0,
                'alpha_fee': 0, 'tao_slippage': 0, 'alpha_slippage': 0}
    if not isinstance(simulation, dict) or set(simulation) != set(expected):
        refuse('root simulation has an unknown shape')
    if any(uint(simulation[k], 'root simulation ' + k) != v for k, v in expected.items()):
        refuse('root simulation differs from exact one-to-one acquisition')
    return {'limit_price': ROOT_PRICE_RAO, 'estimated_output_rao': amount}


def root_result(result, requested_rao):
    """Additional root invariant after the ordinary exact-event receipt parser."""
    amount = uint(requested_rao, 'fixed root purchase cap', 1)
    expected = {'gross_tao_moved_rao': amount, 'foreign_received_rao': amount,
                'swap_fee_tao_rao': 0}
    if not isinstance(result, dict) or not expected.keys() <= result.keys():
        refuse('root acquisition receipt is incomplete')
    if any(uint(result[k], 'root receipt ' + k) != v for k, v in expected.items()):
        refuse('root acquisition receipt differs from the exact approved input')
    return result


def root_preflight(connection, config, settings, *, action='purchase', amount_rao=None):
    """Check both finalized and best heads before purchase or root delivery.

    Call again at signing/submission boundaries. Nonzero hold intervals are
    unsupported; this helper never invents an implicit wait or sells NOAH.
    Initial support also refuses observed conviction lock rows for either owner.
    Absent Owner storage must not be mistaken for its default decoded address.
    The adapter must separately check available stake and all transaction fees.
    """
    root_gate(config, settings)
    if action not in ('purchase', 'deposit-foreign'):
        refuse('root preflight only accepts purchase or root delivery')
    amount = settings.max_tao_rao if amount_rao is None else uint(amount_rao, 'root amount', 1)
    if amount != settings.max_tao_rao:
        refuse('root amount differs from the immutable purchase cap')
    try:
        if str(connection.get_block_hash(0)) != config.genesis_hash:
            refuse('root chain genesis differs from configuration')
        heads = tuple(dict.fromkeys((str(connection.get_chain_finalised_head()),
                                     str(connection.get_chain_head()))))
        facts = []
        for head in heads:
            version = connection.rpc_request('state_getRuntimeVersion', [head])['result']
            code = connection.rpc_request('state_getStorageHash', ['0x3a636f6465', head])['result']
            if (type(version.get('specVersion')) is not int
                    or version['specVersion'] != REVIEWED_SPEC_VERSION
                    or code != settings.runtime_code_hash):
                refuse('root runtime differs from the configured reviewed runtime')
            def read(name, params=None):
                return unwrap(connection.query('SubtensorModule', name, params or [], block_hash=head))
            if uint(read('RootStakeUnlockInterval'), 'root unlock interval') != 0:
                refuse('root purchase requires zero unlock interval; durable waiting is unsupported')
            if uint(read('SubnetMechanism', [0]), 'root mechanism') != 0:
                refuse('root mechanism is not the reviewed stable mechanism')
            for name in ('NetworksAdded', 'SubtokenEnabled', 'TransferToggle'):
                if read(name, [0]) is not True:
                    refuse('root network or transfers are unavailable')
            for hotkey in (config.miner_hotkey, settings.foreign_ark_hotkey):
                key = connection.create_storage_key('SubtensorModule', 'Owner', [hotkey], block_hash=head)
                if connection.get_storage_by_key(head, key.to_hex()) is None:
                    refuse('root source or recipient hotkey account does not exist')
                public_key_of(read('Owner', [hotkey]))
            if not same(read('Owner', [config.miner_hotkey]), config.coldkey):
                refuse('configured owner does not own the original miner hotkey')
            # Checking the complete owner/root prefix also catches sibling-hotkey locks.
            for owner in (config.coldkey, config.ark_coldkey):
                rows = connection.query_map('SubtensorModule', 'Lock', [owner, 0], block_hash=head)
                if next(iter(rows), None) is not None:
                    refuse('root delivery with conviction lock rows is not supported')
            minimum_stake = uint(unwrap(connection.get_constant(
                'SubtensorModule', 'InitialMinStake', block_hash=head)), 'minimum root stake')
            minimum_transfer = uint(unwrap(connection.get_constant(
                'SubtensorModule', 'InitialMinTransfer', block_hash=head)), 'minimum root transfer')
            if amount < minimum_transfer or (action == 'purchase' and amount < minimum_stake):
                refuse('root amount is below the runtime purchase or transfer minimum')
            price = connection.runtime_call('SwapRuntimeApi', 'current_alpha_price', [0], block_hash=head)
            simulation = connection.runtime_call('SwapRuntimeApi', 'sim_swap_tao_for_alpha',
                                                 [0, amount], block_hash=head)
            quote = root_quote(price, simulation, amount)
            facts.append(dict(block_hash=head, root_unlock_interval=0, minimum_stake_rao=minimum_stake,
                              minimum_transfer_rao=minimum_transfer, **quote))
        return {'action': action, 'amount_rao': amount, 'heads': facts,
                'complete_dispatch_simulation': False}
    except DepositRefused:
        raise
    except (Exception, Refusal):
        # Do not leak endpoint details or SDK exception payloads to callers.
        refuse('root preflight refused or could not read all required chain facts')
