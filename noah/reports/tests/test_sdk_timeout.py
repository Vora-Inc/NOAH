"""Real pinned SDK/socket IO over local socketpairs; no chain, wallet or network."""
from contextlib import contextmanager
import json
import signal
import socket
import threading
import time
from types import SimpleNamespace

import pytest

from noah.reports.backing.sdk_client import live_sdk, ReportRpcTimeout


@contextmanager
def watchdog():
    def alarm(*_):
        raise AssertionError('transport did not obey its deadline')
    original = signal.signal(signal.SIGALRM, alarm)
    signal.setitimer(signal.ITIMER_REAL, 3)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, original)


@pytest.fixture
def pair():
    client, server = socket.socketpair()
    stop = threading.Event()
    workers = []
    def feed(chunks, delay=.005):
        def run():
            try:
                for chunk in chunks:
                    if stop.wait(delay):
                        break
                    server.sendall(chunk)
            except OSError:
                pass
        thread = threading.Thread(target=run, daemon=True)
        workers.append(thread); thread.start()
    try:
        yield client, feed
    finally:
        stop.set(); client.close(); server.close()
        for thread in workers:
            thread.join(1)
            assert not thread.is_alive()


def ws_client(monkeypatch, pair, *, auto_discover=False):
    from websocket import WebSocket
    raw, feed = pair
    def connect(ws, url, **_):
        ws.sock = raw
        ws.connected = True
        ws.sock.settimeout(ws.gettimeout())
    monkeypatch.setattr(WebSocket, 'connect', connect)
    # Full-suite SCALE registry loading can exceed 80 ms before network I/O.
    # One second leaves constructor CPU headroom; drip tests run beyond it.
    return live_sdk('ws://synthetic.invalid', auto_discover=auto_discover, timeout_seconds=1.0)


def frame(body):
    from websocket import ABNF
    value = ABNF.create_frame(json.dumps(body), ABNF.OPCODE_TEXT)
    value.mask = 0
    return value.format()


@pytest.mark.parametrize('mode', ['silent', 'irrelevant_messages', 'drip_frame'])
def test_ws_deadline_includes_partial_frames_and_irrelevant_messages(monkeypatch, pair, mode):
    sdk = ws_client(monkeypatch, pair)
    raw, feed = pair
    if mode == 'irrelevant_messages':
        feed([frame({'id':999,'result':None})]*1000)
    elif mode == 'drip_frame':
        feed([bytes([byte]) for byte in frame({'id':1,'result':'x'*1000})])
    started = time.monotonic()
    with watchdog(), pytest.raises(ReportRpcTimeout):
        sdk.rpc_request('chain_getFinalizedHead', [])
    assert time.monotonic()-started < 1.2
    assert raw.fileno() == -1
    with pytest.raises(Exception, match='closed'):
        sdk.rpc_request('chain_getFinalizedHead', [])


def test_ws_success_uses_real_sdk_decoder(monkeypatch, pair):
    sdk = ws_client(monkeypatch, pair)
    pair[1]([frame({'jsonrpc':'2.0','id':1,'result':'accepted'})])
    try:
        assert sdk.rpc_request('chain_getFinalizedHead', [])['result']=='accepted'
        assert sdk.config['auto_reconnect'] is False
    finally:
        sdk.close()
    assert pair[0].fileno()==-1


def test_constructor_failure_closes_socket(monkeypatch, pair):
    with watchdog(), pytest.raises(ReportRpcTimeout):
        ws_client(monkeypatch, pair, auto_discover=True)
    assert pair[0].fileno()==-1


def http_client(monkeypatch, pair, *, auto_discover=False):
    from urllib3.connection import HTTPConnection
    monkeypatch.setenv('NO_PROXY','*')
    monkeypatch.setattr(HTTPConnection, '_new_conn', lambda _: pair[0])
    return live_sdk('http://127.0.0.1:1', auto_discover=auto_discover, timeout_seconds=1.0)


@pytest.mark.parametrize('mode', ['silent', 'drip_headers', 'drip_body'])
def test_http_deadline_includes_headers_and_body(monkeypatch, pair, mode):
    sdk = http_client(monkeypatch, pair)
    if mode == 'drip_headers':
        pair[1]([bytes([byte]) for byte in b'HTTP/1.1 200 OK\r\nX-Drip: '+b'x'*1000+b'\r\nContent-Length: 900\r\n\r\n'])
    elif mode == 'drip_body':
        pair[1]([b'HTTP/1.1 200 OK\r\nContent-Length: 900\r\n\r\n']+[b' ']*1000)
    started=time.monotonic()
    with watchdog(), pytest.raises(ReportRpcTimeout):
        sdk.rpc_request('chain_getFinalizedHead', [])
    assert time.monotonic()-started < 1.2
    assert pair[0].fileno()==-1


def test_http_success_and_session_cleanup(monkeypatch, pair):
    sdk=http_client(monkeypatch,pair)
    body=json.dumps({'jsonrpc':'2.0','id':1,'result':'accepted'}).encode()
    pair[1]([b'HTTP/1.1 200 OK\r\nContent-Length: '+str(len(body)).encode()+b'\r\n\r\n'+body])
    try:
        assert sdk.rpc_request('chain_getFinalizedHead', [])['result']=='accepted'
    finally:
        sdk.close()
    assert pair[0].fileno()==-1


def test_chain_runtime_init_failure_closes_owned_sdk(monkeypatch):
    from noah.reports.backing.chain_session import SubstrateChain
    closed=[]
    def fail():raise RuntimeError('synthetic init failure')
    monkeypatch.setattr('noah.reports.backing.sdk_client.live_sdk',
                        lambda _:SimpleNamespace(init_runtime=fail,close=lambda:closed.append(True)))
    with pytest.raises(RuntimeError,match='synthetic init'):
        SubstrateChain('ws://synthetic.invalid')
    assert closed==[True]


@pytest.mark.parametrize('updates',['silent','incomplete','unrelated'])
def test_broadcast_timeout_retains_unknown_journal_without_resigning(monkeypatch,pair,tmp_path,updates):
    from noah.reports.tests.test_submit import FakeChain,_commit
    from noah.reports.backing.submit import SubmitError
    sdk=ws_client(monkeypatch,pair)
    chain=FakeChain(); signed=[]; sent=[]
    from scalecodec.base import ScaleBytes
    from scalecodec.types import GenericExtrinsic
    if updates!='silent':
        update={'subscription':'sub' if updates=='incomplete' else 'other','result':{'ready':True}}
        pair[1]([frame({'jsonrpc':'2.0','id':1,'result':'sub'})]+
                [frame({'jsonrpc':'2.0','method':'author_extrinsicUpdate','params':update})]*1000)
    original=chain.sign
    def sign(*args):
        signed.append(True)
        tx_hash,_=original(*args)
        return tx_hash,GenericExtrinsic(data=ScaleBytes('0x0000'))
    def broadcast(handle):
        sent.append(True)
        return sdk.submit_extrinsic(handle,wait_for_inclusion=True)
    chain.sign=sign;chain.broadcast=broadcast
    with watchdog(),pytest.raises(SubmitError,match='outcome unknown'):
        _commit(tmp_path,chain)
    journal=json.loads((tmp_path/'evidence/commit.journal').read_text())
    assert journal['state']=='unresolved' and journal['extrinsic_hash']
    with pytest.raises(SubmitError,match='outcome unknown'):
        _commit(tmp_path,chain)
    assert signed==[True] and sent==[True]
    after=json.loads((tmp_path/'evidence/commit.journal').read_text())
    assert after['state']=='unresolved' and after['extrinsic_hash']==journal['extrinsic_hash']

@pytest.mark.parametrize('status',[307,308])
def test_http_redirect_does_not_replay_rpc_post(monkeypatch,pair,status):
    from substrateinterface.exceptions import SubstrateRequestException
    sdk=http_client(monkeypatch,pair)
    calls=[]
    # Session is installed lazily on the first SDK RPC; observe all prepared
    # Requests send calls instead of replacing the real HTTP transport.
    import requests
    original=requests.Session.send
    def recorded(session,request,**kwargs):
        calls.append((request.method,request.url))
        return original(session,request,**kwargs)
    monkeypatch.setattr(requests.Session,'send',recorded)
    pair[1]([f'HTTP/1.1 {status} Redirect\r\nLocation: http://127.0.0.1:1/other\r\nContent-Length: 0\r\n\r\n'.encode()])
    try:
        with watchdog(),pytest.raises(SubstrateRequestException,match=str(status)):
            sdk.rpc_request('chain_getFinalizedHead',[])
    finally:sdk.close()
    assert calls==[('POST','http://127.0.0.1:1/')]


def test_connection_timeout_is_typed_and_closes_created_socket(monkeypatch,pair):
    from websocket import WebSocket,WebSocketTimeoutException
    def connect(ws,*args,**kwargs):
        ws.sock=pair[0]
        raise WebSocketTimeoutException('synthetic connect timeout')
    monkeypatch.setattr(WebSocket,'connect',connect)
    with pytest.raises(ReportRpcTimeout,match='connection deadline'):
        live_sdk('ws://synthetic.invalid',auto_discover=False,timeout_seconds=1.0)
    assert pair[0].fileno()==-1


def test_http_constructor_auto_discovery_is_bounded(monkeypatch,pair):
    with watchdog(),pytest.raises(ReportRpcTimeout):
        http_client(monkeypatch,pair,auto_discover=True)
    assert pair[0].fileno()==-1


def test_collector_fallback_closes_failed_runtime_before_next_endpoint(monkeypatch):
    from noah.reports.backing import collector
    events=[]
    def factory(url,**kwargs):
        events.append(('create',url))
        def init():
            if url.endswith('one.invalid'):raise ReportRpcTimeout('synthetic deadline')
        return SimpleNamespace(init_runtime=init,close=lambda:events.append(('close',url)))
    monkeypatch.setattr(collector,'live_sdk',factory)
    monkeypatch.setattr(collector,'_attach_recorder',lambda *_args,**_kwargs:[])
    client,label=collector._connect(['ws://one.invalid','ws://two.invalid'])
    assert events==[('create','ws://one.invalid'),('close','ws://one.invalid'),('create','ws://two.invalid')]
    assert label=='ws://two.invalid'
    client.close()
