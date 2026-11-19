"""Malformed parent links must not hang report receipt reconciliation."""
import pytest
from noah.reports.backing.finality import prove_ancestry


@pytest.mark.parametrize('next_number', [10, 8, 11])
def test_nonconsecutive_parent_stops_without_matching(next_number):
    calls = []
    def header(key):
        calls.append(key)
        assert len(calls) <= 2
        return (10, 'parent') if key == 'head' else (next_number, 'head')
    proof = prove_ancestry(head_hash='head', head_number=10, inclusion_hash='target',
                          inclusion_number=7, header_of=header)
    assert proof['matches'] is False
    assert calls == ['head', 'parent']


def test_actual_consecutive_ancestor_matches():
    headers = {'head': (10, 'parent'), 'parent': (9, 'target'), 'target': (8, 'older')}
    proof = prove_ancestry(head_hash='head', head_number=10, inclusion_hash='target',
                          inclusion_number=8, header_of=headers.__getitem__)
    assert proof['matches'] is True
