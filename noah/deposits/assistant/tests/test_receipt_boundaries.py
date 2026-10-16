"""Receipt reconciliation must preserve chain and token identity."""
from dataclasses import replace

import pytest

from noah.deposits.assistant import ledger as L, session as S
from .fakes import FakeChain, FakeKeypair, MINER, N
from .test_session import Rig


@pytest.mark.parametrize('command', ['preview', 'run_once'])
def test_wrong_chain_cannot_abandon_pending_intent(tmp_path, command):
    chain = FakeChain()
    chain.submit_mode = 'raise_before_inclusion'
    rig = Rig(tmp_path, chain=chain)
    assert rig.run()['status'] == L.UNCERTAIN
    before = rig.ledger.for_miner(MINER)
    chain.advance(S.ABANDON_AFTER_BLOCKS + 1)
    rig.config = replace(rig.config, genesis_hash='0x' + 'a' * 64)
    result = getattr(S, command)(rig.config, rig.services())
    assert result['status'] == 'refused'
    assert result['code'] == 'wrong_chain'
    assert rig.ledger.for_miner(MINER) == before
    assert len(chain.submitted) == 1


def test_foreign_alpha_receipt_cannot_finalize_noah_intent(tmp_path):
    class ForeignReceiptChain(FakeChain):
        def _effects(self, signed):
            call = signed['call']
            foreign = dict(call, call_params=dict(call['call_params'],
                                                  origin_netuid=99, destination_netuid=99))
            return super()._effects(dict(signed, call=foreign))
    rig = Rig(tmp_path, chain=ForeignReceiptChain())
    assert rig.run()['status'] == L.UNQUALIFIED
    [row] = rig.ledger.for_miner(MINER)
    assert 'subnet 99 alpha, intended subnet 2' in str(row)
    rig.run()
    assert len(rig.chain.submitted) == 1


def test_signing_exception_details_never_reach_output_or_ledger(tmp_path):
    marker = 'SECRET_SENTINEL_DO_NOT_PERSIST'
    class BadKey(FakeKeypair):
        def sign(self, payload):
            raise RuntimeError(marker)
    rig = Rig(tmp_path, keypair=BadKey(), mode='fixed', amount_rao=200*N)
    assert rig.run()['status'] == L.FAILED
    assert marker not in rig.text()
    assert marker not in str(rig.ledger.for_miner(MINER))
    assert rig.chain.submitted == []
