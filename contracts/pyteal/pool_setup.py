#!/usr/bin/env python3
"""
NIKO STAKING PLATFORM - Pool Setup Module
Handles contract initialization, pool funding, and asset opt-in
"""

from pyteal import *
from state import GlobalState, Constants
from helpers import validate_basic_transaction


def initialize():
    """
    Create the pool with initial configuration

    Args:
    0: sponsor_address (32 bytes) - Who funded the pool
    1: reward_token_id (uint64) - ASA ID for rewards
    2: pool_id (uint64) - Unique pool identifier
    3: distribution_type (uint64) - 0=daily, 1=weekly
    4: funding_model (uint64) - 0=one-time, 1=rolling
    5: pool_start_date (uint64) - Unix timestamp
    6: pool_end_date (uint64) - Unix timestamp (informational)
    7: platform_fee_address (32 bytes) - Where platform fees are sent
    8: publisher_address (32 bytes) - OPTIONAL - Address allowed to publish epoch roots
       If omitted (8 args), publisher defaults to Txn.sender() (same as admin / self-publishing)
       If provided (9 args), that address can call set_root (automated publishing)

    Note: Txn.sender() becomes admin (platform backend / pool creator)
    """
    reward_token = Btoi(Txn.application_args[1])
    pool_id = Btoi(Txn.application_args[2])

    return Seq(
        # Security: Prevent account manipulation
        validate_basic_transaction(),

        # Validate argument count: 8 (self-publishing) or 9 (automated with publisher address)
        Assert(
            Or(
                Txn.application_args.length() == Int(8),
                Txn.application_args.length() == Int(9),
            )
        ),
        Assert(Len(Txn.application_args[0]) == Int(32)),  # Valid sponsor address length
        Assert(Len(Txn.application_args[7]) == Int(32)),  # Valid fee address length
        
        # CRITICAL: Prevent zero address for sponsor and platform fee address
        Assert(Txn.application_args[0] != Global.zero_address()),
        Assert(Txn.application_args[7] != Global.zero_address()),
        
        Assert(reward_token > Int(0)),
        Assert(pool_id > Int(0)),
        
        # Validate distribution_type: 0=daily, 1=weekly
        Assert(
            Or(
                Btoi(Txn.application_args[3]) == Int(0),
                Btoi(Txn.application_args[3]) == Int(1),
            )
        ),
        
        # Validate funding_model: 0=one-time, 1=rolling
        Assert(
            Or(
                Btoi(Txn.application_args[4]) == Int(0),
                Btoi(Txn.application_args[4]) == Int(1),
            )
        ),

        # Initialize global state
        App.globalPut(GlobalState.admin_key, Txn.sender()),
        App.globalPut(GlobalState.sponsor_key, Txn.application_args[0]),
        App.globalPut(GlobalState.reward_token_id, reward_token),
        App.globalPut(GlobalState.pool_id_key, pool_id),
        App.globalPut(GlobalState.distribution_type_key, Btoi(Txn.application_args[3])),
        App.globalPut(GlobalState.funding_model_key, Btoi(Txn.application_args[4])),
        App.globalPut(GlobalState.pool_start_date_key, Btoi(Txn.application_args[5])),
        App.globalPut(GlobalState.pool_end_date_key, Btoi(Txn.application_args[6])),
        App.globalPut(GlobalState.platform_fee_address_key, Txn.application_args[7]),
        App.globalPut(GlobalState.current_epoch_id_key, Int(0)),
        App.globalPut(GlobalState.total_deposited_key, Int(0)),
        App.globalPut(GlobalState.total_claimed_key, Int(0)),
        App.globalPut(GlobalState.paused_key, Int(0)),
        App.globalPut(GlobalState.deprecated_key, Int(0)),
        App.globalPut(GlobalState.pending_fee_address_key, Global.zero_address()),
        App.globalPut(GlobalState.fee_update_time_key, Int(0)),

        # Publisher key: use arg[8] if provided (automated), otherwise default to admin (self-publishing)
        If(Txn.application_args.length() == Int(9))
        .Then(Seq(
            Assert(Len(Txn.application_args[8]) == Int(32)),
            Assert(Txn.application_args[8] != Global.zero_address()),
            App.globalPut(GlobalState.publisher_key, Txn.application_args[8]),
        ))
        .Else(
            App.globalPut(GlobalState.publisher_key, Txn.sender()),
        ),

        Approve(),
    )


def opt_in_asset():
    """
    Contract opts into the reward token
    Must be called before funding
    Admin only
    """
    return Seq(
        validate_basic_transaction(),

        Assert(Txn.application_args.length() == Int(1)),
        Assert(Txn.sender() == App.globalGet(GlobalState.admin_key)),
        
        # Require fee for inner transaction
        Assert(Txn.fee() >= Constants.min_fee * Int(2)),

        # Inner transaction to opt into ASA
        InnerTxnBuilder.Begin(),
        InnerTxnBuilder.SetFields(
            {
                TxnField.type_enum: TxnType.AssetTransfer,
                TxnField.xfer_asset: App.globalGet(GlobalState.reward_token_id),
                TxnField.asset_receiver: Global.current_application_address(),
                TxnField.asset_amount: Int(0),
                TxnField.fee: Int(0),
                TxnField.asset_close_to: Global.zero_address(),
            }
        ),
        InnerTxnBuilder.Submit(),

        Approve(),
    )


def fund_pool():
    """
    Deposit rewards into the pool
    
    Group structure:
    0: AssetTransfer (sponsor -> contract)
    1: ApplicationCall (this function)
    
    For one-time pools: Can fund once
    For rolling pools: Can fund multiple times
    """
    funding_model = App.globalGet(GlobalState.funding_model_key)
    already_funded = App.globalGet(GlobalState.total_deposited_key) > Int(0)

    return Seq(
        validate_basic_transaction(),

        # Validate transaction structure
        Assert(Txn.application_args.length() == Int(1)),
        Assert(Txn.sender() == App.globalGet(GlobalState.admin_key)),
        Assert(Global.group_size() == Int(2)),
        Assert(Txn.group_index() == Int(1)),

        # Validate asset transfer in group
        Assert(Gtxn[0].type_enum() == TxnType.AssetTransfer),
        Assert(Gtxn[0].xfer_asset() == App.globalGet(GlobalState.reward_token_id)),
        Assert(Gtxn[0].asset_receiver() == Global.current_application_address()),
        Assert(Gtxn[0].sender() == App.globalGet(GlobalState.sponsor_key)),
        Assert(Gtxn[0].asset_amount() > Int(0)),

        # Security checks on asset transfer
        Assert(Gtxn[0].rekey_to() == Global.zero_address()),
        Assert(Gtxn[0].close_remainder_to() == Global.zero_address()),
        Assert(Gtxn[0].asset_close_to() == Global.zero_address()),

        # Enforce one-time funding if model is 0
        If(funding_model == Int(0))
        .Then(Assert(Not(already_funded))),

        # Update accounting
        App.globalPut(
            GlobalState.total_deposited_key,
            App.globalGet(GlobalState.total_deposited_key) + Gtxn[0].asset_amount(),
        ),

        Approve(),
    )