#!/usr/bin/env python3
"""
NIKO STAKING PLATFORM - User Operations Module (OPCODE OPTIMIZED)

PHASE 1 CHANGES (Security Audit):
1. Removed dead code: total_claimed read and validation
   - Was not being updated anyway (opcode optimization in v1.3.3)
   - Validation was ineffective (always checking 0 + delta <= deposited)
   - Real protection comes from asa_balance.value() check
   - Saves ~10-15 opcodes

CHANGES FROM v1.3.3:
- Removed: total_claimed = App.globalGet(GlobalState.total_claimed_key)
- Removed: Assert(total_claimed + delta_amount.load() <= total_deposited)
- All other security validations intact
- ASA balance check remains (this is the real protection)
"""

from pyteal import *
from state import GlobalState, Constants
from helpers import (
    validate_basic_transaction,
    get_user_box_name,
    verify_merkle_proof,
    compute_leaf_hash,
    get_epoch_root,

)


def opt_in_user():
    """
    Register user for the pool
    
    Note: Box storage is created on first claim, not on opt-in
    This keeps opt-in cost minimal (0.001 ALGO fee only)
    """
    return Seq(
        validate_basic_transaction(),
        Approve(),
    )


def close_out_user():
    """
    Allow users to cleanly exit the pool
    Recovers their local state MBR
    
    Note: Box is NOT deleted automatically
    User should call delete_box() separately to recover box MBR
    """
    return Seq(
        validate_basic_transaction(),
        Approve(),
    )


def delete_box():
    """
    Delete user's box to recover MBR
    
    Allows users to recover the 21,700 µA box MBR.
    Works even after close-out (intentional feature).
    
    Requirements:
    - Box must exist
    - Contract must have sufficient ALGO balance to return MBR
    
    Net recovery: ~18,700 µA (21,700 - txn fees)
    """
    user_box_name = get_user_box_name()
    box_exists = App.box_length(user_box_name)
    
    return Seq(
        validate_basic_transaction(),
        
        # Validate arguments
        Assert(Txn.application_args.length() == Int(1)),
        
        # UX: Verify box reference provided (commented out - see note below)
        # Note: Txn.boxes requires special handling in PyTeal
        # Box validation is handled at runtime by AVM
        # Assert(Txn.boxes.length() >= Int(1)),
        
        # Require fee for inner transaction
        Assert(Txn.fee() >= Constants.min_fee * Int(2)),
        
        # Verify box exists
        box_exists,
        Assert(box_exists.hasValue()),
        Assert(box_exists.value() == Constants.user_box_size),
        
        # Delete the box
        Assert(App.box_delete(user_box_name)),
        
        # Return MBR to user (21,700 - 1,000 = 20,700 µA)
        InnerTxnBuilder.Begin(),
        InnerTxnBuilder.SetFields({
            TxnField.type_enum: TxnType.Payment,
            TxnField.receiver: Txn.sender(),
            TxnField.amount: Constants.box_mbr_cost - Int(1000),
            TxnField.fee: Int(0),
            TxnField.close_remainder_to: Global.zero_address(),
        }),
        InnerTxnBuilder.Submit(),
        
        # Log box deletion (keeping this one - it's cheap and useful)
        Log(Concat(Bytes("BOX_DELETE"), Txn.sender())),
        
        Approve(),
    )


def claim_rewards():
    """
    Process a reward claim with Merkle proof
    
    ACCUMULATING MODEL WITH DELTA PAYMENT:
    - Each epoch's Merkle root contains CUMULATIVE totals
    - User box stores: [last_epoch_claimed (8) || last_cumulative_claimed (8)]
    - Pays delta = (amount - last_cumulative_claimed)
    
    Required structure (relative to claim):
    FIRST CLAIM:  [PlatformFeePayment, BoxPayment, AppCall]
    SUBSEQUENT:   [PlatformFeePayment, AppCall]
    
    Args:
    0: "claim"
    1: epoch_id (uint64)
    2: amount (uint64) - CUMULATIVE total
    3+: proof elements (32 bytes each)
    
    MUST include 2 box references:
    1. User box: SHA256(user_address || pool_id)
    2. Epoch root box: Itob(epoch_id)
    
    PHASE 1 SECURITY CHANGES:
    - Removed total_claimed read and validation (was ineffective)
    - Real protection: asa_balance.value() >= delta_amount check
    - Also protected by: deposited balance tracking, Merkle proofs
    """
    epoch_id = Btoi(Txn.application_args[1])
    cumulative_amount = Btoi(Txn.application_args[2])
    proof_count = Txn.application_args.length() - Int(3)
    claim_index = Txn.group_index()

    reward_token = App.globalGet(GlobalState.reward_token_id)
    current_epoch_id = App.globalGet(GlobalState.current_epoch_id_key)
    total_deposited = App.globalGet(GlobalState.total_deposited_key)
    # PHASE 1 REMOVED: total_claimed (was not being updated anyway)
    platform_fee_address = App.globalGet(GlobalState.platform_fee_address_key)

    user_box_name = get_user_box_name()
    
    # Check contract's ASA balance
    asa_balance = AssetHolding.balance(
        Global.current_application_address(), 
        reward_token
    )
    
    # Check if user has claimed before
    box_exists = App.box_length(user_box_name)
    
    # Scratch variables
    last_epoch_claimed = ScratchVar(TealType.uint64)
    last_cumulative_claimed = ScratchVar(TealType.uint64)
    delta_amount = ScratchVar(TealType.uint64)
    fee_payment_index = ScratchVar(TealType.uint64)
    epoch_root = ScratchVar(TealType.bytes)

    return Seq(
        # === SECURITY: Prevent account manipulation ===
        validate_basic_transaction(),

        # === VALIDATE ARGUMENTS ===
        Assert(Txn.application_args.length() >= Int(3)),
        Assert(cumulative_amount > Int(0)),
        Assert(proof_count <= Constants.max_proof_depth),
        
        # UX: Validate box references (need 2: user box + epoch root box)
        # Note: Txn.boxes requires special handling in PyTeal
        # Box validation is handled at runtime by AVM
        # Assert(Txn.boxes.length() >= Int(2)),
        
        # User must be opted in
        Assert(App.optedIn(Txn.sender(), Global.current_application_id())),
        
        # Fee requirement
        Assert(Txn.fee() >= Constants.min_fee * Int(2)),

        # === SECURITY CHECKS ===
        Assert(App.globalGet(GlobalState.paused_key) == Int(0)),
        Assert(App.globalGet(GlobalState.deprecated_key) == Int(0)),
        Assert(Global.latest_timestamp() >= App.globalGet(GlobalState.pool_start_date_key)),
        
        # Epoch validation
        Assert(epoch_id > Int(0)),
        Assert(epoch_id <= current_epoch_id),

        # === FETCH EPOCH-SPECIFIC MERKLE ROOT ===
        epoch_root.store(get_epoch_root(epoch_id)),

        # === EVALUATE BOX EXISTS ===
        box_exists,

        # === CALCULATE DELTA AND VALIDATE ===
        If(box_exists.hasValue())
        .Then(Seq(
            # Subsequent claim - fee payment at claim_index - 1
            Assert(claim_index >= Int(1)),
            fee_payment_index.store(claim_index - Int(1)),
            
            # Read last claimed epoch and cumulative
            last_epoch_claimed.store(Btoi(App.box_extract(user_box_name, Int(0), Int(8)))),
            last_cumulative_claimed.store(Btoi(App.box_extract(user_box_name, Int(8), Int(8)))),
            
            # Verify strictly increasing epoch
            Assert(epoch_id > last_epoch_claimed.load()),
            
            # Verify cumulative is increasing
            Assert(cumulative_amount >= last_cumulative_claimed.load()),
            
            # Calculate delta
            delta_amount.store(cumulative_amount - last_cumulative_claimed.load()),
        ))
        .Else(Seq(
            # First claim - fee at claim_index - 2, box payment at claim_index - 1
            Assert(claim_index >= Int(2)),
            fee_payment_index.store(claim_index - Int(2)),
            
            # Validate box payment
            Assert(Gtxn[claim_index - Int(1)].type_enum() == TxnType.Payment),
            Assert(Gtxn[claim_index - Int(1)].sender() == Txn.sender()),
            Assert(Gtxn[claim_index - Int(1)].receiver() == Global.current_application_address()),
            Assert(Gtxn[claim_index - Int(1)].amount() >= Constants.box_mbr_cost),
            Assert(Gtxn[claim_index - Int(1)].fee() >= Constants.min_fee),
            Assert(Gtxn[claim_index - Int(1)].rekey_to() == Global.zero_address()),
            Assert(Gtxn[claim_index - Int(1)].close_remainder_to() == Global.zero_address()),
            
            # Create box
            Assert(App.box_create(user_box_name, Constants.user_box_size) == Int(1)),
            
            # Initialize values
            last_epoch_claimed.store(Int(0)),
            last_cumulative_claimed.store(Int(0)),
            delta_amount.store(cumulative_amount),
        )),
        
        # CRITICAL: Reject zero-delta claims
        Assert(delta_amount.load() > Int(0)),
        
        # === VALIDATE PLATFORM FEE PAYMENT ===
        Assert(Gtxn[fee_payment_index.load()].type_enum() == TxnType.Payment),
        Assert(Gtxn[fee_payment_index.load()].sender() == Txn.sender()),
        Assert(Gtxn[fee_payment_index.load()].receiver() == platform_fee_address),
        Assert(Gtxn[fee_payment_index.load()].amount() == Constants.platform_fee_amount),
        Assert(Gtxn[fee_payment_index.load()].fee() >= Constants.min_fee),
        Assert(Gtxn[fee_payment_index.load()].rekey_to() == Global.zero_address()),
        Assert(Gtxn[fee_payment_index.load()].close_remainder_to() == Global.zero_address()),

        # === MERKLE PROOF VERIFICATION ===
        verify_merkle_proof(
            compute_leaf_hash(Txn.sender(), cumulative_amount, epoch_id),
            proof_count,
            epoch_root.load()
        ),

        # === VERIFY SUFFICIENT FUNDS ===
        # PHASE 1 REMOVED: total_claimed validation (was not updated, always 0)
        # 
        # PROTECTION LAYERS REMAINING:
        # 1. total_deposited tracks all funds added to pool
        # 2. asa_balance.value() checks actual ASA balance (PRIMARY PROTECTION)
        # 3. Merkle proof ensures backend authorized this amount
        # 4. Delta calculation prevents double-claiming same epoch
        # 5. Box storage tracks what user already received
        #
        # The total_deposited check was redundant since we check actual ASA balance below.
        # If backend creates bad Merkle tree (authorizes more than deposited), the 
        # asa_balance check will catch it. This is the REAL protection.

        asa_balance,
        Assert(asa_balance.hasValue()),
        Assert(asa_balance.value() >= delta_amount.load()),  # PRIMARY PROTECTION

        # === UPDATE STATE ===
        # Update user box: [epoch (8) || cumulative (8)]
        App.box_replace(user_box_name, Int(0), Itob(epoch_id)),
        App.box_replace(user_box_name, Int(8), Itob(cumulative_amount)),
        
        # PHASE 1 NOTE: total_claimed NOT updated (removed in v1.3.3 for opcode optimization)
        # Can be calculated off-chain from user boxes or transaction history

        # === TRANSFER TOKENS ===
        InnerTxnBuilder.Begin(),
        InnerTxnBuilder.SetFields(
            {
                TxnField.type_enum: TxnType.AssetTransfer,
                TxnField.xfer_asset: reward_token,
                TxnField.asset_receiver: Txn.sender(),
                TxnField.asset_amount: delta_amount.load(),
                TxnField.fee: Int(0),
                # FIXED: Omit asset_close_to instead of setting to zero address
                # Setting to zero address causes "unavailable Account" error
            }
        ),
        InnerTxnBuilder.Submit(),
        
        # OPCODE OPTIMIZATION: Removed claim log (removed in v1.3.3)

        Approve(),
    )