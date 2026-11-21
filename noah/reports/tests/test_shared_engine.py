"""Reader-separated raw-input sharing; no cached engine or verdict authority."""
import copy
from types import SimpleNamespace
import pytest
from noah.reports.shared_engine import SharedEngineSource, SharedReplaySource
from noah.reports.backing.source import ReplayError
from noah.reports.backing.rpc import Receipt, result_sha256
from noah.reports.budget import ProviderBudget
from noah.reports.common import ReportError
from noah.reports import shared_streams as streams
PIN='0x'+'a'*64


def receipt(method='state_getStorage',params=None,result=None):
    params=['0xab',PIN] if params is None else params
    result={'value':[1]} if result is None else result
    sha,size=result_sha256(result)
    return Receipt(method,params,result,1,0,sha,size,'reader_a')


def live(value=None):
    budget=ProviderBudget();source=SharedEngineSource('ws://unused.invalid','reader_a',PIN,budget)
    source.begin_round()
    def call(method,params):
        row=receipt(method,params,copy.deepcopy(value));source.client.receipts.append(row);return row.result
    source.client.call=call
    return source,budget


def test_first_result_hit_and_replay_are_isolated_and_charged_once():
    source,budget=live()
    first=source.call('state_getStorage',['0xab',PIN]);charged=budget.retained
    first['value'].append(2)
    assert source.call('state_getStorage',['0xab',PIN])=={'value':[1]}
    assert budget.retained==charged>0 and len(source.shared_receipts)==1
    replay=SharedReplaySource([source.shared_receipts[0].to_json()],PIN)
    a=replay.for_round([]);a.call('state_getStorage',['0xab',PIN])['value'].append(3)
    assert replay.for_round([]).call('state_getStorage',['0xab',PIN])=={'value':[1]}


def test_readers_never_share_cache_or_budget_failure():
    a,_=live({'reader':'a'});b,_=live({'reader':'b'})
    assert a.call('state_getStorage',['0xab',PIN])!=b.call('state_getStorage',['0xab',PIN])
    failure=ReportError('original failure')
    def refuse():raise failure
    a.budget.connections=[SimpleNamespace(check_failure=refuse)]
    with pytest.raises(ReportError) as caught:a.call('state_getStorage',['0xab',PIN])
    assert caught.value is failure


@pytest.mark.parametrize('method,params',[('chain_getFinalizedHead',[]),('state_getMetadata',[]),('state_getStorage',['0xab','0x'+'b'*64]),('state_getKeysPaged',['0xab',1000,PIN])])
def test_common_engine_requires_exact_immutable_snapshot(method,params):
    with pytest.raises(ReplayError):SharedReplaySource([receipt(method,params).to_json()],PIN)


def test_missing_conflicting_and_round_misplaced_raw_inputs_refuse():
    row=receipt().to_json();changed=receipt(result={'value':[2]}).to_json()
    with pytest.raises(ReplayError):SharedReplaySource([row,changed],PIN)
    source=SharedReplaySource([row],PIN)
    with pytest.raises(ReplayError):source.for_round([row])
    with pytest.raises(ReplayError):source.for_round([]).call('state_getStorage',['0xcd',PIN])


def test_global_retention_limit_is_not_bypassed():
    source,budget=live();budget.retained=budget.retained_limit
    with pytest.raises(ReportError,match='retained'):source.call('state_getStorage',['0xab',PIN])
    assert not source.shared_receipts


def test_chunk_sequence_and_stream_bounds(tmp_path,monkeypatch):
    monkeypatch.setattr(streams,'CHUNK_BYTES',16)
    rows=[{'n':i} for i in range(5)]
    streams.write(tmp_path,'engine',rows)
    assert len(streams.paths(tmp_path,'engine'))==3 and streams.load(tmp_path,'engine')==rows
    (tmp_path/'engine-0001.jsonl.gz').unlink()
    with pytest.raises(ReportError,match='noncontiguous'):streams.load(tmp_path,'engine')


def test_chunk_count_and_rehashed_expansion_bound(tmp_path,monkeypatch):
    import gzip,json
    monkeypatch.setattr(streams,'CHUNK_BYTES',8);monkeypatch.setattr(streams,'MAX_CHUNKS',1)
    with pytest.raises(ReportError,match='count'):streams.write(tmp_path,'responses',[{'n':1},{'n':2}])
    p=tmp_path/'engine-0000.jsonl.gz';p.write_bytes(gzip.compress(b'{"large":"xxxxxxxx"}\n'))
    with pytest.raises(ReportError,match='expansion'):streams.load(tmp_path,'engine')


def test_events_only_for_requested_mining_netuid_or_unknown_shape():
    from noah.reports.backing.collector import _body_has_commitment
    def block(args):
        return {'extrinsics':[SimpleNamespace(value={'call':{'call_module':'Commitments',
            'call_function':'set_commitment','call_args':args}})]}
    assert not _body_has_commitment(block({'netuid':83}),82)
    assert _body_has_commitment(block({'netuid':82}),82)
    assert _body_has_commitment(block({'netuid':'82'}),82)
    assert _body_has_commitment(block([]),82)
    assert _body_has_commitment(block({'netuid':83}))  # Historical behavior.
    nested={'extrinsics':[SimpleNamespace(value={'call':{'call_args':[{'name':'call','value':
        block({'netuid':82})['extrinsics'][0].value['call']}]}})]}
    assert _body_has_commitment(nested,82)


def test_indexed_alpha_matches_each_original_target_and_refuses_missing_page():
    from noah.reports.backing.reads import Reader
    from noah.reports.tests.fakes import alpha_storage_key
    from noah.reports.backing.keys import hex_encode,alpha_v2_prefix
    keys=[hex_encode(alpha_storage_key(b'h'*32,b'c'*32,82)),
          hex_encode(alpha_storage_key(b'i'*32,b'c'*32,82)),
          hex_encode(alpha_storage_key(b'j'*32,b'd'*32,83))]
    params=[hex_encode(alpha_v2_prefix()),1000,None,PIN]
    keys.sort()
    rows=[receipt('state_getKeysPaged',params,keys).to_json()]
    common=SharedReplaySource(rows,PIN)
    class Plain:
        def call(self,method,actual):assert actual==params;return copy.deepcopy(keys)
    for netuid in (82,83,84):
        old=Reader(Plain(),PIN);new=Reader(common.for_round([]),PIN)
        assert old.discover_coldkeys(netuid)==new.discover_coldkeys(netuid)
        assert (old.page_count,old.position_count)==(new.page_count,new.position_count)
    with pytest.raises(ReplayError):Reader(SharedReplaySource([],PIN).for_round([]),PIN).discover_coldkeys(82)


def test_package3_capacity_is_selected_only_for_compatible_weekly_requests():
    from noah.reports.adjudication import budget_for_request,LIMITS,SHARED_LIMITS
    from noah.reports.tests.test_shared_window import MultiRawChain
    request=MultiRawChain().request()
    budget=budget_for_request(request)
    assert budget.total_limit==budget.retained_limit==768*1024**2
    assert 3590 < budget.remaining() <= 3600
    assert (budget.max_calls,budget.response_limit)==(200000,8*1024**2)
    request.pop('assignments')
    assert budget_for_request(request).total_limit==256*1024**2
    assert (LIMITS.total_bytes,LIMITS.total_expanded_bytes)==(256*1024**2,512*1024**2)
    assert (SHARED_LIMITS.total_bytes,SHARED_LIMITS.total_expanded_bytes)==(384*1024**2,768*1024**2)


@pytest.mark.parametrize('kind',['cycle','order','oversize','foreign'])
def test_shared_alpha_inventory_refuses_nonprogressing_or_malformed_pages(kind,monkeypatch):
    from noah.reports.backing import reads
    from noah.reports.backing.rpc import RunFailure
    from noah.reports.backing.keys import hex_encode,alpha_v2_prefix
    monkeypatch.setattr(reads,'KEY_PAGE_SIZE',2)
    prefix=hex_encode(alpha_v2_prefix())
    pages=iter([[prefix+'01',prefix+'02'],[prefix+'01',prefix+'02']] if kind=='cycle' else
        [[prefix+'02',prefix+'01']] if kind=='order' else
        [[prefix+'01',prefix+'02',prefix+'03']] if kind=='oversize' else [['0xwrong']])
    source=SimpleNamespace(call=lambda *_:next(pages))
    with pytest.raises(RunFailure):reads.alpha_inventory(source,PIN)
