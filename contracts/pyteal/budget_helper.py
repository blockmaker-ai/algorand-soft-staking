#!/usr/bin/env python3
"""
BUDGET HELPER CONTRACT - Opcode Budget Extension

This minimal contract exists solely to add 700 opcodes to a transaction group's
shared budget. When called alongside other app calls, it contributes its
budget to the pool without consuming many opcodes itself.

Usage:
- Deploy once on mainnet
- Add a NoOp call to this contract in any transaction group that needs more budget
- The claim contract gains access to the extra ~690 opcodes

Cost per use: ~0.001 ALGO (minimum transaction fee)
"""

from pyteal import *
import os


def approval_program():
    """
    Minimal approval program - just approve everything.
    Uses ~10 opcodes, leaving ~690 for the group budget pool.
    """
    return Cond(
        # Contract creation - allow
        [Txn.application_id() == Int(0), Approve()],

        # NoOp calls - allow (this is the budget pad call)
        [Txn.on_completion() == OnComplete.NoOp, Approve()],

        # Reject updates/deletes to keep it immutable
        [Txn.on_completion() == OnComplete.UpdateApplication, Reject()],
        [Txn.on_completion() == OnComplete.DeleteApplication, Reject()],

        # Allow opt-in/close-out (not needed but harmless)
        [Txn.on_completion() == OnComplete.OptIn, Approve()],
        [Txn.on_completion() == OnComplete.CloseOut, Approve()],
        [Txn.on_completion() == OnComplete.ClearState, Approve()],
    )


def clear_program():
    """Clear state - always approve"""
    return Approve()


if __name__ == "__main__":
    out_dir = os.path.join(os.path.dirname(__file__), "teal")
    os.makedirs(out_dir, exist_ok=True)

    with open(os.path.join(out_dir, "budget_helper_approval.teal"), "w") as f:
        f.write(compileTeal(approval_program(), mode=Mode.Application, version=10))

    with open(os.path.join(out_dir, "budget_helper_clear.teal"), "w") as f:
        f.write(compileTeal(clear_program(), mode=Mode.Application, version=10))

    print("✅ BUDGET HELPER CONTRACT COMPILED")
    print("\n🎯 PURPOSE:")
    print("   Adds ~690 opcodes to transaction group budget pool")
    print("\n💰 COST:")
    print("   • Deploy: ~0.1 ALGO (one time)")
    print("   • Per use: ~0.001 ALGO (min txn fee)")
    print("\n📋 USAGE:")
    print("   1. Deploy this contract once")
    print("   2. Add app call to this contract in claim transaction groups")
    print("   3. Claim contract now has 1400 opcode budget")
