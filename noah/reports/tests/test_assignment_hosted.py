"""Real loopback HTTP plus native signatures; no blockchain transactions."""
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pytest
import sr25519

from noah.reports import assignments as a
from noah.reports.tests.test_assignment_v2 import case


def test_hosted_trusted_signer_wrong_signer_and_outage(case, tmp_path):
    response = {'document': case.signed, 'status': 200}
    paths = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            paths.append(self.path)
            raw = json.dumps(response['document']).encode()
            self.send_response(response['status'])
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        now = datetime(2026, 11, 18, tzinfo=timezone.utc).timestamp()
        url = f'http://127.0.0.1:{server.server_port}/assignments/'
        def fetch():
            return a.current_document(tmp_path, url, now, trusted_public_key=case.key,
                                      expected_template_identity=case.identity)
        assert fetch() == case.signed
        cache = tmp_path / 'weekly-assignments/2026-11-15.json'
        retained = cache.read_bytes()
        other = sr25519.pair_from_seed(b'b' * 32)
        forged_unsigned = {**case.unsigned, 'assignment_signer_public_key': other[0].hex()}
        response['document'] = a.sign_week(forged_unsigned, other, trusted_public_key=other[0].hex())
        with pytest.raises(a.ReportError, match='trusted key'):
            fetch()
        assert cache.read_bytes() == retained
        response['status'] = 503
        assert fetch() == case.signed
        assert paths == ['/assignments/2026-11-15.json'] * 3
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()
