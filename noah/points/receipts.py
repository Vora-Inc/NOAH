"""Strict public receipt disposition rows shared by fixture/output validation."""
import re

REASONS = {'reference-subnet-absent', 'reference-token-incarnation-differs',
           'native-reference-price-zero', 'foreign-reference-price-zero'}
FIELDS = {'receipt_id', 'coldkey', 'origin_hotkey', 'destination_hotkey', 'block',
          'block_hash', 'day', 'event_ordinal', 'extrinsic_index', 'token_netuid',
          'amount_rao', 'status', 'reason', 'credit_rao', 'reference_proof'}


def validate(rows, *, ema=False):
    if not isinstance(rows, list):
        raise ValueError('receipt dispositions must be a list')
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != FIELDS:
            raise ValueError('unexpected receipt disposition fields')
        for name in ('block', 'day', 'event_ordinal', 'extrinsic_index', 'token_netuid'):
            if type(row[name]) is not int or not 0 <= row[name] <= 2**53 - 1:
                raise ValueError('invalid receipt integer')
        for name in ('coldkey', 'origin_hotkey', 'destination_hotkey'):
            if not isinstance(row[name], str) or not re.fullmatch('[1-9A-HJ-NP-Za-km-z]{47,49}', row[name]):
                raise ValueError('invalid receipt identity')
        if not isinstance(row['block_hash'], str) or not re.fullmatch('0x[0-9a-f]{64}', row['block_hash']):
            raise ValueError('invalid receipt block hash')
        if not isinstance(row['receipt_id'], str) or not re.fullmatch('0x[0-9a-f]{64}/' + row['block_hash'] + '/' + str(row['event_ordinal']), row['receipt_id']):
            raise ValueError('invalid canonical receipt identity')
        if row['receipt_id'] in seen:
            raise ValueError('duplicate receipt disposition')
        seen.add(row['receipt_id'])
        for name in ('amount_rao', 'credit_rao'):
            if not isinstance(row[name], str) or not re.fullmatch('0|[1-9][0-9]*', row[name]):
                raise ValueError('noncanonical receipt amount')
        if int(row['amount_rao']) <= 0:
            raise ValueError('empty receipt')
        if row['status'] == 'accepted':
            if row['reason'] not in (('daily-reference', 'ema-spot-reference') if ema else ('daily-reference',)):
                raise ValueError('invalid accepted disposition')
        elif row['status'] == 'unsupported':
            if row['reason'] not in REASONS or row['credit_rao'] != '0':
                raise ValueError('unsupported receipt must have zero credit and proof reason')
        else:
            raise ValueError('pending receipt cannot settle in completed output')
        proof = row['reference_proof']
        if not isinstance(proof, dict) or set(proof) != {'path', 'sha256'} or not isinstance(proof['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', proof['sha256']):
            raise ValueError('invalid reference proof pin')
        if proof['path'] != 'points-reference-proofs/' + proof['sha256'] + '.json':
            raise ValueError('reference proof path differs')
    return rows


def from_evidence(evidence, *, ema=False):
    result = []
    seen = {}
    for row in evidence:
        if 'receipt' not in row:
            continue  # Frozen historical values remain in the immutable baseline.
        receipt = row['receipt']
        value = dict(receipt_id=row['receipt_id'], coldkey=receipt['coldkey'],
                     origin_hotkey=receipt['origin_hotkey'], destination_hotkey=receipt['hotkey'],
                     block=row['block'], block_hash=row['block_hash'], day=receipt['day'],
                     event_ordinal=receipt['event_ordinal'], extrinsic_index=receipt['extrinsic_index'],
                     token_netuid=row['token_netuid'], amount_rao=str(row['amount_rao']),
                     status=row['status'], reason=row['reason'], credit_rao=str(row['credit_rao']),
                     reference_proof=row['reference_proof'])
        if value['receipt_id'] in seen:
            if seen[value['receipt_id']] != value:
                raise ValueError('receipt disposition changed during fixture construction')
            continue
        seen[value['receipt_id']] = value
        result.append(value)
    return validate(sorted(result, key=lambda row: (row['block'], row['event_ordinal'])), ema=ema)


def reconcile_fixture(raw, rows):
    """Exact prospective aggregate binding, independently repeated in TypeScript."""
    hotkeys = raw.get('pointsHotkeys')
    if not isinstance(hotkeys, dict):
        raise ValueError('points fixture requires hotkey lineage map')
    owners = raw.get('pointsOwners')
    if not isinstance(owners, dict) or set(owners) != set(hotkeys) or any(not isinstance(v,str) or not re.fullmatch('[1-9A-HJ-NP-Za-km-z]{47,49}',v) for v in owners.values()):
        raise ValueError('points owner roster differs')
    active, vacated = {}, {}
    miners = raw['miners']
    expected_uids = {str(row['uid']) for row in miners if row['uid'] is not None}
    if (not expected_uids <= set(hotkeys)
            or any(not isinstance(k, str) or not re.fullmatch('0|[1-9][0-9]*', k) or int(k) > 2**53 - 1 for k in hotkeys)
            or any(not isinstance(v, str) or not re.fullmatch('[1-9A-HJ-NP-Za-km-z]{47,49}', v) for v in hotkeys.values())
            or len(set(hotkeys.values())) != len(hotkeys)):
        raise ValueError('points hotkey roster differs from fixture')
    for index, row in enumerate(miners):
        if row['uid'] is None:
            if row['coldkey'] in vacated:
                raise ValueError('ambiguous vacated wallet row')
            vacated[row['coldkey']] = index
        else:
            hotkey = hotkeys[str(row['uid'])]
            if owners[str(row['uid'])] != row['coldkey']:
                raise ValueError('current fixture owner differs from roster')
            if not isinstance(hotkey, str) or not re.fullmatch('[1-9A-HJ-NP-Za-km-z]{47,49}', hotkey):
                raise ValueError('invalid points hotkey')
            active[(row['coldkey'], hotkey)] = index
    cutoff = raw.get('pointsMigration', {}).get('cutoffDay', int(raw['evalDay']))
    totals = {}
    for receipt in rows:
        index = active.get((receipt['coldkey'], receipt['origin_hotkey']))
        if index is None:
            # A currently occupied hotkey cannot be reassigned to a null wallet.
            if any(hotkey == receipt['origin_hotkey'] and owners[uid] == receipt['coldkey'] for uid,hotkey in hotkeys.items()):
                raise ValueError('excluded current receipt cannot move into a vacated row')
            index = vacated.get(receipt['coldkey'])
        if index is None or not cutoff < receipt['day'] <= int(raw['evalDay']):
            raise ValueError('receipt has no prospective financial row')
        key = index, receipt['day']
        if str(receipt['day']) not in miners[index]['days']:
            raise ValueError('receipt day missing from financial row')
        totals[key] = totals.get(key, 0) + int(receipt['credit_rao'])
    for index, row in enumerate(miners):
        for day, value in row['days'].items():
            if int(day) > cutoff and int(value['foreignDepositNoahEquivRao']) != totals.get((index, int(day)), 0):
                raise ValueError('prospective foreign total differs from exact receipt dispositions')
    return True
