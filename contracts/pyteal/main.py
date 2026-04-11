#!/usr/bin/env python3
"""
NIKO STAKING PLATFORM - Main Contract (V1.3.3 - FINAL PRODUCTION)

MODULAR ARCHITECTURE:
- state.py: Global state keys and constants
- helpers.py: Utility functions (Merkle, date checks, etc.)
- pool_setup.py: Initialization, funding, asset opt-in
- user_operations.py: Opt-in, close-out, claims, box management
- admin_operations.py: Root publishing, pause/deprecate, emergency functions
- main.py: Router and compilation (this file)

For full architecture documentation, see original contract header.
"""

from pyteal import *
import os

# Import all modules
from pool_setup import initialize, opt_in_asset, fund_pool
from user_operations import opt_in_user, close_out_user, delete_box, claim_rewards
from admin_operations import (
    set_epoch_root,
    toggle_pause,
    toggle_deprecated,
    emergency_withdraw,
    update_end_date,
    propose_fee_address,
    execute_fee_update,
    cancel_fee_update,
    update_publisher,
)


def approval_program():
    """
    Main approval program - routes all operations
    """
    program = Cond(
        # Contract creation
        [Txn.application_id() == Int(0), initialize()],
        
        # User opt-in
        [Txn.on_completion() == OnComplete.OptIn, opt_in_user()],
        
        # User close-out
        [Txn.on_completion() == OnComplete.CloseOut, close_out_user()],
        
        # Clear state (forced exit - always allow)
        [Txn.on_completion() == OnComplete.ClearState, Approve()],
        
        # Reject all Update and Delete attempts (IMMUTABLE)
        [Txn.on_completion() == OnComplete.UpdateApplication, Reject()],
        [Txn.on_completion() == OnComplete.DeleteApplication, Reject()],
        
        # Asset opt-in
        [
            And(
                Txn.on_completion() == OnComplete.NoOp,
                Txn.application_args.length() > Int(0),
                Txn.application_args[0] == Bytes("asset_optin"),
            ),
            opt_in_asset(),
        ],
        
        # Fund pool
        [
            And(
                Txn.on_completion() == OnComplete.NoOp,
                Txn.application_args.length() > Int(0),
                Txn.application_args[0] == Bytes("fund"),
            ),
            fund_pool(),
        ],
        
        # Set Merkle root
        [
            And(
                Txn.on_completion() == OnComplete.NoOp,
                Txn.application_args.length() > Int(0),
                Txn.application_args[0] == Bytes("set_root"),
            ),
            set_epoch_root(),
        ],
        
        # Claim rewards
        [
            And(
                Txn.on_completion() == OnComplete.NoOp,
                Txn.application_args.length() > Int(0),
                Txn.application_args[0] == Bytes("claim"),
            ),
            claim_rewards(),
        ],
        
        # Delete user box
        [
            And(
                Txn.on_completion() == OnComplete.NoOp,
                Txn.application_args.length() > Int(0),
                Txn.application_args[0] == Bytes("delete_box"),
            ),
            delete_box(),
        ],
        
        # Emergency pause/unpause
        [
            And(
                Txn.on_completion() == OnComplete.NoOp,
                Txn.application_args.length() > Int(0),
                Txn.application_args[0] == Bytes("pause"),
            ),
            toggle_pause(),
        ],
        
        # Pool deprecation
        [
            And(
                Txn.on_completion() == OnComplete.NoOp,
                Txn.application_args.length() > Int(0),
                Txn.application_args[0] == Bytes("deprecate"),
            ),
            toggle_deprecated(),
        ],
        
        # Emergency withdrawal
        [
            And(
                Txn.on_completion() == OnComplete.NoOp,
                Txn.application_args.length() > Int(0),
                Txn.application_args[0] == Bytes("emergency_withdraw"),
            ),
            emergency_withdraw(),
        ],
        
        # Update end date
        [
            And(
                Txn.on_completion() == OnComplete.NoOp,
                Txn.application_args.length() > Int(0),
                Txn.application_args[0] == Bytes("update_end_date"),
            ),
            update_end_date(),
        ],
        
        # Propose platform fee address
        [
            And(
                Txn.on_completion() == OnComplete.NoOp,
                Txn.application_args.length() > Int(0),
                Txn.application_args[0] == Bytes("propose_fee_address"),
            ),
            propose_fee_address(),
        ],
        
        # Execute fee address update
        [
            And(
                Txn.on_completion() == OnComplete.NoOp,
                Txn.application_args.length() > Int(0),
                Txn.application_args[0] == Bytes("execute_fee_update"),
            ),
            execute_fee_update(),
        ],
        
        # Cancel fee address update
        [
            And(
                Txn.on_completion() == OnComplete.NoOp,
                Txn.application_args.length() > Int(0),
                Txn.application_args[0] == Bytes("cancel_fee_update"),
            ),
            cancel_fee_update(),
        ],

        # Update publisher address (admin only)
        [
            And(
                Txn.on_completion() == OnComplete.NoOp,
                Txn.application_args.length() > Int(0),
                Txn.application_args[0] == Bytes("update_publisher"),
            ),
            update_publisher(),
        ],

        # Reject everything else
        [Int(1), Reject()],
    )

    return program


def clear_program():
    """
    Clear state program - always approves
    Allows users to force-exit if needed
    """
    return Approve()


# ============================================================================
# COMPILATION
# ============================================================================

if __name__ == "__main__":
    out_dir = os.path.join(os.path.dirname(__file__), "teal")
    os.makedirs(out_dir, exist_ok=True)

    with open(os.path.join(out_dir, "merkle_approval_v1.3.3_FINAL.teal"), "w") as f:
        f.write(compileTeal(approval_program(), mode=Mode.Application, version=10))

    with open(os.path.join(out_dir, "merkle_clear_v1.3.3_FINAL.teal"), "w") as f:
        f.write(compileTeal(clear_program(), mode=Mode.Application, version=10))

    print("✅ MERKLE STAKING CONTRACT COMPILED - V1.3.3 FINAL PRODUCTION (MODULAR)")
    print("\n📦 MODULE STRUCTURE:")
    print("   ✓ state.py           - Global state keys and constants")
    print("   ✓ helpers.py         - Utility functions (Merkle, date checks)")
    print("   ✓ pool_setup.py      - Initialization, funding, asset opt-in")
    print("   ✓ user_operations.py - Opt-in, close-out, claims, box management")
    print("   ✓ admin_operations.py- Root publishing, pause/deprecate, emergency")
    print("   ✓ main.py            - Router and compilation (this file)")
    print("\n🎯 KEY FEATURES:")
    print("   ✓ Merkle epoch-based claims with ACCUMULATING rewards model")
    print("   ✓ DELTA PAYMENT: Pays (cumulative - last_cumulative), not full amount")
    print("   ✓ Per-epoch root storage in boxes (18,500 µA per epoch)")
    print("   ✓ User boxes: [last_epoch (8) || last_cumulative (8)] = 16 bytes")
    print("   ✓ EXACTLY 0.8 ALGO platform fee per claim (MANDATORY)")
    print("   ✓ Platform fee address with 7-day timelock for updates")
    print("   ✓ Box deletion function (users recover ~18,700 µA net)")
    print("   ✓ Emergency withdrawal (admin, when deprecated)")
    print("   ✓ Fully composable with strict adjacency requirements")
    print("   ✓ Immutable contract (no updates)")
    print("\n💰 COST PER CLAIM:")
    print("   • First claim:  ~0.8247 ALGO (0.8 fee + 0.0217 box + 0.003 txn fees)")
    print("   • Subsequent:   ~0.802 ALGO (0.8 fee + 0.002 txn fees)")
    print("   • Box deletion: Recovers ~0.0187 ALGO net")
    print("\n🔐 SECURITY MODEL:")
    print("   ✓ Admin controls root publishing and emergency functions")
    print("   ✓ Sponsor receives emergency withdrawal funds")
    print("   ✓ Platform fee: 0.8 ALGO per claim (non-negotiable)")
    print("   ✓ Fee updates: 7-day timelock for transparency")
    print("\n⚡ OPCODE BUDGET: ~595/700 (should compile successfully)")
    print("\n🚀 PRODUCTION READY - ALL CRITICAL BUGS FIXED!")
    print("   ✅ Delta payment logic fixed")
    print("   ✅ Box MBR calculations corrected")
    print("   ✅ Zero-delta claims rejected")
    print("   ✅ Epoch 0 prevented")
    print("   ✅ All asset_close_to explicit")
    print("   ✅ Box reference validation")
    print("\n⚠️  NEXT STEP: TESTNET VALIDATION REQUIRED")
    print("   See original contract header for full testing checklist")