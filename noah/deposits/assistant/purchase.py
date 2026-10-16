"""Fixed free-TAO purchase inputs; no earnings, allocation or scoring policy."""
import configparser
from dataclasses import asdict, dataclass
import re

from ...chain.sources import Refusal
from .mixed import refuse, test_gate as mixed_gate
from .proxy_signing import same, uint, unwrap


@dataclass(frozen=True)
class PurchaseSettings:
    foreign_netuid: int
    foreign_ark_hotkey: str
    max_tao_rao: int
    quote_max_age_blocks: int
    max_fee_rao: int
    runtime_code_hash: str
    spec_version: int
    max_slippage_bp: int = 100
    test_only: bool = True
    plan_id: str | None = None


def parse_settings(raw):
    required = {'test_only', 'foreign_netuid', 'foreign_ark_hotkey', 'max_tao_rao',
                'quote_max_age_blocks', 'max_fee_rao', 'runtime_code_hash', 'spec_version'}
    if not required <= set(raw) or set(raw) - required - {'max_slippage_bp', 'plan_id'}:
        refuse('purchase requires explicit fixed amounts and reviewed runtime safety settings')
    if raw['test_only'] != 'true':
        refuse('purchase execution is restricted to private test chains pending live proof')
    def number(name, low, high, default=None):
        value = raw.get(name, default)
        if not isinstance(value, str) or not re.fullmatch(r'0|[1-9][0-9]*', value):
            refuse(name + ' must be a whole integer')
        result = int(value)
        if not low <= result <= high:refuse(name + ' is outside the supported range')
        return result
    from ...chain.ss58 import public_key_of
    try:public_key_of(raw['foreign_ark_hotkey'])
    except (Exception, Refusal):refuse('purchase foreign_ark_hotkey must be a public address')
    if not re.fullmatch(r'0x[0-9a-f]{64}', raw['runtime_code_hash']):
        refuse('purchase requires the reviewed runtime code hash')
    plan_id=raw.get('plan_id')
    if plan_id is not None and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,95}',plan_id):
        refuse('purchase plan_id must be a stable safe identifier')
    return PurchaseSettings(
        number('foreign_netuid', 0, 65535), raw['foreign_ark_hotkey'],
        number('max_tao_rao', 1, 2**64-1), number('quote_max_age_blocks', 1, 100),
        number('max_fee_rao', 1, 2**64-1), raw['runtime_code_hash'],
        number('spec_version', 1, 2**32-1), number('max_slippage_bp', 0, 100, '100'), plan_id=plan_id)


def load_settings(path):
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path)
    if not parser.has_section('purchase'):refuse('purchase-deposit requires a purchase section')
    if parser.has_section('mixed'):refuse('select purchase or legacy mixed settings, not both')
    return parse_settings(dict(parser['purchase']))


def from_mapping(raw):
    if not isinstance(raw, dict):refuse('purchase settings must be an object')
    numeric = {'foreign_netuid','max_tao_rao','quote_max_age_blocks','max_fee_rao',
               'spec_version','max_slippage_bp'}
    for name in numeric & raw.keys():
        if type(raw[name]) is not int:refuse(name + ' must be a whole integer')
    if raw.get('test_only') is not True:refuse('purchase requires test_only=true')
    return parse_settings({k:'true' if v is True else str(v) for k,v in raw.items() if v is not None})


def test_gate(config, settings):
    # The existing private-chain, direct-owner, miner-position and fixed-input
    # restrictions apply. No proxy-type escalation or root inference is allowed.
    from_mapping(asdict(settings))
    from dataclasses import replace
    from .proxy_signing import is_proxy, signing
    mixed_gate(replace(config,signing={'mode':'direct'}), settings)
    if is_proxy(config):
        if signing(config).get('purchase_proxy_type')!='Staking' or not settings.plan_id:
            refuse('proxy purchase requires explicit Staking delegation and a trusted fixed plan_id')
    if settings.foreign_netuid == 0:
        from .purchase_root import root_gate
        root_gate(config, settings)
    uint(config.amount_rao, 'fixed NOAH amount')
    if config.cap_rao is not None and config.amount_rao > config.cap_rao:
        refuse('fixed NOAH amount exceeds its configured cap')


def plan(config, settings):
    test_gate(config, settings)
    return {'noah_deposit_rao': config.amount_rao, 'max_tao_rao': settings.max_tao_rao,
            'foreign_netuid': settings.foreign_netuid, 'funding_source': 'free_tao',
            **({'plan_id': settings.plan_id} if settings.plan_id else {})}


def purchase_result(events, *, coldkey, hotkey, netuid, requested_rao, extrinsic_index):
    """Require exact finalized add-stake evidence, retaining global event ordinals.

    This parser deliberately accepts only the reviewed six-field StakeAdded
    shape: owner, hotkey, gross TAO moved, actual alpha, subnet, TAO swap fee.
    A changed shape or amount remains unresolved; estimated output is never used.
    """
    own = [(i, unwrap(e)) for i,e in enumerate(events)
           if unwrap(e).get('extrinsic_idx') == extrinsic_index]
    system = [e.get('event', e).get('event_id') for _,e in own
              if e.get('event', e).get('module_id') == 'System']
    if system.count('ExtrinsicSuccess') != 1 or 'ExtrinsicFailed' in system:
        refuse('purchase requires exact successful dispatch evidence')
    movements = [(i,e.get('event',e)) for i,e in own
                  if e.get('event',e).get('module_id') == 'SubtensorModule'
                  and e.get('event',e).get('event_id','').startswith('Stake')]
    if len(movements) != 1 or movements[0][1].get('event_id') != 'StakeAdded':
        refuse('purchase stake movements are missing or ambiguous')
    ordinal, event = movements[0]
    a = event.get('attributes')
    if (not isinstance(a, (list,tuple)) or len(a) != 6 or not same(a[0],coldkey)
            or not same(a[1],hotkey) or type(a[4]) is not int or a[4] != netuid):
        refuse('purchase receipt differs from the intended owner, miner or asset')
    gross = uint(a[2], 'purchase gross TAO', 1)
    actual = uint(a[3], 'actual acquired alpha', 1)
    fee = uint(a[5], 'purchase TAO swap fee')
    if gross != requested_rao or fee > gross:
        refuse('purchase TAO debit differs from the exact approved input; transfers remain blocked')
    # The runtime may refund unconsumed TAO. StakeAdded reports gross moved,
    # not net expenditure, and its fee is already included in that gross input.
    result = {'gross_tao_moved_rao': gross, 'foreign_received_rao': actual,
              'swap_fee_tao_rao': fee, 'event_ordinal': ordinal, 'event': event}
    if netuid == 0:
        from .purchase_root import root_result
        return root_result(result, requested_rao)
    return result
