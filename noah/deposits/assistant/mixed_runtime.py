"""Direct SDK adapter for bounded private-test mixed contributions."""
from dataclasses import asdict, replace

from ..availability import deposit_ceiling
from ..chain import ceiling_from_chain
from ..deposit_submission import BytesConvertingSigner
from .mixed import NotSubmitted, conversion_result, refuse, test_gate
from ...chain.sources import Refusal
from .proxy_signing import same, uint, unwrap
from .session import _check_transaction_can_succeed, calculate
from .signer import ERA, hash_text


class Adapter:
    def __init__(self, config, settings, services):
        self.config, self.settings, self.services = config, settings, services
        self.connection = services.connection
        self.ledger = services.ledger

    def identity(self):
        c, s, conn = self.config, self.settings, self.connection
        test_gate(c, s)
        if str(conn.get_block_hash(0)) != c.genesis_hash:
            refuse('mixed chain genesis differs from the configured test chain')
        versions = []
        for head in (conn.get_chain_finalised_head(), conn.get_chain_head()):
            code = conn.rpc_request('state_getStorageHash', ['0x3a636f6465', head])['result']
            version = conn.rpc_request('state_getRuntimeVersion', [head])['result']
            if code != s.runtime_code_hash or version['specVersion'] != s.spec_version:
                refuse('mixed runtime changed; review the exact runtime before continuing')
            versions.append(uint(version.get('transactionVersion'), 'transaction version'))
        if versions[0] != versions[1]:
            refuse('mixed transaction version differs between finalized and best heads')
        self.transaction_version = versions[0]
        if not same(unwrap(conn.query('SubtensorModule', 'Owner', [c.miner_hotkey])), c.coldkey):
            refuse('configured coldkey does not own the original miner hotkey')

    def contribution_budget(self):
        proposal, _, _, _ = calculate(self.config, self.services)
        if proposal.status not in ('ready', 'partial'):
            refuse('no contribution budget is ready for this mixed workflow')
        return uint(proposal.amount_rao, 'contribution budget', 1)

    def available(self, netuid):
        c = self.config
        inputs = ceiling_from_chain(self.services.source, coldkey=c.coldkey,
            hotkey=c.miner_hotkey, netuid=netuid,
            allow_unregistered_position=netuid != c.netuid)
        return deposit_ceiling(position_stake=inputs.position_stake,
            position_collateral=inputs.position_collateral,
            coldkey_available=inputs.coldkey_available).amount

    def enabled(self, netuid):
        if unwrap(self.connection.query('SubtensorModule', 'NetworksAdded', [netuid])) is not True:
            refuse('mixed subnet is absent')
        _check_transaction_can_succeed(replace(self.config, netuid=netuid), self.services)

    def conversion_minimum(self, amount, simulation=None):
        # Runtime v478 validate_stake_transition uses strict > DefaultMinStake;
        # DefaultMinStake is InitialMinStake, in TAO rao, not source alpha.
        minimum = uint(unwrap(self.connection.get_constant('SubtensorModule', 'InitialMinStake')),
                       'minimum swap TAO', 1)
        if simulation is None:
            simulation = unwrap(self.connection.runtime_call('SwapRuntimeApi',
                'sim_swap_alpha_for_tao', [self.config.netuid, amount]))
        tao = uint(simulation['tao_amount'], 'simulated swap TAO')
        if tao <= minimum:
            refuse(f'conversion is below the runtime minimum: simulated {tao} TAO rao must be greater than {minimum}; increase the reviewed fixed swap budget')
        return {'simulated_tao_rao': tao, 'strict_minimum_tao_rao': minimum}

    def quote(self, plan):
        self.identity()
        c, s, conn = self.config, self.settings, self.connection
        for n in (c.netuid, s.foreign_netuid):
            self.enabled(n)
        if plan['contribution_budget_rao'] + c.reserve_rao > self.available(c.netuid):
            refuse('contribution budget exceeds unlocked NOAH after reserve')
        block = conn.get_block_number(conn.get_chain_head())
        prices = [uint(unwrap(conn.runtime_call('SwapRuntimeApi', 'current_alpha_price', [n])),
                       'alpha price', 1) for n in (c.netuid, s.foreign_netuid)]
        ratio = prices[0] * 1_000_000_000 // prices[1]
        limit = (ratio * (10000-s.max_slippage_bp) + 9999) // 10000
        uint(limit, 'conversion price limit', 1)
        tao = unwrap(conn.runtime_call('SwapRuntimeApi', 'sim_swap_alpha_for_tao',
                                      [c.netuid, plan['swap_budget_rao']]))
        minimum = self.conversion_minimum(plan['swap_budget_rao'], tao)
        output = unwrap(conn.runtime_call('SwapRuntimeApi', 'sim_swap_tao_for_alpha',
                                         [s.foreign_netuid, uint(tao['tao_amount'], 'simulated TAO', 1)]))
        params=dict(hotkey=c.miner_hotkey, origin_netuid=c.netuid,
            destination_netuid=s.foreign_netuid, alpha_amount=plan['swap_budget_rao'],
            limit_price=limit, allow_partial=False)
        estimated=uint(output['alpha_amount'], 'simulated alpha', 1)
        calls={'convert':conn.compose_call(call_module='SubtensorModule',
            call_function='swap_stake_limit',call_params=params)}
        for name,netuid,hotkey,amount in (
            ('deposit-noah',c.netuid,c.validator_hotkey,plan['contribution_budget_rao']-plan['swap_budget_rao']),
            ('deposit-foreign',s.foreign_netuid,s.foreign_ark_hotkey,estimated)):
            calls[name]=conn.compose_call(call_module='SubtensorModule',
                call_function='transfer_stake_and_hotkey',call_params={
                    'destination_coldkey':c.ark_coldkey,'origin_hotkey':c.miner_hotkey,
                    'destination_hotkey':hotkey,'origin_netuid':netuid,
                    'destination_netuid':netuid,'alpha_amount':amount})
        return dict(params=params, quote_block=block, minimum=minimum,
            contribution_budget_rao=plan['contribution_budget_rao'],
            estimated_output_rao=estimated,
            fee_estimates_rao={name:self.fee(call) for name,call in calls.items()},
            recipient={'ark_coldkey':c.ark_coldkey,'noah_hotkey':c.validator_hotkey,
                       'foreign_hotkey':s.foreign_ark_hotkey},
            note='Estimated foreign quantity is not a deposit amount or scoring valuation.')

    def fee(self, call):
        from bittensor.sp_core import Keypair
        conn, c = self.connection, self.config
        public = Keypair(ss58_address=c.coldkey)  # No wallet access.
        info = unwrap(conn.get_payment_info(call=call, keypair=public))
        fee = uint(info.get('partial_fee', info.get('partialFee')), 'fee estimate')
        if 2*fee > self.settings.max_fee_rao:
            refuse('mixed transaction fee exceeds its configured bound')
        data = unwrap(conn.query('System', 'Account', [c.coldkey]))['data']
        frozen = max(uint(data.get(n, 0), n) for n in ('frozen','misc_frozen','fee_frozen'))
        existential = uint(unwrap(conn.get_constant('Balances','ExistentialDeposit')), 'existential deposit')
        if uint(data['free'], 'free TAO') - frozen - existential < 2*fee:
            refuse('mixed transaction cannot cover its fee with headroom')
        return fee

    def prepare(self, action, amount, quote):
        self.identity()
        c, s, conn = self.config, self.settings, self.connection
        uint(amount, 'step amount', 1)
        if action == 'convert':
            age = conn.get_block_number(conn.get_chain_head()) - quote['quote_block']
            if not 0 <= age <= s.quote_max_age_blocks:
                raise NotSubmitted('quote_expired')
            if amount != quote['params']['alpha_amount']:
                refuse('conversion amount differs from its approved quote')
            if quote['contribution_budget_rao'] + c.reserve_rao > self.available(c.netuid):
                refuse('the contribution budget is no longer available after reserve')
            function, params = 'swap_stake_limit', quote['params']
            for n in (c.netuid, s.foreign_netuid):self.enabled(n)
        elif action in ('deposit-noah', 'deposit-foreign'):
            foreign = action == 'deposit-foreign'
            netuid = s.foreign_netuid if foreign else c.netuid
            reserve = 0 if foreign else c.reserve_rao  # NOAH reserve is never foreign units.
            self.enabled(netuid)
            if amount + reserve > self.available(netuid):
                refuse('actual deposit amount is no longer available after reserve')
            function = 'transfer_stake_and_hotkey'
            params = dict(destination_coldkey=c.ark_coldkey, origin_hotkey=c.miner_hotkey,
                destination_hotkey=s.foreign_ark_hotkey if foreign else c.validator_hotkey,
                origin_netuid=netuid, destination_netuid=netuid, alpha_amount=amount)
        else:
            refuse('unsupported mixed action')
        call = conn.compose_call(call_module='SubtensorModule', call_function=function, call_params=params)
        fee = self.fee(call)
        return dict(function=function, params=params, fee_estimate_rao=fee,
                    quote_block=quote['quote_block'] if action=='convert' else None,
                    start_block=conn.get_block_number(conn.get_chain_finalised_head()))

    def fresh(self, step):
        if step['action']=='convert':
            self.conversion_minimum(step['amount_rao'])
            age=self.connection.get_block_number(self.connection.get_chain_head())-step['quote_block']
            if not 0<=age<=self.settings.quote_max_age_blocks:
                raise NotSubmitted('quote_expired')

    def sign(self, step):
        self.identity()
        self.fresh(step)
        conn = self.connection
        call = conn.compose_call(call_module='SubtensorModule', call_function=step['function'],
                                 call_params=step['params'])
        self.fee(call)
        key = self.services.signer_factory()
        if not same(key.ss58_address, self.config.coldkey):
            refuse('mixed signing wallet differs from the original owner')
        adapter = self
        class GuardedKey:
            def __getattr__(self, name):return getattr(key, name)
            def sign(self, payload):
                adapter.identity()
                adapter.fresh(step)
                context = getattr(conn, 'runtime', None)
                if (getattr(context, 'runtime_version', None) != adapter.settings.spec_version
                        or getattr(context, 'transaction_version', None) != adapter.transaction_version):
                    refuse('SDK signing context differs from reviewed runtime')
                return key.sign(payload)
        nonce = conn.get_account_next_index(self.config.coldkey)
        signed = conn.create_signed_extrinsic(call=call, keypair=BytesConvertingSigner(GuardedKey()),
            nonce=nonce,
            era={'period':ERA,'current':step['start_block']})
        # Provenance for inspection; legacy rows do not contain these fields.
        step['signing_context'] = dict(nonce=nonce, era_period=ERA,
                                      era_current=step['start_block'])
        return (signed,step), hash_text(signed.extrinsic_hash)

    def broadcast(self, signed, *, before_submit):
        signed,step=signed
        try:
            self.identity()
            self.fresh(step)
        except NotSubmitted:
            raise
        except (Exception, Refusal):
            # This catch encloses only read-only guards, never submit_extrinsic.
            raise NotSubmitted('pre_submission_check_failed') from None
        before_submit()  # Persist attempt BEFORE any call into submission code.
        self.connection.submit_extrinsic(signed, wait_for_inclusion=True, wait_for_finalization=True)

    def reconcile(self, step):
        self.identity()
        conn = self.connection
        final = conn.get_block_number(conn.get_chain_finalised_head())
        for number in range(step['start_block'], min(final, step['start_block']+2*ERA)+1):
            block_hash = conn.get_block_hash(number)
            for index, extrinsic in enumerate(conn.get_block(block_hash=block_hash)['extrinsics']):
                raw_hash = getattr(extrinsic, 'extrinsic_hash', None)
                if raw_hash is None:refuse('history cannot supply exact transaction hashes')
                if hash_text(raw_hash) != step['extrinsic_hash']:continue
                value = unwrap(extrinsic)
                call = value.get('call', {})
                params = {a['name']:a['value'] for a in call.get('call_args', [])}
                if (not same(value.get('address'), self.config.coldkey)
                        or call.get('call_module') != 'SubtensorModule'
                        or call.get('call_function') != step['function'] or params != step['params']):
                    refuse('finalized transaction differs from the saved mixed intent')
                events = [unwrap(e) for e in conn.get_events(block_hash=block_hash)]
                own = [e for e in events if e.get('extrinsic_idx') == index]
                system = {e.get('event',e).get('event_id') for e in own
                          if e.get('event',e).get('module_id')=='System'}
                result = dict(block_number=number, block_hash=str(block_hash), extrinsic_id=f'{number}-{index}')
                if 'ExtrinsicFailed' in system:
                    failures = [e.get('event',e).get('attributes') for e in own
                        if e.get('event',e).get('module_id')=='System'
                        and e.get('event',e).get('event_id')=='ExtrinsicFailed']
                    fees = [e.get('event',e).get('attributes') for e in own
                        if e.get('event',e).get('module_id')=='TransactionPayment'
                        and e.get('event',e).get('event_id')=='TransactionFeePaid']
                    movements = [e for e in own if e.get('event',e).get('module_id')=='SubtensorModule'
                        and e.get('event',e).get('event_id','').startswith('Stake')]
                    return dict(result,status='failed',dispatch_failures=failures,
                                transaction_fees=fees,zero_stake_movements=not movements)
                if 'ExtrinsicSuccess' not in system:return dict(result,status='uncertain')
                if step['action']=='convert':
                    actual = conversion_result(events,coldkey=self.config.coldkey,
                        hotkey=self.config.miner_hotkey,origin_netuid=self.config.netuid,
                        destination_netuid=self.settings.foreign_netuid,
                        requested_rao=step['amount_rao'],extrinsic_index=index)
                    return dict(result,status='finalized',conversion_result=actual)
                from .reconcile import deposits_in
                foreign = step['action']=='deposit-foreign'
                hotkey = self.settings.foreign_ark_hotkey if foreign else self.config.validator_hotkey
                netuid = self.settings.foreign_netuid if foreign else self.config.netuid
                matches = [d for d in deposits_in(conn,ark_coldkey=self.config.ark_coldkey,
                    validator_hotkey=hotkey,block=number) if d.extrinsic_index==index]
                if len(matches)!=1:refuse('mixed deposit receipt is missing or ambiguous')
                d=matches[0]
                if (d.unattributed_reason is not None or not same(d.coldkey,self.config.coldkey)
                        or not same(d.origin_hotkey,self.config.miner_hotkey)
                        or d.token_netuid!=netuid or d.amount_rao!=step['amount_rao']):
                    refuse('mixed deposit receipt differs from the intended asset, amount or miner')
                return dict(result,status='finalized',receipt=asdict(d))
        # Keep uncertain even after mortality: no automatic fresh submission.
        return {'status':'uncertain'}


    def certify_failed_conversion(self, step):
        """Only the early AmountTooLow failure can be explicitly closed here."""
        if step.get('action') != 'convert' or step.get('function') != 'swap_stake_limit':
            refuse('failure closure only accepts the original protected conversion')
        result = self.reconcile(step)  # Exact hash/call/owner, final chain and runtime again.
        if result.get('status') != 'failed' or result.get('zero_stake_movements') is not True:
            refuse('failure closure needs finalized failure with zero stake movements')
        failures, fees = result['dispatch_failures'], result['transaction_fees']
        if len(failures) != 1 or len(fees) != 1:
            refuse('failure or fee evidence is missing or ambiguous')
        error = failures[0].get('dispatch_error', {}).get('Module', {})
        expected = unwrap(self.connection.get_metadata_error('SubtensorModule', 'AmountTooLow',
                                                            block_hash=result['block_hash']))
        call = self.connection.compose_call(call_module='SubtensorModule',
            call_function=step['function'], call_params=step['params'])
        module = int(str(call.call_index).removeprefix('0x')[:2], 16)
        expected_bytes = uint(expected['index'], 'AmountTooLow error index').to_bytes(4, 'little')
        if error.get('index') != module or error.get('error') != '0x'+expected_bytes.hex():
            refuse('only the reviewed early AmountTooLow failure can be closed')
        if not same(fees[0].get('who'), self.config.coldkey):
            refuse('failed conversion fee payer differs from original owner')
        uint(fees[0].get('actual_fee'), 'finalized failed conversion fee')
        return dict(result, extrinsic_hash=step['extrinsic_hash'],
                    decoded_error='AmountTooLow', retry_submitted=False)


def main(path, *, label, preview=False, status=False, journal_path=None, close_failed=False):
    if sum(bool(v) for v in (preview, status, close_failed)) > 1:
        refuse('select only one of preview, status, or close-failed')
    from pathlib import Path
    import json
    from .config import load_config
    from .mixed import load_settings
    from .mixed_workflow import MixedJournal, run
    from .cli import build_services, _close_quietly
    config,settings=load_config(path),load_settings(path)
    test_gate(config,settings)
    services=build_services(config)
    journal=None
    try:
        journal=MixedJournal(journal_path or Path(config.state_dir)/'mixed')
        result=run(config,settings,Adapter(config,settings,services),journal,
                   label=label,preview=preview,status=status,close_failed=close_failed)
        print(json.dumps(result,indent=2))
        return 0 if (result.get('preview') or result.get('delivery_complete')
                     or (close_failed and result.get('closed_failed')) or status) else 2
    finally:
        if journal is not None:journal.db.close()
        services.ledger.close()
        _close_quietly(services.connection)
