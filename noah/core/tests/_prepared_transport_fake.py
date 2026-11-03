"""Transport boundary double; real pinned SDK is exercised in test_prepared_transaction."""

from types import SimpleNamespace
from ..prepared_transaction import Prepared, transaction_hash


class PreparedTransportFake:
    def __init__(self, client, wallet):
        self.client, self.wallet = client, wallet

    def prepare(self, call, chain):
        data = b"offline-test-extrinsic"
        head = self.client.block
        account = self.client.query(("System", "Account"), [self.wallet.ss58_address])
        nonce = account["nonce"] if isinstance(account, dict) else 7
        evidence = {
            "schema": 1,
            "signed_hex": "0x" + data.hex(),
            "extrinsic_hash": transaction_hash(data),
            "nonce": nonce,
            "signer": self.wallet.ss58_address,
            "head_at_send": head,
            "era": {"period": 128, "current": head},
            "birth": head,
            "death": head + 128,
            "birth_hash": f"0x{head:064x}",
        }
        return Prepared(SimpleNamespace(data=data, call=call), evidence)

    def send(self, prepared, chain):
        return self.client.submit_call(
            prepared.signed.call,
            self.wallet,
            wait_for_inclusion=True,
            wait_for_finalization=True,
        )


def install(monkeypatch):
    from .. import prepared_transaction

    monkeypatch.setattr(
        prepared_transaction, "SDKPreparedTransport", PreparedTransportFake
    )
    monkeypatch.setattr(
        prepared_transaction, "signed_bytes_at", lambda *_: b"offline-test-extrinsic"
    )
