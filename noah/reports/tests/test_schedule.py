import copy
import pytest

from noah.reports.common import ReportError
from noah.reports.schedule import validate_schedule, day_for_time, _resolve, verify_resolution
from noah.reports.verification import CALCULATION
from noah.reports.backing.protocol.preimage import REPORT_POLICY
from noah.reports.backing.keys import timestamp_now_key

ANCHOR = 1794787200


def schedule():
    return dict(schema='noah.report-schedule/1', genesis_hash='11'*32,
        mining_netuid=2, reported_netuid=3, day_anchor_ts=ANCHOR, effective_day=1,
        snapshot_offset_seconds=600, prepare_lead_seconds=600,
        commit_seconds=7200, reveal_seconds=7200,
        eligibility_policy_version=REPORT_POLICY, calculation_version=CALCULATION,
        scoring_release='test-schedule', ark_coldkey='22'*32, ark_validator_hotkey='33'*32)


class Chain:
    def __init__(self, *, shift=0, head=9000, fork=False):
        self.shift, self.head, self.fork = shift, head, fork

    def block_hash(self, n):
        if n == 0:
            return '0x'+'11'*32
        return '0x'+format(n + (100000 if self.fork else 0), '064x')

    def rpc_request(self, method, params):
        if method == 'chain_getBlockHash':
            result = self.block_hash(params[0])
        elif method == 'chain_getFinalizedHead':
            result = self.block_hash(self.head)
        elif method == 'chain_getHeader':
            n = int(params[0],16) - (100000 if self.fork else 0)
            result = {'number': hex(n), 'parentHash': self.block_hash(n-1)}
        elif method == 'state_getStorage':
            assert params[0] == '0x'+timestamp_now_key().hex()
            n = int(params[1],16) - (100000 if self.fork else 0)
            result = '0x'+((ANCHOR+n*12+self.shift)*1000).to_bytes(8,'little').hex()
        else:
            raise AssertionError(method)
        return {'result': result}


def test_same_day_has_same_round_despite_later_process_or_finalized_head():
    a = _resolve(schedule(), [Chain(), Chain(head=9100)], 1)
    b = _resolve(schedule(), [Chain(head=9200), Chain(head=9300)], 1)
    assert a['round'] == b['round']
    assert a['policy'] == b['policy']
    assert a['round']['block_number'] == 7250
    assert a['round']['commit_open_ms'] == (ANCHOR+86400+1200)*1000
    assert a['round']['reveal_close_ms']-a['round']['commit_open_ms'] == 14400000
    assert a['round']['release_prefix'] == validate_schedule(schedule())[:32]
    assert verify_resolution(schedule(), 1, a) == a


def test_boundaries_straddle_slot_and_hashes_agree():
    r = _resolve(schedule(), [Chain(shift=1), Chain(shift=1)], 1)
    proof = r['boundary_proof']; one=proof['readers'][0]
    assert one['predecessor']['timestamp_ms'] < proof['target_ms'] <= one['snapshot']['timestamp_ms']
    assert verify_resolution(schedule(), 1, r) == r
    with pytest.raises(ReportError, match='disagree'):
        _resolve(schedule(), [Chain(), Chain(fork=True)], 1)


def test_future_day_and_before_effective_day_do_not_manufacture_missing_reports():
    with pytest.raises(ReportError, match='not finalized'):
        _resolve(schedule(), [Chain(head=7000)], 1)
    with pytest.raises(ReportError, match='precedes activation'):
        _resolve(schedule(), [Chain()], 0)


def test_late_chain_never_retimes_windows():
    class Delayed(Chain):
        def rpc_request(self, method, params):
            result = super().rpc_request(method, params)
            if method == 'state_getStorage' and int(params[1],16) >= 7250:
                n=int(params[1],16)
                result={'result':'0x'+((ANCHOR+n*12+600)*1000).to_bytes(8,'little').hex()}
            return result
    with pytest.raises(ReportError, match='missed'):
        _resolve(schedule(), [Delayed()], 1)


@pytest.mark.parametrize('mutate', [
    lambda r:r['round'].__setitem__('block_number',7251),
    lambda r:r['round'].__setitem__('commit_open_ms',r['round']['commit_open_ms']+1),
    lambda r:r['policy'].__setitem__('effective_day',0),
    lambda r:r['boundary_proof']['readers'][0]['snapshot'].__setitem__('timestamp_ms',1),
    lambda r:r['boundary_proof']['readers'][0]['receipts'].pop(),
    lambda r:r['boundary_proof']['readers'][0]['receipts'].append({'method':'extra','params':[],'result':1}),
    lambda r:r['boundary_proof']['readers'][0]['receipts'][0].__setitem__('params',[1]),
])
def test_offline_replay_rejects_changed_saved_decision_or_raw_members(mutate):
    r=_resolve(schedule(),[Chain(),Chain()],1);mutate(r)
    with pytest.raises(ReportError):verify_resolution(schedule(),1,r)


def test_changed_config_and_reader_count_refuse():
    r=_resolve(schedule(),[Chain(),Chain()],1)
    changed=schedule();changed['reported_netuid']=4
    with pytest.raises(ReportError):verify_resolution(changed,1,r)
    r['boundary_proof']['readers'].pop()
    with pytest.raises(ReportError):verify_resolution(schedule(),1,r)


@pytest.mark.parametrize('key,value', [('commit_seconds',60),('reveal_seconds',60),
    ('prepare_lead_seconds',0),('snapshot_offset_seconds',80000),('day_anchor_ts',1),
    ('effective_day',True),('mining_netuid',0),('eligibility_policy_version','attributed-deposit-before-b-v1')])
def test_invalid_schedule_refuses(key,value):
    doc=schedule();doc[key]=value
    with pytest.raises(ReportError):validate_schedule(doc)


def test_day_mapping_uses_utc_and_configuration_is_explicit():
    assert day_for_time(schedule(),(ANCHOR+86400)*1000)==1
    assert day_for_time(schedule(),(ANCHOR+172800)*1000-1)==1
    doc=schedule();doc['new_field']='unknown'
    with pytest.raises(ReportError):validate_schedule(doc)
