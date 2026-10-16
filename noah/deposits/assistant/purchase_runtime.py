"""Direct-owner, private-test SDK adapter for fixed free-TAO purchases."""
from ...chain.sources import Refusal
from .mixed import NotSubmitted, refuse
from .mixed_runtime import Adapter as MixedAdapter
from .purchase import plan, purchase_result, test_gate
from .proxy_signing import same, uint, unwrap, is_proxy, signing, preflight, request
from .signer import ERA, hash_text


class Adapter(MixedAdapter):
    def root_check(self, action):
        if self.settings.foreign_netuid == 0:
            from .purchase_root import root_preflight
            return root_preflight(self.connection, self.config, self.settings,
                action='purchase' if action == 'purchase' else 'deposit-foreign')

    def identity(self):
        test_gate(self.config, self.settings)
        c,s,conn=self.config,self.settings,self.connection
        if str(conn.get_block_hash(0))!=c.genesis_hash:refuse('purchase chain genesis differs')
        versions=[]
        for head in (conn.get_chain_finalised_head(),conn.get_chain_head()):
            code=conn.rpc_request('state_getStorageHash',['0x3a636f6465',head])['result']
            version=conn.rpc_request('state_getRuntimeVersion',[head])['result']
            if code!=s.runtime_code_hash or version['specVersion']!=s.spec_version:
                refuse('purchase runtime differs from reviewed runtime')
            versions.append(uint(version.get('transactionVersion'),'transaction version'))
        if versions[0]!=versions[1]:refuse('purchase runtime versions differ between chain heads')
        self.transaction_version=versions[0]
        if not same(unwrap(conn.query('SubtensorModule','Owner',[c.miner_hotkey])),c.coldkey):
            refuse('coldkey does not own original miner hotkey')

    def funds(self, purchase_rao, fee_budget_rao):
        """No discretionary reserve; preserve positive funds and runtime survival."""
        conn = self.connection
        def require(address, spend):
            data=unwrap(conn.query('System','Account',[address]))['data']
            free=uint(data['free'],'free TAO')
            frozen=max(uint(data.get(k,0),k) for k in ('frozen','misc_frozen','fee_frozen'))
            survival=max(1,uint(unwrap(conn.get_constant('Balances','ExistentialDeposit')),'existential deposit'))
            if free-frozen-spend<survival:
                refuse('purchase and fees would exhaust spendable TAO or account survival')
        if is_proxy(self.config):
            require(self.config.coldkey,purchase_rao)
            require(signing(self.config)['proxy_address'],fee_budget_rao)
        else:require(self.config.coldkey,purchase_rao+fee_budget_rao)

    @property
    def uses_proxy(self):
        return is_proxy(self.config)

    def fee(self, call):
        if not self.uses_proxy:return super().fee(call)
        value=unwrap(call)
        function=value.get('call_function')
        action='purchase' if function=='add_stake_limit' else 'deposit'
        _,facts=preflight(self.connection,self.config,call,action)
        fee=facts['fee_estimate_rao']
        if 2*fee>self.settings.max_fee_rao:refuse('proxy fee exceeds configured bound')
        return fee

    def proxy_request(self, step, operation):
        action={'purchase':'purchase','deposit-noah':'purchase-noah',
                'deposit-foreign':'purchase-foreign'}[step['action']]
        return request(self.config,action=action,intent_id=self.settings.plan_id,
                       amount_rao=step['amount_rao'],operation=operation,
                       limit_price=step['params'].get('limit_price'),quote_block=step['quote_block'],
                       journal_id=step.get('journal_id'),reservation_token=step.get('reservation_token'))

    def cancel_proxy(self, operation, cancellation):
        return request(self.config,action='purchase',intent_id=self.settings.plan_id,
                       amount_rao=self.settings.max_tao_rao,operation=operation,cancellation=cancellation)

    def prepare_proxy(self, step):
        return self.proxy_request(step,'prepare')

    def recover_proxy(self, step):
        return self.proxy_request(step,'recover-pre-submit')

    def submit_proxy(self, step):
        self.identity()
        # PREPARE checked the client plan. The signer independently requotes
        # and checks the exact typed call at signature and submit boundaries.
        return self.proxy_request(step,'submit')

    def quote(self, *, limit_price=None, recovery=False):
        self.identity()
        c,s,conn = self.config,self.settings,self.connection
        fixed = plan(c,s)
        self.root_check("purchase")
        for n in (c.netuid,s.foreign_netuid):self.enabled(n)
        if c.amount_rao + c.reserve_rao > self.available(c.netuid):
            refuse('fixed NOAH deposit is unavailable after its configured reserve')
        head = conn.get_chain_finalised_head()
        price = uint(unwrap(conn.runtime_call('SwapRuntimeApi','current_alpha_price',
                                             [s.foreign_netuid],block_hash=head)), 'purchase price',1)
        # This controls maximum execution price, not spending: input remains
        # exactly max_tao_rao even if the market price moves within this bound.
        limit = price * (10000+s.max_slippage_bp) // 10000
        uint(limit,'maximum TAO per alpha',1)
        simulation = unwrap(conn.runtime_call('SwapRuntimeApi','sim_swap_tao_for_alpha',
                                              [s.foreign_netuid,s.max_tao_rao],block_hash=head))
        estimated = uint(simulation['alpha_amount'],'estimated acquired alpha',1)
        if s.foreign_netuid == 0:
            from .purchase_root import root_quote
            root = root_quote(price, simulation, s.max_tao_rao)
            limit, estimated = root['limit_price'], root['estimated_output_rao']
        if limit_price is not None:
            requested = uint(limit_price, 'requested purchase limit', 1)
            if requested > limit:
                refuse('purchase limit exceeds trusted slippage bound')
            if s.foreign_netuid == 0 and requested != limit:
                refuse('root purchase requires the exact one-to-one price limit')
            limit = requested
        # Estimate fees on the exact bounded call that will be signed, including
        # a signer's stricter requested limit. No checked snapshot is cached.
        params = dict(hotkey=c.miner_hotkey,netuid=s.foreign_netuid,
                      amount_staked=s.max_tao_rao,limit_price=limit,allow_partial=False)
        calls = {'purchase':conn.compose_call(call_module='SubtensorModule',
                    call_function='add_stake_limit',call_params=params)}
        for action,amount in [('deposit-noah',c.amount_rao),('deposit-foreign',estimated)]:
            if not amount:continue
            calls[action] = conn.compose_call(call_module='SubtensorModule',
                call_function='transfer_stake_and_hotkey',
                call_params=self.deposit_params(action,amount))
        fees = {name:self.fee(call) for name,call in calls.items()}
        self.funds(s.max_tao_rao, 2*sum(fees.values()))
        quote={'plan':fixed,'params':params,'quote_block':conn.get_block_number(head),
                'quote_block_hash':str(head),'estimated_output_rao':estimated,
                'fee_estimates_rao':fees,
                'note':'Fixed TAO input; foreign transfer amount comes only from finalized acquisition evidence. No scoring credit is established.'}
        if self.uses_proxy and not recovery and not getattr(self,'signer_owned',False):
            step=dict(action='purchase',amount_rao=s.max_tao_rao,params=params,quote_block=quote['quote_block'])
            try:checked=self.proxy_request(step,'check')
            except (Exception,Refusal):
                refuse('trusted signer policy check unavailable or refused; preview is not ready')
            if checked.get('status')!='checked':
                refuse('trusted signer has an existing or unresolved plan; inspect its original intent')
            quote['signer_policy_checked']=True
        return quote

    def deposit_params(self, action, amount):
        c,s = self.config,self.settings
        if action not in ('deposit-noah','deposit-foreign'):refuse('unsupported typed purchase action')
        foreign = action == 'deposit-foreign'
        n = s.foreign_netuid if foreign else c.netuid
        return dict(destination_coldkey=c.ark_coldkey,origin_hotkey=c.miner_hotkey,
                    destination_hotkey=s.foreign_ark_hotkey if foreign else c.validator_hotkey,
                    origin_netuid=n,destination_netuid=n,alpha_amount=amount)

    def prepare(self, action, amount, quote):
        self.identity()
        c,s,conn=self.config,self.settings,self.connection
        uint(amount,'step amount',1)
        self.root_check(action)
        if action == 'purchase':
            if amount != s.max_tao_rao or quote['plan'] != plan(c,s):
                refuse('purchase differs from its approved fixed plan')
            params = quote['params']
            function = 'add_stake_limit'
            if c.amount_rao+c.reserve_rao > self.available(c.netuid):
                refuse('fixed NOAH deposit is no longer available; purchase stopped')
            for n in (c.netuid,s.foreign_netuid):self.enabled(n)
            self.funds(amount, 2*sum(quote['fee_estimates_rao'].values()))
        else:
            netuid = c.netuid if action == 'deposit-noah' else s.foreign_netuid
            reserve = c.reserve_rao if action == 'deposit-noah' else 0
            self.enabled(netuid)
            if amount+reserve > self.available(netuid):refuse('fixed transfer is no longer available')
            if action == 'deposit-noah' and amount != c.amount_rao:refuse('NOAH amount differs from fixed plan')
            params = self.deposit_params(action,amount)
            function = 'transfer_stake_and_hotkey'
        step = dict(action=action,amount_rao=amount,function=function,params=params,
                    quote_block=quote['quote_block'],quote_params=quote['params'],
                    fee_estimates_rao=quote['fee_estimates_rao'],
                    estimated_output_rao=quote['estimated_output_rao'],
                    start_block=conn.get_block_number(conn.get_chain_finalised_head()))
        self.fresh(step)
        call=conn.compose_call(call_module='SubtensorModule',call_function=function,call_params=params)
        step['fee_estimate_rao']=self.fee(call)
        return step

    def fresh(self, step):
        """Recheck the allowed typed call immediately before signing and sending."""
        c,s,conn=self.config,self.settings,self.connection
        action=step['action']; amount=uint(step['amount_rao'],'step amount',1)
        self.root_check(action)
        if action == 'purchase':
            p=step['params']
            expected=dict(hotkey=c.miner_hotkey,netuid=s.foreign_netuid,
                          amount_staked=s.max_tao_rao,limit_price=p.get('limit_price'),allow_partial=False)
            uint(expected['limit_price'],'limit price',1)
            if s.foreign_netuid == 0 and expected['limit_price'] != 1_000_000_000:
                refuse('root purchase requires the exact one-to-one price limit')
            if step['function'] != 'add_stake_limit' or p != expected or amount != s.max_tao_rao:
                refuse('only the configured bounded TAO purchase can be signed')
            if p != step['quote_params']:refuse('purchase price differs from approved quote')
            age=conn.get_block_number(conn.get_chain_head())-step['quote_block']
            if not 0<=age<=s.quote_max_age_blocks:raise NotSubmitted('quote_expired')
            if c.amount_rao+c.reserve_rao > self.available(c.netuid):
                refuse('fixed NOAH deposit is no longer available; purchase stopped')
            for n in (c.netuid,s.foreign_netuid):self.enabled(n)
            # Re-estimate the whole sequence before unlocking/signing and again
            # before submission; a stale fee quote cannot consume survival funds.
            calls=[conn.compose_call(call_module='SubtensorModule',
                call_function='add_stake_limit',call_params=p)]
            for name,quantity in [('deposit-noah',c.amount_rao),
                                  ('deposit-foreign',step['estimated_output_rao'])]:
                if quantity:
                    calls.append(conn.compose_call(call_module='SubtensorModule',
                        call_function='transfer_stake_and_hotkey',
                        call_params=self.deposit_params(name,quantity)))
            self.funds(amount,2*sum(self.fee(call) for call in calls))
        else:
            if step['function'] != 'transfer_stake_and_hotkey' or step['params'] != self.deposit_params(action,amount):
                refuse('only the configured attributed transfer can be signed')
            if action=='deposit-noah' and amount != c.amount_rao:refuse('NOAH amount exceeds fixed plan')
            netuid=c.netuid if action=='deposit-noah' else s.foreign_netuid
            self.enabled(netuid)
            reserve=c.reserve_rao if action=='deposit-noah' else 0
            if amount+reserve > self.available(netuid):refuse('fixed transfer is no longer available')
            call=conn.compose_call(call_module='SubtensorModule',call_function=step['function'],
                                   call_params=step['params'])
            self.funds(0,2*self.fee(call))

    def reconcile(self, step):
        self.identity()
        if self.uses_proxy:
            return self.proxy_request(step,'status')
        self.validate_call(step)
        if step['action'] != 'purchase':
            result=super().reconcile(step)
            if result.get('status')=='finalized':
                result.update(self.receipt_context(result,step))
            return result
        conn=self.connection
        final=conn.get_block_number(conn.get_chain_finalised_head())
        for number in range(step['start_block'],min(final,step['start_block']+2*ERA)+1):
            block_hash=conn.get_block_hash(number)
            for index,extrinsic in enumerate(conn.get_block(block_hash=block_hash)['extrinsics']):
                raw_hash=getattr(extrinsic,'extrinsic_hash',None)
                if raw_hash is None:refuse('history must supply exact transaction hashes')
                if hash_text(raw_hash)!=step['extrinsic_hash']:continue
                value=unwrap(extrinsic); call=value.get('call',{})
                params={a['name']:a['value'] for a in call.get('call_args',[])}
                if (not same(value.get('address'),self.config.coldkey)
                        or call.get('call_module')!='SubtensorModule'
                        or call.get('call_function')!=step['function'] or params!=step['params']):
                    refuse('finalized purchase differs from the saved exact transaction')
                events=[unwrap(e) for e in conn.get_events(block_hash=block_hash)]
                own=[e.get('event',e) for e in events if e.get('extrinsic_idx')==index]
                result=dict(block_number=number,block_hash=str(block_hash),extrinsic_id=f'{number}-{index}')
                context=self.receipt_context(result,step)
                if any(e.get('module_id')=='System' and e.get('event_id')=='ExtrinsicFailed' for e in own):
                    return dict(result,**context,status='failed')
                actual=purchase_result(events,coldkey=self.config.coldkey,
                    hotkey=self.config.miner_hotkey,netuid=self.settings.foreign_netuid,
                    requested_rao=step['amount_rao'],extrinsic_index=index)
                return dict(result,**context,status='finalized',purchase_result=actual)
        return {'status':'uncertain'}

    def validate_call(self, step):
        """Receipt checks don't enforce quote freshness after a successful send."""
        action=step['action'];p=step['params'];amount=step['amount_rao']
        if action=='purchase':
            expected=dict(hotkey=self.config.miner_hotkey,netuid=self.settings.foreign_netuid,
                amount_staked=self.settings.max_tao_rao,limit_price=p.get('limit_price'),allow_partial=False)
            if step['function']!='add_stake_limit' or p!=expected or amount!=self.settings.max_tao_rao:
                refuse('saved purchase is not the allowed fixed call')
        elif (step['function']!='transfer_stake_and_hotkey'
                or p!=self.deposit_params(action,amount)
                or (action=='deposit-noah' and amount!=self.config.amount_rao)):
            refuse('saved transfer is not the allowed fixed call')

    def receipt_context(self, result, step):
        conn=self.connection;block=result['block_hash'];index=int(result['extrinsic_id'].split('-')[1])
        owner=unwrap(conn.query('SubtensorModule','Owner',[self.config.miner_hotkey],
                                block_hash=conn.get_block_hash(result['block_number']-1)))
        if not same(owner,self.config.coldkey):refuse('historical purchase owner changed')
        events=[dict(unwrap(e),ordinal_in_block=i) for i,e in enumerate(conn.get_events(block_hash=block))
                if unwrap(e).get('extrinsic_idx')==index]
        timestamp=uint(unwrap(conn.query('Timestamp','Now',block_hash=block)), 'block timestamp')
        return dict(genesis=self.config.genesis_hash,extrinsic_hash=step['extrinsic_hash'],
            extrinsic_index=index,events=events,block_timestamp_ms=timestamp,
            historical_owner=owner,origin_miner_hotkey=self.config.miner_hotkey)


def main(path, *, label, preview=False, status=False, journal_path=None, cancel_unsubmitted=False,
         recover_pre_submit=False):
    from pathlib import Path
    import json
    from .config import load_config
    from .purchase import load_settings
    from .purchase_workflow import PurchaseJournal, run
    from .cli import build_services, _close_quietly
    config,settings=load_config(path),load_settings(path)
    test_gate(config,settings)
    if cancel_unsubmitted:
        from types import SimpleNamespace
        from .ledger import Ledger
        services=SimpleNamespace(connection=None,ledger=Ledger(Path(config.state_dir)/'deposits.db'))
    else:services=build_services(config)
    journal=None
    try:
        journal=PurchaseJournal(journal_path or Path(config.state_dir)/'purchase')
        result=run(config,settings,Adapter(config,settings,services),journal,
                   label=label,preview=preview,status=status,cancel_unsubmitted=cancel_unsubmitted,
                   recover_pre_submit=recover_pre_submit)
        print(json.dumps(result,indent=2))
        return 0 if (result.get('preview') or result.get('delivery_complete') or result.get('closed_failed')
                     or result.get('recovered_pre_submit') or status) else 2
    finally:
        if journal is not None:journal.close()
        services.ledger.close()
        _close_quietly(services.connection)
