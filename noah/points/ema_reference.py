"""Receipt-parent EMA/spot evidence. No daily history or mutable price oracle."""
import hashlib
import json
from pathlib import Path

from ..chain.sources import Unverified
from ..chain.ema_context import EMA_CODES as CODES
from ..core.calculation_record import durable_write, encoded
from .eligibility import generation, require
from .ema_policy import EMA_SOURCES
from .migration import hash32
from .pricing import RationalPrice, ema_spot_credit

SCHEMA = 'noah-token-ema-reference/1'
METHOD = 'chain-ema-spot-minimum/1'
FIELDS = {'schema','method','ema_source','genesis','native_netuid','asset_netuid',
          'receipt','reference','runtime','parent_runtime','metadata_sha256',
          'receipt_native','receipt_asset','parent_native','parent_asset',
          'ema_raw','spot_raw','prices','status','reason'}
MAX_SIZE = 65536


def digest(document):
    return hashlib.sha256(encoded(document)).hexdigest()


def price(raw, *, ema_source=None):
    if raw is None:
        if ema_source != 'SubnetMovingPrice':
            raise Unverified('missing optional EMA cannot establish reference')
        value = 0
    else:
        import re
        width = 16 if ema_source else 8
        require(type(raw) is str and re.fullmatch('0x[0-9a-f]{'+str(width*2)+'}',raw))
        value = int.from_bytes(bytes.fromhex(raw[2:]),'little',signed=ema_source=='SubnetMovingPrice')
    if value < 0:
        raise Unverified('negative EMA reference')
    denominator = (2**32 if ema_source=='SubnetMovingPrice' else 2**64) if ema_source else 10**9
    return {'numerator':str(value),'denominator':str(denominator)}


def lifecycle_reason(document):
    block = document['receipt']['block']
    reasons=[]
    for role in ('native','asset'):
        if role=='asset' and document['asset_netuid']==0:
            require(document['receipt_asset'] is None and document['parent_asset'] is None)
            continue
        current = document['receipt_'+role]; previous = document['parent_'+role]
        present, marker = generation(current)
        before, old = generation(previous)
        require(marker[0] <= block and old[0] <= block-1, 'future token generation anchor')
        if not present:
            raise Unverified('receipt token identity not established')
        if not before:
            reasons.append('reference-subnet-absent')
            continue
        if marker != old or marker[0] >= block:
            raise Unverified('same-block token incarnation is ambiguous')
    return reasons[0] if reasons else None


def validate_reference_proof(document):
    require(type(document) is dict and set(document)==FIELDS)
    require(document['schema']==SCHEMA and document['method']==METHOD and document['ema_source'] in EMA_SOURCES)
    require(hash32(document['genesis']))
    for name in ('native_netuid','asset_netuid'):
        require(type(document[name]) is int and 0<=document[name]<65536)
    require(document['native_netuid']>0 and document['native_netuid']!=document['asset_netuid'])
    receipt=document['receipt'];reference=document['reference']
    require(type(receipt) is dict and set(receipt)=={'block','block_hash','parent_hash','timestamp'})
    require(type(receipt['block']) is int and 0<receipt['block']<2**53 and type(receipt['timestamp']) is int and receipt['timestamp']>=0)
    require(hash32(receipt['block_hash']) and hash32(receipt['parent_hash']))
    require(type(reference) is dict and type(reference.get('block')) is int and reference=={'block':receipt['block']-1,'block_hash':receipt['parent_hash']})
    for name in ('runtime','parent_runtime'):
        value=document[name]
        require(type(value) is dict and set(value)=={'spec_version','code_hash','transaction_version'})
        require(type(value['spec_version']) is int and value['spec_version'] in CODES and value['code_hash']==CODES[value['spec_version']] and type(value['transaction_version']) is int and value['transaction_version']==1)
    import re
    require(type(document['metadata_sha256']) is str and re.fullmatch('[0-9a-f]{64}',document['metadata_sha256']))
    reason=lifecycle_reason(document)
    if reason:
        require(document['ema_raw'] is None and document['spot_raw'] is None and document['prices'] is None)
    else:
        require(type(document['ema_raw']) is dict and set(document['ema_raw'])=={'native','asset'})
        require(type(document['spot_raw']) is dict and set(document['spot_raw'])=={'native','asset'})
        values={}
        for role in ('native','asset'):
            for kind in ('ema','spot'):
                raw=document[kind+'_raw'][role]
                if role=='asset' and document['asset_netuid']==0:
                    require(raw is None);value={'numerator':'1','denominator':'1'}
                else:value=price(raw,ema_source=document['ema_source'] if kind=='ema' else None)
                values[kind+'_'+role]=value
        require(document['prices']==values,'price representation differs from authenticated raw value')
        if any(values[k+'_native']['numerator']=='0' for k in ('ema','spot')):
            reason='native-reference-price-zero'
        elif any(values[k+'_asset']['numerator']=='0' for k in ('ema','spot')):
            reason='foreign-reference-price-zero'
    require(document['status']==('unsupported' if reason else 'supported') and document['reason']==reason)
    require(len(encoded(document))<MAX_SIZE)
    return digest(document)


def credit(document, amount):
    validate_reference_proof(document)
    if document['status']=='unsupported':return 0
    values={k:RationalPrice(int(v['numerator']),int(v['denominator'])) for k,v in document['prices'].items()}
    return ema_spot_credit(amount,document['asset_netuid'],native_netuid=document['native_netuid'],
        ema_asset=values['ema_asset'],ema_noah=values['ema_native'],
        spot_asset=values['spot_asset'],spot_noah=values['spot_native'])


class EmaReference:
    def __init__(self, chain, directory, native_netuid, ema_source):
        require(ema_source in EMA_SOURCES)
        self.chain,self.root,self.native,self.source=chain,Path(directory),native_netuid,ema_source
        self.reset()

    def reset(self):
        self.results={}
        self.documents={}

    def precollect(self, receipts):
        groups={}
        for block,asset in receipts:groups.setdefault(block,set()).add(asset)
        for block,assets in sorted(groups.items()):self._collect(block,assets)

    def _collect(self, block, assets):
        from .eligibility import compact
        assets=set(assets)-{self.native}
        if all((block,a) in self.results for a in assets):return
        wanted=sorted((assets|{self.native})-{0})
        chain=self.chain
        require(type(block) is int and 0<block<=chain.head_block())
        at=chain.block_hash(block);parent=chain.block_hash(block-1)
        current=chain.token_reference_context(block,wanted)
        before=chain.token_reference_context(block-1,wanted)
        require(current['block_hash']==at and current['parent_hash']==parent and before['block_hash']==parent)
        documents={}
        for asset in sorted(assets):
            doc=dict(schema=SCHEMA,method=METHOD,ema_source=self.source,genesis=chain.genesis_hash(),
                native_netuid=self.native,asset_netuid=asset,
                receipt=dict(block=block,block_hash=at,parent_hash=parent,timestamp=chain.block_timestamp(block)),
                reference=dict(block=block-1,block_hash=parent),runtime=current['runtime'],parent_runtime=before['runtime'],
                metadata_sha256=before['metadata_sha256'],receipt_native=compact(current,self.native),
                parent_native=compact(before,self.native),receipt_asset=None if asset==0 else compact(current,asset),
                parent_asset=None if asset==0 else compact(before,asset),ema_raw=None,spot_raw=None,prices=None,
                status='unsupported',reason=None)
            doc['reason']=lifecycle_reason(doc)
            documents[asset]=doc
        supported=[a for a,d in documents.items() if not d['reason']]
        if supported:
            snapshot=chain.ema_price_snapshot(block-1,sorted((set(supported)|{self.native})-{0}),expected_block_hash=parent,ema_source=self.source)
            # The price reader authenticates the same parent state, not a newer quote.
            require(snapshot['block_hash']==parent and snapshot['runtime']==before['runtime'] and snapshot['metadata_sha256']==before['metadata_sha256'])
            for asset in supported:
                doc=documents[asset];values={};ema_raw={};spot_raw={}
                for role,netuid in (('native',self.native),('asset',asset)):
                    for kind in ('ema','spot'):
                        raw=None if netuid==0 else snapshot['prices'][str(netuid)][kind]['raw']
                        (ema_raw if kind=='ema' else spot_raw)[role]=raw
                        values[kind+'_'+role]={'numerator':'1','denominator':'1'} if netuid==0 else price(raw,ema_source=self.source if kind=='ema' else None)
                doc.update(ema_raw=ema_raw,spot_raw=spot_raw,prices=values,status='supported')
                if any(values[k+'_native']['numerator']=='0' for k in ('ema','spot')):doc.update(status='unsupported',reason='native-reference-price-zero')
                elif any(values[k+'_asset']['numerator']=='0' for k in ('ema','spot')):doc.update(status='unsupported',reason='foreign-reference-price-zero')
        require(chain.block_hash(block)==at and chain.block_hash(block-1)==parent)
        for asset,doc in documents.items():
            sha=validate_reference_proof(doc);reference={'path':'points-reference-proofs/'+sha+'.json','sha256':sha}
            durable_write(self.root/reference['path'],encoded(doc))
            self.documents[sha]=doc
            self.results[block,asset]=dict(status=doc['status'],reason=doc['reason'],proof=reference,document=doc)

    def result(self, block, asset):
        if (block,asset) not in self.results:self._collect(block,{asset})
        return self.results[block,asset]


def validate_fixture(fixture):
    """Independent scorer boundary: every new credit is linked to exact proof bytes."""
    from .receipts import validate
    import re
    boundary=fixture.get('pointsPricingBoundary');proofs=fixture.get('pointsPricing')
    require(type(boundary) is dict and set(boundary)=={'anchor','effectiveBlock','policySha256','predecessorPolicySha256','emaSource','genesis','nativeNetuid'})
    anchor=boundary['anchor']
    require(type(anchor) is dict and set(anchor)=={'block','hash'} and type(anchor['block']) is int and 0<anchor['block']<2**53-1 and hash32(anchor['hash']))
    require(type(boundary['effectiveBlock']) is int and boundary['effectiveBlock']==anchor['block']+1)
    require(boundary['emaSource'] in EMA_SOURCES and hash32(boundary['genesis']))
    require(type(boundary['nativeNetuid']) is int and 0<boundary['nativeNetuid']<65536)
    for key in ('policySha256','predecessorPolicySha256'):
        require(type(boundary[key]) is str and re.fullmatch('[0-9a-f]{64}',boundary[key]))
    require(type(proofs) is dict)
    used=set()
    for row in validate(fixture['pointsReceipts'],ema=True):
        if row['block']<boundary['effectiveBlock']:
            require(row['reason']!='ema-spot-reference','old receipt cannot use new pricing')
            continue
        reference=row['reference_proof'];sha=reference['sha256']
        require(sha in proofs,'missing embedded EMA reference')
        proof=proofs[sha];require(validate_reference_proof(proof)==sha)
        require(proof['genesis']==boundary['genesis'] and proof['native_netuid']==boundary['nativeNetuid'] and proof['ema_source']==boundary['emaSource'])
        require(proof['asset_netuid']==row['token_netuid'] and proof['receipt']['block']==row['block'] and proof['receipt']['block_hash']==row['block_hash'])
        if row['block']==boundary['effectiveBlock']:
            require(proof['receipt']['parent_hash']==anchor['hash'])
        require(row['receipt_id']==f"{proof['genesis']}/{row['block_hash']}/{row['event_ordinal']}")
        require(row['day']==(proof['receipt']['timestamp']-int(fixture['dayEpochTs']))//86400)
        require(row['credit_rao']==str(credit(proof,int(row['amount_rao']))))
        require(row['status']==('accepted' if proof['status']=='supported' else 'unsupported'))
        require(row['reason']==('ema-spot-reference' if proof['status']=='supported' else proof['reason']))
        used.add(sha)
    require(set(proofs)==used,'unused or missing EMA proof population')
    return True


def verify_dispositions(directory, fixture, policy, *, read_proof=None):
    from .eligibility import verify_dispositions as verify_old
    from .ema_policy import fixture_boundary
    validate_fixture(fixture)
    require(fixture['pointsPricingBoundary']==fixture_boundary(policy.ema_document,policy.manifest['netuid']))
    old=[r for r in fixture['pointsReceipts'] if r['block']<policy.ema_document['effective_block']]
    verify_old(directory,old,policy,read_proof=read_proof)
    root=Path(directory) if directory is not None else None
    for sha,doc in fixture['pointsPricing'].items():
        ref={'path':'points-reference-proofs/'+sha+'.json','sha256':sha}
        if read_proof is None:
            path=root/ref['path'];require(path.is_file() and not path.is_symlink() and path.stat().st_size<MAX_SIZE)
            data=path.read_bytes()
        else:data=read_proof(ref)
        require(type(data) is bytes and data==encoded(doc),'retained and embedded EMA proof differ')
    return True
