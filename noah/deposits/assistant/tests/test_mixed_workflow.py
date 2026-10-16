from dataclasses import replace
import hashlib

import pytest

from noah.deposits.assistant import mixed_workflow as W
from noah.deposits.assistant.amounts import DepositRefused
from .fakes import config
from .test_mixed import settings


class Adapter:
    def __init__(self, journal):
        self.journal=journal
        self.signs=[];self.sends=[];self.receipts={}
        self.crash=None;self.uncertain=False

    def identity(self):pass
    def contribution_budget(self):return 100
    def quote(self, plan):return {'estimated_output_rao':80, 'quote_block':100}
    def prepare(self, action, amount, quote):
        return {'start_block':100,'params':{'amount':amount}}
    def sign(self, step):
        self.signs.append(step['action'])
        return step, '0x'+hashlib.sha256(step['action'].encode()).hexdigest()
    def broadcast(self, step, *, before_submit):
        saved=self.journal.workflows()[0]['steps'][step['action']]
        assert saved['extrinsic_hash']==step['extrinsic_hash'] and saved['status']=='signed'
        before_submit()
        self.sends.append(step['action'])
        result={'status':'finalized', 'block_number':101, 'extrinsic_id':'101-2'}
        if step['action']=='convert':
            result['conversion_result']={'noah_spent_rao':40,'foreign_received_rao':73}
        self.receipts[step['action']]=result
        if self.crash==step['action']:raise SystemExit('simulated process death after submission')
    def reconcile(self, step):
        if self.uncertain:return {'status':'uncertain'}
        return self.receipts.get(step['action'],{'status':'uncertain'})


def setup(tmp_path):
    journal=W.MixedJournal(tmp_path)
    adapter=Adapter(journal)
    cfg=config(genesis_hash='0x'+'b'*64,mode='fixed',amount_rao=100)
    return cfg,journal,adapter


def run(cfg,journal,adapter,**kw):
    return W.run(cfg,settings(),adapter,journal,label=kw.pop('label','one'),
                 ask=lambda p:'SEND MIXED 0.0000001 NOAH',say=lambda _:None,**kw)


def test_preview_never_signs_or_records_an_intent(tmp_path):
    cfg,j,a=setup(tmp_path)
    result=run(cfg,j,a,preview=True)
    assert result['preview'] and result['quote']['estimated_output_rao']==80
    assert j.workflows()==[] and a.signs==[] and a.sends==[]


def test_exact_output_drives_ordered_deposits_and_repeat_is_idempotent(tmp_path):
    cfg,j,a=setup(tmp_path)
    result=run(cfg,j,a)
    assert a.sends==['convert','deposit-noah','deposit-foreign']
    assert result['steps']['deposit-noah']['amount_rao']==60
    assert result['steps']['deposit-foreign']['amount_rao']==73  # Not quote=80 or prior holdings.
    assert result['delivery_complete'] and result['credit_verified'] is False
    assert result['settlement_verified'] is False
    run(cfg,j,a)
    assert len(a.sends)==3


@pytest.mark.parametrize('action',W.STEPS)
def test_crash_after_broadcast_reconciles_without_duplicate(tmp_path,action):
    cfg,j,a=setup(tmp_path);a.crash=action
    with pytest.raises(SystemExit):run(cfg,j,a)
    a.crash=None
    result=run(cfg,j,a)
    assert result['delivery_complete']
    assert a.sends==list(W.STEPS)


def test_uncertain_conversion_blocks_both_deposit_legs_and_new_label(tmp_path):
    cfg,j,a=setup(tmp_path);a.uncertain=True
    result=run(cfg,j,a)
    assert not result['delivery_complete'] and a.sends==['convert']
    run(cfg,j,a)
    assert a.sends==['convert']
    with pytest.raises(DepositRefused):run(cfg,j,a,label='two')
    assert a.sends==['convert']
    a.uncertain=False
    assert run(cfg,j,a)['delivery_complete']
    assert a.sends==list(W.STEPS)


def test_status_reconciles_without_sending_remaining_legs(tmp_path):
    cfg,j,a=setup(tmp_path);a.crash='convert'
    with pytest.raises(SystemExit):run(cfg,j,a)
    a.crash=None
    result=run(cfg,j,a,status=True)
    assert result['steps']['convert']['status']=='finalized'
    assert not result['delivery_complete'] and a.sends==['convert']


def test_changed_config_cannot_reuse_original_intent(tmp_path):
    cfg,j,a=setup(tmp_path);run(cfg,j,a)
    with pytest.raises(DepositRefused):run(replace(cfg,amount_rao=200),j,a)
    assert a.sends==list(W.STEPS)


def test_crash_before_durable_hash_blocks_instead_of_resigning(tmp_path):
    cfg,j,a=setup(tmp_path)
    original=a.sign
    def crash(step):raise SystemExit('crash while opening signer')
    a.sign=crash
    with pytest.raises(SystemExit):run(cfg,j,a)
    a.sign=original
    assert run(cfg,j,a)['steps']['convert']['status']=='uncertain'
    assert a.signs==[] and a.sends==[]


def test_signing_error_text_not_persisted(tmp_path):
    cfg,j,a=setup(tmp_path)
    def crash(step):raise RuntimeError('PRIVATE_SENTINEL')
    a.sign=crash
    result=run(cfg,j,a)
    assert result['steps']['convert']['status']=='uncertain'
    assert 'PRIVATE_SENTINEL' not in str(j.workflows())
