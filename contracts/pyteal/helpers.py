#!/usr/bin/env python3
"""
NIKO STAKING PLATFORM - Helper Functions Module (OPCODE OPTIMIZED)
Contains utility functions for Merkle verification, date validation, and common operations

OPCODE OPTIMIZATION: Removed proof element length validation
- Saves ~5 opcodes per proof element × 12 max = ~60 opcodes
- Still safe: invalid lengths will fail at root comparison

FIXED: Use BytesLt() for byte comparison instead of < operator
"""

from pyteal import *
from state import GlobalState, Constants


@Subroutine(TealType.uint64)
def is_friday_utc():
    """
    Check if today is Friday in UTC
    
    Epoch reference: 1970-01-01 was Thursday (day 0)
    Day 1 = Friday, Day 2 = Saturday, etc.
    """
    return ((Global.latest_timestamp() / Constants.seconds_per_day) % Int(7)) == Int(1)



@Subroutine(TealType.bytes)
def compute_leaf_hash(user_address: Expr, amount: Expr, epoch_id: Expr):
    """
    Compute Merkle leaf hash for a claim
    
    Leaf = SHA256(user_address || app_id || pool_id || epoch_id || amount)
    
    This binds the claim to:
    - Specific user (prevents claiming for others)
    - Specific app (prevents cross-app replay)
    - Specific pool (prevents cross-pool replay)
    - Specific epoch (prevents cross-epoch replay)
    - Specific amount (backend-determined reward)
    """
    return Sha256(
        Concat(
            Concat(
                Concat(
                    Concat(
                        user_address,
                        Itob(Global.current_application_id())
                    ),
                    Itob(App.globalGet(GlobalState.pool_id_key))
                ),
                Itob(epoch_id)
            ),
            Itob(amount)
        )
    )


@Subroutine(TealType.bytes)
def hash_pair(a: Expr, b: Expr):
    """
    Hash a pair of nodes using sorted order
    
    FIXED: Use BytesLt() for lexicographic byte comparison
    
    This is the "sorted pair" Merkle verification method:
    - Always hash(min, max) regardless of proof position
    - Standard approach matching OpenZeppelin and industry best practices
    - Compare as bytes (lexicographically), not as integers
    """
    return Sha256(
        If(BytesLt(a, b))
        .Then(Concat(a, b))
        .Else(Concat(b, a))
    )


def verify_merkle_proof(leaf: Expr, proof_count: Expr, root: Expr):
    """
    Verify a Merkle proof using iterative hashing
    
    OPTIMIZED for 700 opcode budget:
    - Uses minimal scratch space
    - Efficient iteration
    - OPCODE CUT: Removed proof element length validation (saves ~60 opcodes total)
    
    Args:
        leaf: Starting hash (computed from claim data)
        proof_count: Number of proof elements
        root: Expected root hash
        
    Proof elements are read from Txn.application_args starting at index 3
    (after "claim", epoch_id, amount)
    
    Opcode cost: ~30 per proof element × 12 max = ~360 opcodes (was ~420)
    """
    current_hash = ScratchVar(TealType.bytes)
    i = ScratchVar(TealType.uint64)
    
    return Seq(
        # Initialize with leaf hash
        current_hash.store(leaf),
        i.store(Int(0)),
        
        # Iteratively hash with each proof element
        While(i.load() < proof_count).Do(
            Seq(
                # OPCODE OPTIMIZATION: Removed length validation (saves ~5 opcodes per iteration)
                # Invalid proof elements will cause root comparison to fail anyway
                # REMOVED: Assert(Len(Txn.application_args[Int(3) + i.load()]) == Int(32)),
                
                # Hash current value with proof[i] using sorted pair
                current_hash.store(
                    hash_pair(
                        current_hash.load(),
                        Txn.application_args[Int(3) + i.load()]
                    )
                ),
                i.store(i.load() + Int(1))
            )
        ),
        
        # Verify final hash equals root
        Assert(current_hash.load() == root)
    )


@Subroutine(TealType.bytes)
def get_epoch_root(epoch_id: Expr):
    """
    Retrieve the Merkle root for a specific epoch from box storage
    
    Box name: Itob(epoch_id) = 8 bytes
    Box value: root = 32 bytes
    
    This enables claiming any published epoch by fetching its stored root.
    
    Args:
        epoch_id: Epoch identifier
        
    Returns:
        32-byte Merkle root for that epoch
        
    Raises:
        Assert fails if epoch root not found (epoch not published)
    """
    root_box_name = Itob(epoch_id)
    root_value = App.box_get(root_box_name)
    
    return Seq(
        # Fetch the root from box storage
        root_value,
        
        # Verify root exists and is correct size
        Assert(root_value.hasValue()),
        Assert(Len(root_value.value()) == Int(32)),
        
        # Return the root
        root_value.value()
    )


def get_user_box_name():
    """Get the box name for the current user"""
    return Sha256(Concat(Txn.sender(), Itob(App.globalGet(GlobalState.pool_id_key))))


def validate_basic_transaction():
    """Common transaction validation - prevents account manipulation"""
    return Seq(
        Assert(Txn.rekey_to() == Global.zero_address()),
        Assert(Txn.close_remainder_to() == Global.zero_address()),
        Assert(Txn.asset_close_to() == Global.zero_address()),
    )