"""Only a fully validated public cache index label loses credential ambiguity."""
import copy
import hashlib
import json

import pytest
from noah.secret_scan import _points_cache_labels, scan_text


def cache():
    rows=[dict(start=86400,end=172800,noah_price_rao=2_000_000_000,asset_price_rao=3_000_000_000)]
    return dict(identity=dict(genesis='0x'+'1'*64,kind='day',
        key=[2,3,86400,172800,24,'0x'+'2'*64,48,'0x'+'3'*64]),value=rows,
        sha256=hashlib.sha256(encoded(rows).encode()).hexdigest())


def encoded(doc):return json.dumps(doc, sort_keys=True, indent=2)+'\n'


def test_real_short_cache_index_hash_is_not_a_private_key():
    text=encoded(cache());changed=_points_cache_labels(text)
    assert changed!=text
    assert changed.replace('"idx": [','"key": [')==text
    assert len(changed)==len(text)
    assert not scan_text(text)


@pytest.mark.parametrize('damage',['extra','wrong-hash','gap','negative-price','wrong-width',
                                  'wrong-kind','wrong-subnet','bool','unknown-row','noncanonical','duplicate'])
def test_malformed_or_noncanonical_cache_keeps_all_scanner_context(damage):
    doc=copy.deepcopy(cache())
    if damage=='extra':doc['private_key']='0x'+'4'*64
    elif damage=='wrong-hash':doc['sha256']='0'*64
    elif damage=='gap':doc['value'][0]['start']+=1
    elif damage=='negative-price':doc['value'][0]['noah_price_rao']=-1
    elif damage=='wrong-width':doc['identity']['key'][5]='0x1234'
    elif damage=='wrong-kind':doc['identity']['kind']='private'
    elif damage=='wrong-subnet':doc['identity']['key'][0]=65536
    elif damage=='bool':doc['identity']['key'][0]=True
    elif damage=='unknown-row':doc['value'][0]['private_key']='0x'+'4'*64
    text=encoded(doc)
    if damage=='noncanonical':text=json.dumps(doc)
    if damage=='duplicate':text=text.replace('"kind": "day"','"kind": "day", "kind": "day"')
    assert _points_cache_labels(text)==text


def test_credential_and_phrase_scans_are_not_whole_file_exempted():
    doc=cache();doc['private_key']='0x'+'4'*64
    assert any(f.kind=='key-hex' for f in scan_text(encoded(doc)))
    text = encoded(cache()) + ''.join(('private', '_key')) + ' = 0x' + '5'*64 + '\n'
    assert any(f.kind=='key-hex' for f in scan_text(text))
