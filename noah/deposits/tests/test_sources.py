"""``noah.chain.sources.canonical_endpoint``: the URL fragment must never
distinguish two spellings of the SAME network endpoint.

Placed under ``noah/deposits/tests/`` (an already-adopted, already-covered
``pyproject.toml`` testpaths directory) rather than a new ``noah/chain/
tests/`` package, so this narrow, targeted regression test does not need a
new top-level testpaths entry -- and, with it, a change to the
validator/miner-tool split ``scripts/run-tests-python.sh`` derives directly
from that list -- for one file with a handful of tests.
"""

from noah.chain.sources import canonical_endpoint


def test_a_fragment_only_variant_of_one_endpoint_canonicalizes_identically():
    """Real codex adversarial finding, ticket #10: the fragment used to be
    carried straight through, so two URLs differing ONLY by fragment
    canonicalized to two DIFFERENT strings and could pass a dual-source
    agreement gate as two DISTINCT endpoints -- one real server, counted
    twice. A URL fragment (RFC 3986 3.5) is never sent to the server and
    cannot distinguish one endpoint from another; both variants below must
    canonicalize to the identical string."""
    a = canonical_endpoint("wss://Node.Example:443/rpc#witness-a")
    b = canonical_endpoint("wss://Node.Example:443/rpc#witness-b")
    c = canonical_endpoint("wss://Node.Example:443/rpc")  # no fragment at all
    assert a == b == c


def test_the_fragment_is_dropped_even_when_nothing_else_about_the_url_changes():
    """The narrowest possible case: identical scheme, host, port, and
    path; only the fragment differs. If canonicalization ever regresses to
    carrying the fragment through again, this is the first thing that
    would disagree."""
    with_fragment = canonical_endpoint("https://archive.example/v1#block-9001")
    without_fragment = canonical_endpoint("https://archive.example/v1")
    assert with_fragment == without_fragment
    assert "#" not in with_fragment


def test_query_strings_are_still_preserved_unlike_fragments():
    """Only the fragment is dropped -- a query string (which, unlike a
    fragment, IS sent to the server and can genuinely select different
    server-side behaviour) still distinguishes two URLs, exactly as
    before this fix. Naming this keeps the fragment fix from silently
    becoming a broader, unflagged change to query handling."""
    a = canonical_endpoint("https://node.example/rpc?shard=1")
    b = canonical_endpoint("https://node.example/rpc?shard=2")
    assert a != b


def test_two_real_distinct_hosts_still_disagree():
    """The fragment fix must not fold together two genuinely different
    endpoints -- only fragment-only variants of the SAME one."""
    a = canonical_endpoint("wss://node-one.example/rpc#x")
    b = canonical_endpoint("wss://node-two.example/rpc#x")
    assert a != b
