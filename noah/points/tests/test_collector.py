"""Exact recent UTC references do not require ancient genesis state."""

import pytest

from noah.chain.live import ChainUnavailable
from noah.chain.sources import Refusal, SourceDisagreement
from noah.points.collector import Collector
from noah.points.ledger import R
from noah.points.pricing import daily_average_credit


class RecentChain:
    receipt = 1_000_000
    end = 2_000 * 86400
    seconds = 3600

    def __init__(self, *, offset=3600, readable=100, missing=None):
        self.offset, self.readable, self.missing = offset, readable, missing
        self.time_reads, self.price_reads = [], []
        self.fork = ''

    def genesis_hash(self):
        return 'genesis'

    def head_block(self):
        return self.receipt

    def block_hash(self, block):
        assert 1 <= block <= self.receipt, 'never read past finalized receipt'
        return f'{self.fork}{block:064x}'

    def block_timestamp(self, block):
        self.time_reads.append(block)
        if block < self.receipt - self.readable or block == self.missing:
            raise ChainUnavailable('fixture: old state pruned')
        return self.end + self.offset + (block-self.receipt)*self.seconds

    def alpha_price_at(self, block, *, netuid, expected_block_hash):
        assert expected_block_hash == self.block_hash(block)
        self.price_reads.append((block, netuid))
        return R if netuid == 2 else 2*R


@pytest.mark.parametrize('offset', [0, 1, 1800, 3600, 86399])
def test_pruned_ancient_state_with_complete_recent_day(tmp_path, offset):
    chain = RecentChain(offset=offset)
    rows = Collector(chain, tmp_path, 2).prior_day(chain.receipt, 3)
    assert rows[0].start == chain.end-86400 and rows[-1].end == chain.end
    assert sum(row.end-row.start for row in rows) == 86400
    assert all(a.end == b.start for a,b in zip(rows, rows[1:]))
    assert daily_average_credit(R, 3, chain.end+offset, rows, native_netuid=2) == 2*R
    assert min(chain.time_reads) > chain.receipt-100
    assert all(block < chain.receipt or offset > 0 for block,_ in chain.price_reads)


def test_uses_actual_boundary_predecessor_price_and_finalized_successor(tmp_path):
    chain = RecentChain(offset=1800)
    rows = Collector(chain, tmp_path, 2).prior_day(chain.receipt, 3)
    assert len(rows) == 25
    assert rows[0].end-rows[0].start == 1800
    assert rows[-1].end-rows[-1].start == 1800
    assert chain.price_reads[0][0] == chain.receipt-25
    assert chain.price_reads[-1][0] == chain.receipt-1
    assert chain.receipt in chain.time_reads  # exact day-end successor, finalized


@pytest.mark.parametrize('readable,missing', [(10,None), (100,999990)])
def test_missing_required_window_refuses_without_cache(tmp_path, readable, missing):
    chain = RecentChain(readable=readable,missing=missing)
    with pytest.raises(ChainUnavailable):
        Collector(chain, tmp_path, 2).prior_day(chain.receipt, 3)
    assert not list(tmp_path.glob('*.json'))


def test_unreadable_search_margin_is_not_reinterpreted_as_a_time_bound(tmp_path):
    # All exact day intervals exist (receipt-25 through receipt-1), but the
    # exponential bracket probes further. This remains an explicit refusal;
    # an arbitrary archive failure never invents a timestamp or narrows evidence.
    chain = RecentChain(readable=25)
    with pytest.raises(ChainUnavailable):
        Collector(chain, tmp_path, 2).prior_day(chain.receipt, 3)
    assert not list(tmp_path.glob('*.json'))


def test_irregular_timestamps_are_integrated_without_assumed_block_cadence(tmp_path):
    chain = RecentChain()
    moments = [-200000,-150000,-100000,-90000,-86000,-43000,-1,10]
    chain.block_timestamp = lambda block: chain.end + moments[block-chain.receipt+7]
    rows = Collector(chain, tmp_path, 2).prior_day(chain.receipt, 3)
    assert [(row.start-chain.end,row.end-chain.end) for row in rows] == [
        (-86400,-86000),(-86000,-43000),(-43000,-1),(-1,0)]
    assert [block for block,netuid in chain.price_reads if netuid==3] == [
        chain.receipt-4,chain.receipt-3,chain.receipt-2,chain.receipt-1]


def test_disagreeing_boundary_probe_is_never_treated_as_pruning(tmp_path):
    class Disagreeing(RecentChain):
        def block_timestamp(self, block):
            if block == self.receipt-1:
                raise SourceDisagreement('fixture disagreement')
            return super().block_timestamp(block)
    with pytest.raises(SourceDisagreement):
        Collector(Disagreeing(), tmp_path, 2).prior_day(RecentChain.receipt, 3)
    assert not list(tmp_path.glob('*.json'))


def test_unfinalized_receipt_is_rejected_before_timestamp_or_price(tmp_path):
    chain = RecentChain()
    with pytest.raises(Refusal):
        Collector(chain, tmp_path, 2).prior_day(chain.receipt+1, 3)
    assert chain.time_reads == chain.price_reads == []


def test_restart_reuses_complete_series_and_changed_hash_cannot_reuse_it(tmp_path):
    chain = RecentChain()
    original = Collector(chain, tmp_path, 2).prior_day(chain.receipt, 3)
    restarted = RecentChain()
    assert Collector(restarted, tmp_path, 2).prior_day(chain.receipt, 3) == original
    assert restarted.price_reads == []
    changed = RecentChain()
    changed.fork = 'different'
    assert Collector(changed, tmp_path, 2).prior_day(chain.receipt, 3) == original
    assert len(changed.price_reads) == 48


def test_daily_cache_binds_native_denomination_in_shared_directory(tmp_path):
    chain = RecentChain()
    first = Collector(chain, tmp_path, 2).prior_day(chain.receipt, 3)
    other = RecentChain()
    second = Collector(other, tmp_path, 9).prior_day(other.receipt, 3)
    assert all(row.noah_price_rao == R for row in first)
    assert all(row.noah_price_rao == 2*R for row in second)
    assert len(other.price_reads) == 48
    assert len(list(tmp_path.glob('*.json'))) == 2
    restarted = RecentChain()
    assert Collector(restarted, tmp_path, 9).prior_day(restarted.receipt, 3) == second
    assert restarted.price_reads == []


def test_no_full_day_before_chain_start_is_refused(tmp_path):
    chain = RecentChain(offset=0,readable=100)
    chain.receipt = 10
    with pytest.raises(Refusal):
        Collector(chain, tmp_path, 2).prior_day(10, 3)
    assert not list(tmp_path.glob('*.json'))


@pytest.mark.parametrize('count', [1, 8, 40])
def test_precollect_exact_scalar_bytes_bounded_groups_and_restart(tmp_path, count):
    assets = list(range(3, count + 3))
    scalar_path, grouped_path = tmp_path/'scalar', tmp_path/'grouped'
    scalar, grouped_chain = RecentChain(), RecentChain()
    before = Collector(scalar, scalar_path, 2)
    expected = {n: before.prior_day(scalar.receipt, n) for n in assets}
    grouped = Collector(grouped_chain, grouped_path, 2)
    sizes = []
    original = grouped._collect_group
    def collect(block, window, netuids):
        sizes.append(len(netuids))
        return original(block, window, netuids)
    grouped._collect_group = collect
    grouped.precollect(grouped_chain.receipt, reversed(assets))
    assert sizes == [8]*(count//8) + ([count%8] if count%8 else [])
    assert {p.name:p.read_bytes() for p in scalar_path.glob('*.json')} == {
        p.name:p.read_bytes() for p in grouped_path.glob('*.json')}
    # Read order is block-major within each group, without removing reads.
    first = grouped_chain.price_reads[:min(count,8)+1]
    assert len({h for h,_ in first}) == 1
    restarted = RecentChain()
    again = Collector(restarted, grouped_path, 2)
    again.precollect(restarted.receipt, assets)
    assert {n:again.prior_day(restarted.receipt,n) for n in assets} == expected
    assert restarted.price_reads == []
    assert len(grouped.prices) <= grouped.MAX_PRICES
    assert len(grouped.times) <= grouped.MAX_TIMES


def test_precollect_root_native_duplicates_and_mixed_warm_cache(tmp_path):
    chain = RecentChain()
    c = Collector(chain,tmp_path,2)
    prior = c.prior_day(chain.receipt,3)
    warm = {p.name:p.read_bytes() for p in tmp_path.glob('*.json')}
    chain.price_reads.clear()
    c.precollect(chain.receipt,[3,0,2,0,2,4])
    assert c.prior_day(chain.receipt,3) == prior
    assert not any(n in (0,3) for _,n in chain.price_reads)
    assert all((tmp_path/name).read_bytes()==data for name,data in warm.items())
    assert len(list(tmp_path.glob('*.json'))) == 4
    assert all(row.asset_price_rao==R for row in c.prior_day(chain.receipt,0))
    assert all(row.noah_price_rao==row.asset_price_rao for row in c.prior_day(chain.receipt,2))


@pytest.mark.parametrize('bad', [True, -1, '3', None])
def test_precollect_rejects_invalid_workload_before_reads(tmp_path,bad):
    chain = RecentChain()
    with pytest.raises(Refusal):
        Collector(chain,tmp_path,2).precollect(chain.receipt,[1,bad])
    assert chain.price_reads == chain.time_reads == []


@pytest.mark.parametrize('failure', [SourceDisagreement, ChainUnavailable])
@pytest.mark.parametrize('bad_asset,completed', [(4,0),(12,8)])
def test_precollect_failed_group_never_publishes_partial_series(tmp_path,failure,bad_asset,completed):
    class Broken(RecentChain):
        def alpha_price_at(self,block,*,netuid,expected_block_hash):
            if netuid==bad_asset and block==self.receipt-10:
                raise failure('injected mid-series refusal')
            return super().alpha_price_at(block,netuid=netuid,expected_block_hash=expected_block_hash)
    c=Collector(Broken(),tmp_path,2)
    with pytest.raises(failure):
        c.precollect(c.chain.receipt,range(3,19))
    assert len(list(tmp_path.glob('*.json'))) == completed
    # Retry can reuse only the earlier complete group, never a partial file.
    retry=Collector(RecentChain(),tmp_path,2)
    retry.precollect(retry.chain.receipt,range(3,19))
    assert len(list(tmp_path.glob('*.json'))) == 16


def test_precollect_corrupt_warm_cache_refuses_without_overwrite(tmp_path):
    import json
    c=Collector(RecentChain(),tmp_path,2)
    c.prior_day(c.chain.receipt,3)
    path=next(tmp_path.glob('*.json'))
    data=json.loads(path.read_text()); data['value']=[]
    path.write_text(json.dumps(data)); corrupted=path.read_bytes()
    c.chain.price_reads.clear()
    with pytest.raises(Refusal):
        c.precollect(c.chain.receipt,[3,4])
    assert path.read_bytes()==corrupted
    assert c.chain.price_reads==[] and len(list(tmp_path.glob('*.json')))==1


def test_precollect_multiple_days_do_not_share_series(tmp_path):
    chain=RecentChain(readable=200)
    c=Collector(chain,tmp_path,2)
    c.precollect(chain.receipt,[3,4])
    c.precollect(chain.receipt-24,[3,4])
    assert len(list(tmp_path.glob('*.json')))==4
    today=c.prior_day(chain.receipt,3)
    yesterday=c.prior_day(chain.receipt-24,3)
    assert yesterday[-1].end==today[0].start


def test_precollect_changed_boundary_during_preparation_refuses(tmp_path):
    from noah.chain.sources import SourceLied
    c=Collector(RecentChain(),tmp_path,2)
    original=c._window
    count=0
    def changed(block):
        nonlocal count
        count+=1
        window=original(block)
        if count>1: window[-1]='changed'
        return window
    c._window=changed
    with pytest.raises(SourceLied,match='changed during group'):
        c.precollect(c.chain.receipt,[3,4])
    assert not list(tmp_path.glob('*.json'))
