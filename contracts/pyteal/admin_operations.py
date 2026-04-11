#!/usr/bin/env python3
"""
NIKO STAKING PLATFORM - Admin Operations Module
Handles admin-only functions: root publishing, pause/deprecate, emergency withdrawal, fee management
"""

from pyteal import *
from state import GlobalState, Constants
from helpers import validate_basic_transaction, is_friday_utc


def set_epoch_root():
    """
    Publish a new Merkle root for an epoch
    
    CRITICAL: Stores root in box for later retrieval
    IMMUTABLE: Cannot republish same epoch
    
    Args:
    0: "set_root"
    1: epoch_id (uint64) - MUST be > 0
    2: root (32 bytes) - Merkle root hash
    
    Box structure:
    - Box name: Itob(epoch_id) = 8 bytes
    - Box value: root = 32 bytes
    - MBR: 18,500 µA (paid by contract)
    
    Weekly pools: Can only set on Fridays UTC
    """
    new_epoch_id = Btoi(Txn.application_args[1])
    new_root = Txn.application_args[2]
    current_epoch_id = App.globalGet(GlobalState.current_epoch_id_key)
    dist_type = App.globalGet(GlobalState.distribution_type_key)
    
    epoch_root_box_name = Itob(new_epoch_id)

    return Seq(
        validate_basic_transaction(),

        # Validate caller: either admin (self-publishing) or publisher (automated publishing)
        Assert(
            Or(
                Txn.sender() == App.globalGet(GlobalState.admin_key),
                Txn.sender() == App.globalGet(GlobalState.publisher_key),
            )
        ),
        Assert(Txn.application_args.length() == Int(3)),
        Assert(Len(new_root) == Int(32)),
        
        # CRITICAL: Epoch ID must be > 0
        Assert(new_epoch_id > Int(0)),
        
        # UX: Verify box reference provided (commented out - see note below)
        # Note: Txn.boxes requires special handling in PyTeal
        # Box validation is handled at runtime by AVM
        # Assert(Txn.boxes.length() >= Int(1)),

        # Security checks
        Assert(App.globalGet(GlobalState.deprecated_key) == Int(0)),
        
        # Epoch must be monotonically increasing
        Assert(
            Or(
                current_epoch_id == Int(0),
                new_epoch_id > current_epoch_id
            )
        ),
        
        # Weekly pools: Only set roots on Fridays UTC
        If(dist_type == Int(1))
        .Then(Assert(is_friday_utc())),

        # Create box for root storage (fails if exists)
        Assert(App.box_create(epoch_root_box_name, Constants.epoch_root_size) == Int(1)),
        App.box_replace(epoch_root_box_name, Int(0), new_root),

        # Update current epoch tracker
        App.globalPut(GlobalState.current_epoch_id_key, new_epoch_id),

        # Log epoch root publication
        Log(Concat(
            Bytes("SET_ROOT"),
            Concat(Itob(new_epoch_id), new_root)
        )),

        Approve(),
    )


def toggle_pause():
    """
    Emergency pause/unpause mechanism
    
    Args:
    0: "pause"
    1: state (uint64) - 0=unpause, 1=pause
    """
    new_state = ScratchVar(TealType.uint64)
    
    return Seq(
        validate_basic_transaction(),
        
        Assert(Txn.sender() == App.globalGet(GlobalState.admin_key)),
        Assert(Txn.application_args.length() == Int(2)),
        
        new_state.store(Btoi(Txn.application_args[1])),
        Assert(Or(new_state.load() == Int(0), new_state.load() == Int(1))),
        
        App.globalPut(GlobalState.paused_key, new_state.load()),
        
        Log(Concat(Bytes("PAUSE"), Itob(new_state.load()))),
        
        Approve(),
    )


def toggle_deprecated():
    """
    Mark pool as deprecated
    
    Args:
    0: "deprecate"
    1: state (uint64) - 0=active, 1=deprecated
    
    Once deprecated:
    - No new claims allowed
    - No new roots can be published
    - Emergency withdrawal becomes available
    - ONE-WAY: Cannot be reversed
    """
    new_state = ScratchVar(TealType.uint64)
    current_state = App.globalGet(GlobalState.deprecated_key)
    
    return Seq(
        validate_basic_transaction(),
        
        Assert(Txn.sender() == App.globalGet(GlobalState.admin_key)),
        Assert(Txn.application_args.length() == Int(2)),
        
        new_state.store(Btoi(Txn.application_args[1])),
        Assert(Or(new_state.load() == Int(0), new_state.load() == Int(1))),
        
        # ONE-WAY: Once deprecated, cannot reactivate
        If(current_state == Int(1))
        .Then(Assert(new_state.load() == Int(1))),
        
        App.globalPut(GlobalState.deprecated_key, new_state.load()),
        
        Log(Concat(Bytes("DEPRECATE"), Itob(new_state.load()))),
        
        Approve(),
    )


def emergency_withdraw():
    """
    Emergency withdrawal of all remaining rewards
    
    SECURITY REQUIREMENTS:
    - Only admin can call
    - Pool must be deprecated first
    - Sends remaining ASA balance to sponsor
    
    Args:
    0: "emergency_withdraw"
    
    FIXED: Removed explicit asset_close_to parameter to avoid AVM issue
    with zero address being treated as unavailable account
    """
    reward_token = App.globalGet(GlobalState.reward_token_id)
    sponsor = App.globalGet(GlobalState.sponsor_key)
    
    asa_balance = AssetHolding.balance(
        Global.current_application_address(),
        reward_token
    )
    
    return Seq(
        validate_basic_transaction(),
        
        # Validate caller
        Assert(Txn.sender() == App.globalGet(GlobalState.admin_key)),
        Assert(Txn.application_args.length() == Int(1)),
        
        # CRITICAL: Pool must be deprecated
        Assert(App.globalGet(GlobalState.deprecated_key) == Int(1)),
        
        # Require fee for inner transaction
        Assert(Txn.fee() >= Constants.min_fee * Int(2)),
        
        # Get balance
        asa_balance,
        Assert(asa_balance.hasValue()),
        
        # Only withdraw if balance exists
        If(asa_balance.value() > Int(0))
        .Then(Seq(
            # Send all remaining rewards to sponsor
            InnerTxnBuilder.Begin(),
            InnerTxnBuilder.SetFields({
                TxnField.type_enum: TxnType.AssetTransfer,
                TxnField.xfer_asset: reward_token,
                TxnField.asset_receiver: sponsor,
                TxnField.asset_amount: asa_balance.value(),
                TxnField.fee: Int(0),
                # FIXED: Removed asset_close_to line - defaults to zero address
                # This avoids AVM "unavailable Account" error for zero address
            }),
            InnerTxnBuilder.Submit(),
            
            Log(Concat(Bytes("EMERGENCY_WITHDRAW"), Itob(asa_balance.value()))),
        )),
        
        Approve(),
    )


def update_end_date():
    """
    Update pool end date (informational only)
    
    Args:
    0: "update_end_date"
    1: new_end_date (uint64)
    """
    return Seq(
        validate_basic_transaction(),
        
        Assert(Txn.sender() == App.globalGet(GlobalState.admin_key)),
        Assert(Txn.application_args.length() == Int(2)),
        
        App.globalPut(GlobalState.pool_end_date_key, Btoi(Txn.application_args[1])),
        Approve(),
    )


def propose_fee_address():
    """
    Propose a new platform fee address (Step 1 of 2)
    
    TIMELOCK: 7-day waiting period before execution
    
    Args:
    0: "propose_fee_address"
    1: new_fee_address (32 bytes)
    """
    return Seq(
        validate_basic_transaction(),
        
        Assert(Txn.sender() == App.globalGet(GlobalState.admin_key)),
        Assert(Txn.application_args.length() == Int(2)),
        Assert(Len(Txn.application_args[1]) == Int(32)),
        
        # Validate new address is not zero
        Assert(Txn.application_args[1] != Global.zero_address()),
        
        # Store pending address and update time
        App.globalPut(GlobalState.pending_fee_address_key, Txn.application_args[1]),
        App.globalPut(GlobalState.fee_update_time_key, Global.latest_timestamp() + Constants.fee_update_delay),
        
        # Log proposal
        Log(Concat(
            Bytes("PROPOSE_FEE_ADDR"),
            Concat(
                Txn.application_args[1],
                Itob(Global.latest_timestamp() + Constants.fee_update_delay)
            )
        )),
        
        Approve(),
    )


def execute_fee_update():
    """
    Execute pending fee address update (Step 2 of 2)
    
    REQUIREMENTS:
    - 7 days must have passed since propose_fee_address()
    - Pending address must be set
    
    Args:
    0: "execute_fee_update"
    """
    pending_address = App.globalGet(GlobalState.pending_fee_address_key)
    update_time = App.globalGet(GlobalState.fee_update_time_key)
    
    return Seq(
        validate_basic_transaction(),
        
        Assert(Txn.sender() == App.globalGet(GlobalState.admin_key)),
        Assert(Txn.application_args.length() == Int(1)),
        
        # Verify pending update exists
        Assert(pending_address != Global.zero_address()),
        Assert(update_time > Int(0)),
        
        # Verify timelock has passed
        Assert(Global.latest_timestamp() >= update_time),
        
        # Update fee address
        App.globalPut(GlobalState.platform_fee_address_key, pending_address),
        
        # Clear pending state
        App.globalPut(GlobalState.pending_fee_address_key, Global.zero_address()),
        App.globalPut(GlobalState.fee_update_time_key, Int(0)),
        
        # Log fee address update
        Log(Concat(
            Bytes("UPDATE_FEE_ADDR"),
            Concat(
                pending_address,
                Itob(Global.latest_timestamp())
            )
        )),
        
        Approve(),
    )


def cancel_fee_update():
    """
    Cancel a pending fee address update

    Args:
    0: "cancel_fee_update"
    """
    return Seq(
        validate_basic_transaction(),

        Assert(Txn.sender() == App.globalGet(GlobalState.admin_key)),
        Assert(Txn.application_args.length() == Int(1)),

        # Clear pending state
        App.globalPut(GlobalState.pending_fee_address_key, Global.zero_address()),
        App.globalPut(GlobalState.fee_update_time_key, Int(0)),

        Log(Bytes("CANCEL_FEE_UPDATE")),

        Approve(),
    )


def update_publisher():
    """
    Update the publisher address (admin only)

    Allows pool creators to change the epoch publisher at any time.
    - Set to a platform-managed address for automated publishing
    - Set back to Txn.sender() (admin) to revert to self-publishing

    Args:
    0: "update_publisher"
    1: new_publisher_address (32 bytes)
    """
    return Seq(
        validate_basic_transaction(),

        Assert(Txn.sender() == App.globalGet(GlobalState.admin_key)),
        Assert(Txn.application_args.length() == Int(2)),
        Assert(Len(Txn.application_args[1]) == Int(32)),
        Assert(Txn.application_args[1] != Global.zero_address()),

        App.globalPut(GlobalState.publisher_key, Txn.application_args[1]),

        Log(Concat(Bytes("UPDATE_PUBLISHER"), Txn.application_args[1])),

        Approve(),
    )