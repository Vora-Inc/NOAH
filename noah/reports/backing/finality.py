"""Prove an included block is the ancestor of the finalized head at that height."""

from __future__ import annotations


def prove_ancestry(
    *,
    head_hash: str,
    head_number: int,
    inclusion_hash: str,
    inclusion_number: int,
    header_of,
) -> dict:
    """`header_of(block_hash)` returns `(number, parent_hash)`.

    Matching only the finalized height is not enough. The walk has to land on
    the inclusion hash.
    """
    proof = {
        "finalized_head": head_hash,
        "finalized_head_number": head_number,
        "inclusion_number": inclusion_number,
        "inclusion_hash": inclusion_hash,
        "canonical_hash": None,
        "matches": False,
    }
    if head_number < inclusion_number:
        return proof
    cursor = head_hash
    number, parent = header_of(cursor)
    if number != head_number:
        return proof
    while number > inclusion_number:
        cursor = parent
        next_number, parent = header_of(cursor)
        if next_number != number - 1:
            # Malformed provider data must neither skip ancestry nor loop on a
            # cyclic/non-descending parent chain during receipt reconciliation.
            return proof
        number = next_number
    proof["canonical_hash"] = cursor
    proof["matches"] = number == inclusion_number and cursor.lower() == inclusion_hash.lower()
    return proof
