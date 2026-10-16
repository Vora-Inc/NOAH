import copy
from dataclasses import replace

import pytest

from noah.deposits.assistant import mixed as M
from noah.deposits.assistant.amounts import DepositRefused
from .fakes import COLD, MINER, VAL, config


def settings(**kw):
    return replace(M.MixedSettings(3, VAL, None, 4000, 100, 5, 1000000,
                                  '0x'+'a'*64, 478), **kw)


def events():
    def event(module, name, attrs):
        return dict(extrinsic_idx=2, event=dict(module_id=module, event_id=name, attributes=attrs))
    return [event('SubtensorModule','StakeRemoved',[COLD,MINER,999,40,2,0]),
            event('SubtensorModule','StakeAdded',[COLD,MINER,999,73,3,0]),
            event('SubtensorModule','StakeSwapped',[COLD,MINER,2,3,999]),
            event('System','ExtrinsicSuccess',[])]


def decode(rows):
    return M.conversion_result(rows,coldkey=COLD,hotkey=MINER,origin_netuid=2,
                               destination_netuid=3,requested_rao=40,extrinsic_index=2)


def test_budget_first_then_swap_and_actual_output():
    assert M.allocation(101, settings()) == {
        'contribution_budget_rao':101,'swap_budget_rao':40,'noah_deposit_rao':61}
    assert M.allocation(101, settings(swap_amount_rao=40,swap_percent_of_budget_bp=None))['swap_budget_rao']==40
    result=decode(events())
    assert result=={'noah_spent_rao':40,'foreign_received_rao':73,
                    'source_swap_alpha_rao':40,'source_alpha_fee_rao':0,
                    'destination_tao_fee_rao':0}
    assert M.delivered_amounts(101,result)=={'noah_rao':61,'foreign_rao':73}
    # 999 is TAO-equivalent event information, not a raw-alpha amount.
    assert 999 not in M.delivered_amounts(101,result).values()


@pytest.mark.parametrize('total,swap',[(0,1),(10,10),(10,11),(True,1)])
def test_invalid_budget_refused(total,swap):
    with pytest.raises(DepositRefused):M.allocation(total,settings(swap_amount_rao=swap,swap_percent_of_budget_bp=None))


@pytest.mark.parametrize('mutation', ['missing','duplicate','wrong_owner','wrong_hotkey',
                                    'wrong_netuid','wrong_amount','failed','wrong_index','extra_fields'])
def test_conversion_needs_exact_unambiguous_success(mutation):
    rows=events()
    if mutation=='missing':rows.pop(1)
    elif mutation=='duplicate':rows.append(copy.deepcopy(rows[0]))
    elif mutation=='wrong_owner':rows[0]['event']['attributes'][0]=VAL
    elif mutation=='wrong_hotkey':rows[1]['event']['attributes'][1]=VAL
    elif mutation=='wrong_netuid':rows[1]['event']['attributes'][4]=4
    elif mutation=='wrong_amount':rows[0]['event']['attributes'][3]=39
    elif mutation=='failed':rows[-1]['event']['event_id']='ExtrinsicFailed'
    elif mutation=='wrong_index':rows[1]['extrinsic_idx']=3
    elif mutation=='extra_fields':rows[1]['event']['attributes'].append(0)
    with pytest.raises(DepositRefused):decode(rows)


def test_other_extrinsic_does_not_supply_or_change_output():
    rows=events();other=copy.deepcopy(rows[1]);other['extrinsic_idx']=1
    other['event']['attributes'][3]=999999
    assert decode(rows+[other])['foreign_received_rao']==73


def test_production_gate_cannot_be_enabled_by_label_or_flag():
    from noah.core.chain import MAINNET
    cfg=config(genesis_hash='0x'+'b'*64,mode='fixed')
    M.test_gate(cfg,settings())
    for bad in (replace(cfg,network='mainnet'),replace(cfg,genesis_hash=MAINNET),
                replace(cfg,genesis_hash=None)):
        with pytest.raises(DepositRefused):M.test_gate(bad,settings())
    with pytest.raises(DepositRefused):M.test_gate(cfg,settings(test_only=False))


def test_ini_requires_explicit_test_scope_and_exact_swap_size(tmp_path):
    p=tmp_path/'mixed.ini'
    text=('\n[mixed]\ntest_only = true\nforeign_netuid = 3\n'
          f'foreign_ark_hotkey = {VAL}\nswap_percent_of_budget_bp = 4000\nmax_slippage_bp = 100\n'
          'quote_max_age_blocks = 5\nmax_fee_rao = 1000000\nspec_version = 478\n'
          'runtime_code_hash = 0x'+'a'*64+'\n')
    p.write_text(text);assert M.load_settings(p)==settings()
    p.write_text(text+'swap_amount_rao = 1\n')
    with pytest.raises(DepositRefused):M.load_settings(p)
    p.write_text(text.replace('test_only = true','test_only = false'))
    with pytest.raises(DepositRefused):M.load_settings(p)


def test_earnings_denominators_and_snapshot_identity_are_preserved():
    args=dict(earnings_rao=1001,contribution_bp=8000,swap_of_earnings_bp=2500,
              earnings_snapshot_id='synthetic-finalized-day-1',swap_snapshot_id='synthetic-finalized-day-1')
    result=M.allocation_from_earnings(**args)
    assert result['contribution_budget_rao']==800
    assert result['swap_budget_rao']==250  # Not 25% of the 800 contribution.
    assert result['noah_deposit_rao']==550
    with pytest.raises(DepositRefused):M.allocation_from_earnings(**dict(args,swap_snapshot_id='different-day'))
    with pytest.raises(DepositRefused):M.allocation_from_earnings(**dict(args,swap_of_earnings_bp=9000))


def test_proxy_and_validator_positions_are_refused_before_chain_access():
    from noah.deposits.assistant.mixed_workflow import run
    from unittest.mock import Mock
    cfg=config(genesis_hash='0x'+'b'*64,mode='fixed')
    for bad in (replace(cfg,stake_position='validator'),
                replace(cfg,signing={'mode':'proxy'})):
        adapter,journal=Mock(),Mock()
        with pytest.raises(DepositRefused):
            run(bad,settings(),adapter,journal,label='refuse')
        adapter.identity.assert_not_called()
        adapter.sign.assert_not_called()
        journal.locked.assert_not_called()


@pytest.mark.parametrize('mode',['percent','maintenance','custom'])
def test_nonfixed_mixed_budget_refused_before_chain_or_key(mode):
    from noah.deposits.assistant.mixed_workflow import run
    from unittest.mock import Mock
    adapter,journal=Mock(),Mock()
    with pytest.raises(DepositRefused):
        run(config(genesis_hash='0x'+'b'*64,mode=mode),settings(),adapter,journal,label='no')
    adapter.identity.assert_not_called()
    adapter.sign.assert_not_called()


def test_source_alpha_fee_is_part_of_gross_noah_debit():
    rows=events();rows[0]['event']['attributes'][3]=37;rows[0]['event']['attributes'][5]=3
    rows[1]['event']['attributes'][5]=999  # TAO fee, never foreign alpha.
    result=decode(rows)
    assert result['noah_spent_rao']==40
    assert result['source_swap_alpha_rao']==37 and result['source_alpha_fee_rao']==3
    assert result['destination_tao_fee_rao']==999
    assert M.delivered_amounts(100,result)=={'noah_rao':60,'foreign_rao':73}


@pytest.mark.parametrize('value',[True,-1,'3',4])
def test_source_fee_requires_integer_and_exact_gross(value):
    rows=events();rows[0]['event']['attributes'][3]=37;rows[0]['event']['attributes'][5]=value
    with pytest.raises(DepositRefused):decode(rows)
