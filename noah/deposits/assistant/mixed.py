"""Budget and receipt rules for the test-only mixed contribution workflow.

This module does not assign scoring credit. Production execution stays disabled
until the human-supplied policy has been reviewed and explicitly adopted.
"""
import configparser
from dataclasses import dataclass
import re

from ...chain.sources import Refusal
from .amounts import DepositRefused
from .proxy_signing import same, uint, unwrap


def refuse(reason):
    raise DepositRefused(reason, 'mixed_deposit')


class NotSubmitted(DepositRefused):
    """A checked boundary refused before invoking the submission RPC."""
    def __init__(self, code):
        if code not in ('quote_expired', 'preparation_check_failed', 'pre_submission_check_failed'):
            code = 'pre_submission_check_failed'
        self.public_code = code
        super().__init__('No transaction was submitted: ' + code + '.', code)


@dataclass(frozen=True)
class MixedSettings:
    foreign_netuid: int
    foreign_ark_hotkey: str
    swap_amount_rao: int | None
    swap_percent_of_budget_bp: int | None
    max_slippage_bp: int
    quote_max_age_blocks: int
    max_fee_rao: int
    runtime_code_hash: str
    spec_version: int
    test_only: bool = True


def load_settings(path):
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path)
    if not parser.has_section('mixed'):
        refuse('convert-deposit requires a mixed section')
    return parse_settings(dict(parser['mixed']))


def parse_settings(raw):
    required = {'test_only', 'foreign_netuid', 'foreign_ark_hotkey',
                'max_slippage_bp', 'quote_max_age_blocks', 'max_fee_rao',
                'runtime_code_hash', 'spec_version'}
    sizes = {'swap_amount_rao', 'swap_percent_of_budget_bp'} & set(raw)
    if not required <= set(raw) or len(sizes) != 1 or set(raw) - required - sizes:
        refuse('mixed settings require the safety fields and exactly one swap sizing option')
    if raw['test_only'] != 'true':
        refuse('production mixed deposits are not supported in this release')
    def number(name, minimum, maximum):
        text = raw[name]
        if not re.fullmatch(r'0|[1-9][0-9]*', text):
            refuse(name + ' must be a nonnegative whole integer')
        value = int(text)
        if not minimum <= value <= maximum:
            refuse(name + ' is outside the supported range')
        return value
    from ...chain.ss58 import public_key_of
    try:
        public_key_of(raw['foreign_ark_hotkey'])
    except (Exception, Refusal):
        refuse('mixed foreign_ark_hotkey must be a valid public address')
    if not re.fullmatch(r'0x[0-9a-f]{64}', raw['runtime_code_hash']):
        refuse('mixed runtime_code_hash must be the reviewed runtime hash')
    return MixedSettings(
        foreign_netuid=number('foreign_netuid', 1, 65535),
        foreign_ark_hotkey=raw['foreign_ark_hotkey'],
        swap_amount_rao=number('swap_amount_rao', 1, 2**64-1) if 'swap_amount_rao' in sizes else None,
        swap_percent_of_budget_bp=number('swap_percent_of_budget_bp', 1, 9999) if 'swap_percent_of_budget_bp' in sizes else None,
        max_slippage_bp=number('max_slippage_bp', 0, 9999),
        quote_max_age_blocks=number('quote_max_age_blocks', 1, 100),
        max_fee_rao=number('max_fee_rao', 1, 2**64-1),
        runtime_code_hash=raw['runtime_code_hash'],
        spec_version=number('spec_version', 1, 2**32-1),
    )


def from_mapping(raw):
    """Validate typed settings from deployment configuration using the INI rules."""
    if not isinstance(raw, dict):
        refuse('mixed configuration must be an object')
    numeric = {'foreign_netuid','swap_amount_rao','swap_percent_of_budget_bp',
               'max_slippage_bp','quote_max_age_blocks','max_fee_rao','spec_version'}
    for key in numeric & set(raw):
        if type(raw[key]) is not int:refuse(key+' must be a whole integer')
    if raw.get('test_only') is not True:
        refuse('mixed configuration requires explicit test_only=true')
    return parse_settings({k: ('true' if v is True else str(v)) for k,v in raw.items()})


def test_gate(config, settings):
    from ...core.chain import MAINNET
    public_testnet = '0x8f9cf856bf558a14440e75569c9e58594757048d7b3a84b5d25f6bd978263105'
    if (settings.test_only is not True or config.network not in ('testnet', 'private-test')
            or not re.fullmatch(r'0x[0-9a-f]{64}', config.genesis_hash or '')
            or config.genesis_hash in (MAINNET, public_testnet)):
        refuse('mixed execution requires an explicitly pinned disposable test chain')
    if config.mode != 'fixed':
        refuse('mixed execution currently requires a fixed budget; percentage and maintenance coverage await shared period accounting')
    if config.stake_position != 'miner':
        refuse('mixed deposits require the attributed miner stake position')
    if settings.foreign_netuid == config.netuid:
        refuse('mixed destination must be a different subnet')
    # Proxy deposits are supported separately. This test workflow must not infer
    # authorization for a new combination of conversion and foreign transfer.
    from .proxy_signing import is_proxy
    if is_proxy(config):
        refuse('combined mixed/proxy execution is not yet supported; use the fixed proxy deposit command')


def allocation(total_rao, settings):
    """Choose the contribution first; the swap consumes part of that same budget."""
    uint(total_rao, 'contribution budget', 1)
    if (settings.swap_amount_rao is None) == (settings.swap_percent_of_budget_bp is None):
        refuse('select exactly one swap sizing mode')
    swap = settings.swap_amount_rao
    if swap is None:
        uint(settings.swap_percent_of_budget_bp, 'swap_percent_of_budget_bp', 1)
        if settings.swap_percent_of_budget_bp >= 10000:
            refuse('swap percentage must leave a NOAH deposit')
        swap = total_rao * settings.swap_percent_of_budget_bp // 10000
    uint(swap, 'swap budget', 1)
    if swap >= total_rao:
        refuse('swap budget must leave a positive NOAH deposit')
    return {'contribution_budget_rao': total_rao, 'swap_budget_rao': swap,
            'noah_deposit_rao': total_rao - swap}


def conversion_result(events, *, coldkey, hotkey, origin_netuid, destination_netuid,
                      requested_rao, extrinsic_index):
    """Decode actual alpha from exact swap events, never from prices or balances."""
    groups = {'StakeRemoved': [], 'StakeAdded': [], 'StakeSwapped': []}
    system = set()
    for raw in events:
        raw = unwrap(raw)
        if raw.get('extrinsic_idx') != extrinsic_index:
            continue
        event = raw.get('event', raw)
        module, name = event.get('module_id'), event.get('event_id')
        if module == 'System':
            system.add(name)
        if module == 'SubtensorModule' and name in groups:
            groups[name].append(event.get('attributes'))
    if 'ExtrinsicSuccess' not in system or 'ExtrinsicFailed' in system:
        refuse('conversion does not have exact successful dispatch evidence')
    if any(len(rows) != 1 for rows in groups.values()):
        refuse('conversion alpha movements are missing or ambiguous')
    removed, added, swapped = (groups[n][0] for n in groups)
    for movement, netuid in ((removed, origin_netuid), (added, destination_netuid)):
        if (not isinstance(movement, (list, tuple)) or len(movement) != 6
                or not same(movement[0], coldkey) or not same(movement[1], hotkey)
                or type(movement[4]) is not int or movement[4] != netuid):
            refuse('conversion stake event does not match the configured position')
        uint(movement[3], 'observed alpha', 1)
    if (not isinstance(swapped, (list, tuple)) or len(swapped) != 5
            or not same(swapped[0], coldkey) or not same(swapped[1], hotkey)
            or type(swapped[2]) is not int or type(swapped[3]) is not int
            or tuple(swapped[2:4]) != (origin_netuid, destination_netuid)):
        refuse('conversion summary does not match the requested subnet pair')
    # v478 StakeRemoved reports net swap input and alpha fee separately.
    # Both debit the source stake; StakeAdded's fee is instead denominated in TAO.
    source_fee = uint(removed[5], 'source alpha fee')
    destination_fee = uint(added[5], 'destination TAO fee')
    gross = removed[3] + source_fee
    if gross != requested_rao:
        refuse('conversion consumed an unexpected NOAH amount; downstream deposits remain blocked')
    return {'noah_spent_rao': gross, 'foreign_received_rao': added[3],
            'source_swap_alpha_rao': removed[3], 'source_alpha_fee_rao': source_fee,
            'destination_tao_fee_rao': destination_fee}


def delivered_amounts(total_rao, result):
    """Only the exact finalized conversion result determines the two deposits."""
    spent = uint(result['noah_spent_rao'], 'actual NOAH spent', 1)
    foreign = uint(result['foreign_received_rao'], 'actual foreign alpha received', 1)
    uint(total_rao, 'contribution budget', 1)
    if spent >= total_rao:
        refuse('actual conversion exhausted the NOAH contribution budget')
    return {'noah_rao': total_rao - spent, 'foreign_rao': foreign}


def allocation_from_earnings(*, earnings_rao, contribution_bp, swap_of_earnings_bp,
                             earnings_snapshot_id, swap_snapshot_id):
    """Provider boundary; both percentages refer to the SAME earnings observation.

    This is arithmetic, not an implementation of production earnings eligibility.
    A caller must obtain its observation under the adopted policy first.
    """
    uint(earnings_rao,'eligible earnings',1)
    for value in (contribution_bp,swap_of_earnings_bp):
        uint(value,'earnings percentage',1)
        if value>10000:refuse('earnings percentage exceeds 100%')
    if (not isinstance(earnings_snapshot_id,str) or not earnings_snapshot_id
            or earnings_snapshot_id!=swap_snapshot_id):
        refuse('contribution and swap must use the same identified earnings snapshot')
    total=earnings_rao*contribution_bp//10000
    swap=earnings_rao*swap_of_earnings_bp//10000
    if not 0<swap<total:refuse('rounded earnings allocation must leave both deposit legs positive')
    return {'contribution_budget_rao':total,'swap_budget_rao':swap,
            'noah_deposit_rao':total-swap,'earnings_snapshot_id':earnings_snapshot_id}
