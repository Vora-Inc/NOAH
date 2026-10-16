"""Shared validation of bounded stake conversion settings."""
import copy

DEFAULT = {
    'enabled': False, 'destination_netuid': None, 'destination_hotkey': None,
    'amount_mode': 'fixed', 'amount_rao': None, 'percent_bp': None,
    'max_amount_rao': None, 'reserve_rao': None, 'max_slippage_bp': None,
    'quote_max_age_blocks': None, 'allow_partial': False,
}

def integer(value, name, low=0, high=2**64 - 2):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{name} must be an integer in [{low}, {high}]')
    return value


def validate(raw=None):
    supplied = {} if raw is None else raw
    if not isinstance(supplied, dict) or set(supplied) - set(DEFAULT):
        raise ValueError('unknown conversion field or non-object conversion')
    conversion = copy.deepcopy(DEFAULT)
    conversion.update(supplied)
    if type(conversion['enabled']) is not bool or conversion['allow_partial'] is not False:
        raise ValueError('enabled must be boolean; partial conversions are unsupported')
    if conversion['amount_mode'] not in ('fixed', 'percent_available'):
        raise ValueError('amount_mode must be fixed or percent_available')
    ranges = {'destination_netuid': (1, 65535), 'amount_rao': (1, 2**64 - 2),
              'percent_bp': (1, 10000), 'max_amount_rao': (1, 2**64 - 2),
              'reserve_rao': (0, 2**64 - 2), 'max_slippage_bp': (0, 9999),
              'quote_max_age_blocks': (1, 100)}
    for key, bounds in ranges.items():
        if conversion[key] is not None:
            integer(conversion[key], key, *bounds)
    if conversion['destination_hotkey'] is not None:
        value = conversion['destination_hotkey']
        if not isinstance(value, str) or not value or not value.isalnum():
            raise ValueError('destination_hotkey must be a chain address')
    if conversion['enabled']:
        required = ['destination_netuid', 'destination_hotkey', 'max_amount_rao',
                    'reserve_rao', 'max_slippage_bp', 'quote_max_age_blocks']
        sized, other = ('amount_rao', 'percent_bp') if conversion['amount_mode'] == 'fixed' else ('percent_bp', 'amount_rao')
        if any(conversion[key] is None for key in required + [sized]) or conversion[other] is not None:
            raise ValueError('enabled conversion requires complete settings and exactly one sizing mode')
        if sized == 'amount_rao' and conversion[sized] > conversion['max_amount_rao']:
            raise ValueError('conversion amount exceeds max_amount_rao')
    return conversion


def conversion_amount(config, settings, available, reserved_deposit=0):
    integer(available, 'available')
    integer(reserved_deposit, 'reserved deposit')
    reserve = max(config.reserve_rao, settings['reserve_rao'])
    remaining = max(0, available - reserve - reserved_deposit)
    amount = settings['amount_rao'] if settings['amount_mode'] == 'fixed' else min(
        remaining * settings['percent_bp'] // 10000, settings['max_amount_rao'])
    if amount <= 0 or amount > remaining or amount > settings['max_amount_rao']:
        raise ValueError('conversion amount exceeds available stake/reserve/cap or rounds to zero')
    return amount
