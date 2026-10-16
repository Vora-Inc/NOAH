"""Bounded proxy purchase requests, sharing the delegate's durable signer journal.

The signer policy fixes one plan, owner, miner, asset, receiver and TAO cap.
Client labels cannot authorize an additional purchase or repeat a delivery.
"""
from dataclasses import asdict
import time
import uuid

from ...chain.sources import Refusal
from .proxy_signing import (preflight, proxy_executed, reject, request_identity, wrap,
                            same, signing, uint, unwrap)
from .signer import ERA, digest, hash_text

ACTIONS=('purchase','purchase-noah','purchase-foreign')


def canonical_address(value):
    from ...chain.ss58 import public_key_of
    return public_key_of(value).hex()


def canonical_request(req):
    result=dict(req)
    identity=dict(result['identity'])
    for name in ('owner','miner','delegate','ark','validator'):
        identity[name]=canonical_address(identity[name])
    result['identity']=identity
    return result


def find(rows,signer,action):
    # Semantic lookup also recognizes intents written before canonical IDs.
    c=signer.config
    matches=[r for r in rows if r['genesis']==c.genesis_hash
             and same(r['owner'],c.coldkey) and same(r['miner'],c.miner_hotkey)
             and r['request'].get('intent_id')==signer.purchase.plan_id
             and r['request'].get('action')==action]
    if len(matches)>1:reject('multiple records bind this fixed plan; reconcile manually')
    return matches[0] if matches else None


def ident(signer,action):
    c=signer.config
    return digest(['fixed-purchase',c.genesis_hash,canonical_address(c.coldkey),canonical_address(c.miner_hotkey),
                   signer.purchase.plan_id,action])


def result(row):
    return {k:v for k,v in row.items() if k in (
        'status','extrinsic_hash','block_hash','block_number','extrinsic_id',
        'purchase_result','receipt','events','genesis','extrinsic_index',
        'block_timestamp_ms','historical_owner','origin_miner_hotkey',
        'journal_id','reservation_token','failure_stage','failure_code')}


def adapter(signer):
    from .purchase_runtime import Adapter
    # This read-only composition adapter never opens or owns delegate material.
    instance=Adapter(signer.config,signer.purchase,signer.services)
    instance.signer_owned=True  # Never recurse through this signer's socket.
    return instance


def calls(signer,req,rows):
    """Build only policy-derived calls; derive foreign amount from our own receipt."""
    a=adapter(signer)
    c,s=signer.config,signer.purchase
    action=req['action'];amount=uint(req['amount_rao'],'requested amount',1)
    if action=='purchase':
        if amount!=s.max_tao_rao:reject('purchase amount differs from immutable fixed policy')
        spent_noah=sum(r['request']['amount_rao'] for r in rows
            if r['genesis']==c.genesis_hash and same(r['owner'],c.coldkey)
            and same(r['miner'],c.miner_hotkey) and r['status']!='refused'
            and r['request']['action'] in ('deposit','convert','purchase-noah'))
        if c.amount_rao>signer.policy['max_amount_rao'] or spent_noah+c.amount_rao>signer.policy['max_total_rao']:
            reject('complete fixed NOAH leg exceeds signer amount or remaining lifetime cap')
        requested_block=uint(req['quote_block'],'quote block')
        age=signer.services.connection.get_block_number(signer.services.connection.get_chain_head())-requested_block
        if not 0<=age<=s.quote_max_age_blocks:reject('purchase quote expired')
        # One complete, fresh policy-derived quote per authority boundary. It
        # checks runtime/owner/root rules, both assets, available NOAH, exact-call
        # fees, delegations and owner/delegate funds. Nesting prepare()->fresh()
        # here repeated that same work twice and could expire a short quote.
        # Never reuse this quote across requests, key loading, signing or sending.
        quote=a.quote(limit_price=uint(req['limit_price'],'limit price',1))
        params=dict(quote['params'])
        step=dict(action='purchase',amount_rao=amount,function='add_stake_limit',params=params,
                  quote_block=requested_block,quote_params=dict(params),
                  fee_estimates_rao=dict(quote['fee_estimates_rao']),
                  estimated_output_rao=quote['estimated_output_rao'],
                  start_block=signer.services.connection.get_block_number(
                      signer.services.connection.get_chain_finalised_head()))
    else:
        prior=find(rows,signer,'purchase')
        if not prior:reject('purchase receipt is required before transfers')
        if prior['status'] in ('prepared','uncertain'):reconcile(signer,prior)
        if prior['status']!='finalized' or not prior.get('purchase_result'):
            reject('purchase is not finalized with exact acquisition evidence')
        if prior['policy_binding'] not in bindings(signer):reject('purchase was bound to different settings')
        foreign=action=='purchase-foreign'
        expected=prior['purchase_result']['foreign_received_rao'] if foreign else c.amount_rao
        if amount!=expected:reject('transfer differs from fixed NOAH or exact acquired alpha')
        if foreign and c.amount_rao:
            noah=find(rows,signer,'purchase-noah')
            if not noah or noah['status']!='finalized':reject('fixed NOAH transfer must finalize before foreign delivery')
        # Only the acquisition establishes the foreign amount. Existing stake
        # cannot authorize a larger transfer, even with a new client label.
        quote=dict(params=prior['step']['quote_params'],quote_block=prior['step']['quote_block'],
                   estimated_output_rao=expected,fee_estimates_rao={})
        step=a.prepare('deposit-foreign' if foreign else 'deposit-noah',amount,quote)
    conn=signer.services.connection
    inner=conn.compose_call(call_module='SubtensorModule',call_function=step['function'],call_params=step['params'])
    outer,facts=preflight(conn,c,inner,'purchase' if action=='purchase' else 'deposit')
    if 2*facts['fee_estimate_rao']>min(signer.policy['max_fee_rao'],s.max_fee_rao):
        reject('purchase proxy fee exceeds trusted bound')
    step['fee_estimate_rao']=facts['fee_estimate_rao']
    if action=='purchase':
        # Preserve the final exact outer-call check and account for a fee change
        # during quoting in the complete sequence's positive-balance check.
        fees=dict(step['fee_estimates_rao'],purchase=facts['fee_estimate_rao'])
        a.funds(amount,2*sum(fees.values()))
        age=conn.get_block_number(conn.get_chain_head())-step['quote_block']
        if not 0<=age<=s.quote_max_age_blocks:reject('purchase quote expired')
    return outer,step


def bindings(signer):
    c=asdict(signer.config);p=asdict(signer.purchase)
    legacy=digest({'config':c,'purchase':p})
    for name in ('coldkey','miner_hotkey','ark_coldkey','validator_hotkey'):
        c[name]=canonical_address(c[name])
    if (c.get('signing') or {}).get('proxy_address'):
        c['signing']['proxy_address']=canonical_address(c['signing']['proxy_address'])
    p['foreign_ark_hotkey']=canonical_address(p['foreign_ark_hotkey'])
    return {legacy,digest({'config':c,'purchase':p})}


def binding(signer):
    c=asdict(signer.config);p=asdict(signer.purchase)
    for name in ('coldkey','miner_hotkey','ark_coldkey','validator_hotkey'):
        c[name]=canonical_address(c[name])
    if (c.get('signing') or {}).get('proxy_address'):
        c['signing']['proxy_address']=canonical_address(c['signing']['proxy_address'])
    p['foreign_ark_hotkey']=canonical_address(p['foreign_ark_hotkey'])
    return digest({'config':c,'purchase':p})


def execute(signer,request):
    if signer.purchase is None:reject('purchase signer policy is not enabled')
    required={'version','action','intent_id','amount_rao','identity','operation','expires_at'}
    cancelling=request.get('operation') in ('cancel-check','cancel')
    if request.get('action')=='purchase' and not cancelling:required|={'limit_price','quote_block'}
    if cancelling:required|={'config_binding'}
    if request.get('operation')=='cancel':required|={'journal_id','policy_fingerprint','cancellation_token'}
    if request.get('operation') in ('submit','status','recover-pre-submit'):required|={'journal_id','reservation_token'}
    if (set(request)!=required or type(request.get('version')) is not int or request['version']!=1
            or request['action'] not in ACTIONS or request['operation'] not in ('check','prepare','submit','status','recover-pre-submit','cancel-check','cancel')):
        reject('only typed fixed purchase requests are accepted')
    expires=uint(request['expires_at'],'expiry')
    if not time.time()<expires<=time.time()+180:reject('purchase request expired')
    if request['intent_id']!=signer.purchase.plan_id:reject('request does not name trusted fixed plan')
    expected=dict(request,identity=request_identity(signer.config))
    if canonical_request(request)['identity']!=canonical_request(expected)['identity']:
        reject('request identity differs from signer policy')
    operation=request['operation']
    req={k:v for k,v in request.items() if k not in ('expires_at','operation','journal_id','reservation_token')}
    c=signer.config;j=signer.journal;conn=signer.services.connection;s=signing(c)
    uid=ident(signer,req['action'])
    with j.locked():
        rows=j.rows();old=find(rows,signer,req['action'])
        incarnation=j.incarnation()
        from .purchase_cancellation import tombstone, cancel
        saved=tombstone(signer)
        if cancelling:return cancel(signer,request,rows,saved,incarnation)
        if saved:reject('fixed purchase plan is permanently cancelled')
        if old:
            if (operation != 'recover-pre-submit' and canonical_request(old['request'])!=canonical_request(req)
                    or old['policy_binding'] not in bindings(signer)):
                reject('fixed plan action already binds different settings or amounts')
            if old.get('journal_id') is not None and old['journal_id']!=incarnation:
                reject('signer journal identity changed; investigate before retrying')
        if operation in ('submit','status','recover-pre-submit'):
            if (not old or request.get('journal_id')!=incarnation
                    or request.get('reservation_token')!=old.get('reservation_token')
                    or not old.get('reservation_token')):
                reject('durable signer reservation is absent or replaced; outcome requires investigation')
        if operation == 'recover-pre-submit':
            # The only supported recovery is a durable pre-broadcast row. The
            # signer writes nonce and hash before calling submit_extrinsic.
            # The installed predecessor's exact after_hash/quote_expired code
            # is emitted solely by its final local calls() check, before RPC.
            def signed_before_rpc(value):
                return (value.get('failure_code')=='quote_expired'
                    and value.get('failure_stage') in ('after_hash','signed_before_submit')
                    and value.get('submit_started') is None
                    and type(value.get('nonce')) is int and value['nonce']>=0
                    and isinstance(value.get('extrinsic_hash'),str)
                    and value['extrinsic_hash'].startswith('0x')
                    and len(value['extrinsic_hash'])==66
                    and all(c in '0123456789abcdef' for c in value['extrinsic_hash'][2:])
                    and all(value.get(k) is None for k in ('block_hash','events','receipt')))
            history=old.get('recovery_history',[])
            if (not isinstance(history,list) or any(
                    not isinstance(prior,dict)
                    or set(prior) not in ({'request','step','status'},
                        {'request','step','status','extrinsic_hash','nonce','failure_stage','failure_code'})
                    or not isinstance(prior['request'],dict) or not isinstance(prior['step'],dict)
                    or any(prior['step'].get(k) is not None for k in
                           ('nonce','extrinsic_hash','block_hash','events','receipt'))
                    or (not signed_before_rpc(prior) and any(
                        prior.get(k) is not None for k in ('nonce','extrinsic_hash','block_hash','events','receipt')))
                    for prior in history)):
                reject('original recovery history is not proven pre-submission')
            current_signed=signed_before_rpc(old)
            if (req['action'] != 'purchase' or old['status'] not in ('uncertain','not_submitted')
                    or (not current_signed and any(old.get(k) is not None for k in
                        ('nonce','extrinsic_hash','block_hash','events','receipt','submit_started')))
                    or not isinstance(old.get('step'),dict)
                    or old['step'].get('action') != 'purchase'
                    or old['step'].get('amount_rao') != req['amount_rao']
                    or old['step'].get('quote_block') != old['request'].get('quote_block')):
                reject('original purchase is not proven pre-submission')
            prior=dict(old['request']); prior.update(limit_price=req['limit_price'],quote_block=req['quote_block'])
            if canonical_request(prior)!=canonical_request(req):
                reject('recovery changes fixed purchase identity or amount')
            other_rows=[r for r in rows if r['id']!=old['id']]
            if any(r['genesis']==c.genesis_hash and
                   (same(r['owner'],c.coldkey) or same(r['delegate'],s['proxy_address'])) and
                   r['status'] in ('not_submitted','prepared','uncertain') for r in other_rows):
                reject('owner or delegate has another unresolved transaction')
            spent=sum(r['request']['amount_rao'] for r in other_rows
                if r['genesis']==c.genesis_hash and same(r['owner'],c.coldkey)
                and same(r['miner'],c.miner_hotkey) and r['status']!='refused'
                and r['request']['action']=='purchase')
            if spent+req['amount_rao']>signer.policy['max_total_tao_rao']:
                reject('lifetime TAO purchase cap exceeded')
            signer.runtime()
            _,step=calls(signer,req,other_rows)
            old.setdefault('recovery_history',[]).append(dict(
                request=old['request'],step=old['step'],status=old['status'],
                **({k:old[k] for k in ('extrinsic_hash','nonce','failure_stage','failure_code')}
                   if current_signed else {})))
            for key in ('extrinsic_hash','nonce','failure_stage','failure_code'):
                old.pop(key,None)
            old.update(request=req,step=step,start_block=step['start_block'],status='not_submitted')
            j.put(old)
            return result(old)
        if old and (operation not in ('submit','check') or old['status']!='not_submitted'):
            return reconcile(signer,old)
        # Ignore our own never-submitted reservation when refreshing it; every
        # other reservation blocks competing owner/delegate requests.
        other_rows=[r for r in rows if not old or r['id']!=old['id']]
        if any(r['genesis']==c.genesis_hash and
               (same(r['owner'],c.coldkey) or same(r['delegate'],s['proxy_address'])) and
               r['status'] in ('not_submitted','prepared','uncertain') for r in other_rows):
            reject('owner or delegate has a reserved or unresolved transaction')
        amount=uint(req['amount_rao'],'amount',1)
        owner_rows=[r for r in other_rows if r['genesis']==c.genesis_hash and same(r['owner'],c.coldkey)
                    and same(r['miner'],c.miner_hotkey) and r['status']!='refused']
        if req['action']=='purchase':
            spent=sum(r['request']['amount_rao'] for r in owner_rows if r['request']['action']=='purchase')
            if spent+amount>signer.policy['max_total_tao_rao']:reject('lifetime TAO purchase cap exceeded')
        elif req['action']=='purchase-noah':
            spent=sum(r['request']['amount_rao'] for r in owner_rows
                      if r['request']['action'] in ('deposit','convert','purchase-noah'))
            if amount>signer.policy['max_amount_rao'] or spent+amount>signer.policy['max_total_rao']:
                reject('NOAH signer amount or lifetime cap exceeded')
        signer.runtime()
        if operation=='submit' and req['action']=='purchase':
            # PREPARE already saved the typed call. The guarded signature and
            # pre-submit boundaries below each rebuild and fully check it.
            # Requoting here only consumes the small quote-age window.
            step=dict(old['step'])
            if (step.get('action')!='purchase' or step.get('amount_rao')!=req['amount_rao']
                    or step.get('quote_block')!=req['quote_block']
                    or step.get('params',{}).get('limit_price')!=req['limit_price']
                    or step.get('function')!='add_stake_limit'):
                reject('durable purchase reservation differs from typed request')
            step['start_block']=conn.get_block_number(conn.get_chain_finalised_head())
            inner=conn.compose_call(call_module='SubtensorModule',call_function='add_stake_limit',
                                    call_params=step['params'])
            outer=wrap(conn,c,inner,'purchase')
        else:
            outer,step=calls(signer,req,other_rows)
        if time.time()>=expires:reject('purchase request expired during checks')
        if operation=='check':return {'status':'checked','runtime_permission_verified':True}
        if operation=='prepare':
            row=dict(id=uid,request=req,genesis=c.genesis_hash,owner=c.coldkey,miner=c.miner_hotkey,
                netuid=c.netuid,delegate=s['proxy_address'],policy_binding=binding(signer),
                status='not_submitted',start_block=step['start_block'],step=step,
                journal_id=incarnation,reservation_token=uuid.uuid4().hex)
            j.put(row)
            return result(row)
        # Only a matching durable reservation can reach the key loader. A
        # prepared legacy row without a hash is never downgraded to retryable.
        row=old
        row.update(status='prepared',start_block=step['start_block'],step=step)
        j.put(row)
        try:
            key=signer.key_loader(c)
            if not same(key.ss58_address,s['proxy_address']):reject('delegate key mismatch')
        except (Exception,Refusal):
            row['status']='refused';j.put(row);return result(row)
        try:
            # The policy-derived outer call above is immutable. The signing
            # callback and the pre-submit boundary each recheck live policy;
            # another full quote here only ages the approved quote.
            nonce=conn.get_account_next_index(s['proxy_address'])
            class GuardedKey:
                def __getattr__(self,name):return getattr(key,name)
                def sign(self,payload):
                    if time.time()>=expires:reject('purchase request expired before signature')
                    signer.runtime(require_context=True)
                    _,checked=calls(signer,req,rows)
                    if (checked['function']!=step['function'] or checked['params']!=step['params']):
                        reject('signature call differs from trusted fresh purchase')
                    if time.time()>=expires:reject('purchase request expired during signature checks')
                    return key.sign(payload)
            from ..deposit_submission import BytesConvertingSigner
            signed=conn.create_signed_extrinsic(call=outer,keypair=BytesConvertingSigner(GuardedKey()),
                nonce=nonce,era={'period':ERA,'current':row['start_block']})
            row.update(extrinsic_hash=hash_text(signed.extrinsic_hash),nonce=nonce)
            j.put(row)  # Durable identity before any submission RPC.
            signer.runtime(require_context=True)
            calls(signer,req,rows)
            if time.time()>=expires:reject('purchase request expired before submission')
            row['submit_started']=True
            j.put(row)  # Durable boundary before the submission RPC.
            conn.submit_extrinsic(signed,wait_for_inclusion=True,wait_for_finalization=True)
            return reconcile(signer,row)
        except (Exception,Refusal) as error:
            reason=str(error) if isinstance(error,Refusal) else ''
            code=('quote_expired' if reason=='purchase quote expired' else
                  'request_expired' if reason.startswith('purchase request expired') else
                  'pre_submit_check_refused' if isinstance(error,Refusal) else 'pre_submit_check_unavailable')
            stage=('after_submit_started' if row.get('submit_started') else
                   'signed_before_submit' if 'extrinsic_hash' in row else 'before_hash')
            row.update(status='uncertain',failure_stage=stage,
                       failure_code=code)
            j.put(row);return result(row)


def _params(call):
    call=unwrap(call)
    return {a['name']:a['value'] for a in call.get('call_args',[])}


def reconcile(signer,row):
    if row['status'] not in ('prepared','uncertain'):return result(row)
    conn=signer.services.connection
    if str(conn.get_block_hash(0))!=row['genesis']:reject('wrong chain for purchase reconciliation')
    final=conn.get_block_number(conn.get_chain_finalised_head())
    for number in range(row['start_block'],min(final,row['start_block']+2*ERA)+1):
        block=conn.get_block_hash(number)
        for index,extrinsic in enumerate(conn.get_block(block_hash=block)['extrinsics']):
            value=unwrap(extrinsic)
            txhash=getattr(extrinsic,'extrinsic_hash',None) or value.get('extrinsic_hash')
            if txhash is None:reject('history lacks exact transaction hashes')
            if hash_text(txhash)!=row.get('extrinsic_hash'):continue
            call=value.get('call',{});outer=_params(call);inner=unwrap(outer.get('call',{}))
            proxy_type='Staking' if row['request']['action']=='purchase' else 'Transfer'
            if (not same(value.get('address'),row['delegate']) or call.get('call_module')!='Proxy'
                    or call.get('call_function')!='proxy' or not same(outer.get('real'),row['owner'])
                    or outer.get('force_proxy_type')!=proxy_type
                    or inner.get('call_module')!='SubtensorModule'
                    or inner.get('call_function')!=row['step']['function']
                    or _params(inner)!=row['step']['params']):
                reject('proxy transaction differs from the fixed typed intent')
            events=[unwrap(e) for e in conn.get_events(block_hash=block)]
            own=[dict(e,ordinal_in_block=i) for i,e in enumerate(events) if e.get('extrinsic_idx')==index]
            system=[e.get('event',e).get('event_id') for e in own if e.get('event',e).get('module_id')=='System']
            outcome=proxy_executed(own)
            row.update(block_hash=str(block),block_number=number,extrinsic_id=f'{number}-{index}',
                       extrinsic_index=index,events=own)
            if 'ExtrinsicFailed' in system or outcome is False:
                row['status']='failed';signer.journal.put(row);return result(row)
            if system.count('ExtrinsicSuccess')!=1 or outcome is not True:
                row['status']='uncertain';signer.journal.put(row);return result(row)
            owner=unwrap(conn.query('SubtensorModule','Owner',[row['miner']],block_hash=conn.get_block_hash(number-1)))
            if not same(owner,row['owner']):reject('historical owner differs from intended miner owner')
            row.update(historical_owner=owner,origin_miner_hotkey=row['miner'],
                       block_timestamp_ms=uint(unwrap(conn.query('Timestamp','Now',block_hash=block)),'block timestamp'))
            if row['request']['action']=='purchase':
                from .purchase import purchase_result
                row['purchase_result']=purchase_result(events,coldkey=row['owner'],hotkey=row['miner'],
                    netuid=row['step']['params']['netuid'],requested_rao=row['request']['amount_rao'],extrinsic_index=index)
            else:
                from .reconcile import deposits_in
                params=row['step']['params']
                hotkey=params['destination_hotkey'];netuid=params['destination_netuid']
                matches=[d for d in deposits_in(conn,ark_coldkey=params['destination_coldkey'],validator_hotkey=hotkey,block=number)
                         if d.extrinsic_index==index]
                if len(matches)!=1:reject('proxy transfer receipt missing or ambiguous')
                d=matches[0]
                if (d.unattributed_reason is not None or not same(d.coldkey,row['owner'])
                        or not same(d.origin_hotkey,row['miner']) or d.token_netuid!=netuid
                        or d.amount_rao!=row['request']['amount_rao']):reject('proxy transfer attribution differs')
                row['receipt']=asdict(d)
            row['status']='finalized';signer.journal.put(row);return result(row)
    # Expiry alone does not grant a fresh request or drop its lifetime reservation.
    row['status']='uncertain';signer.journal.put(row);return result(row)
