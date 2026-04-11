#!/usr/bin/env python3
"""
NIKO STAKING PLATFORM - State Management Module
Defines all global state keys and constants used across the contract

OPCODE OPTIMIZATION: max_proof_depth reduced from 14 to 12
"""

from pyteal import *


class GlobalState:
    """Global state keys for the staking contract"""
    
    # Core identifiers
    admin_key = Bytes("admin")                        # Platform backend address
    sponsor_key = Bytes("sponsor")                    # Pool sponsor (funded the pool)
    reward_token_id = Bytes("reward_token")           # ASA ID for rewards
    pool_id_key = Bytes("pool_id")                    # Unique pool identifier
    
    # Pool configuration
    distribution_type_key = Bytes("dist_type")        # 0=daily, 1=weekly
    funding_model_key = Bytes("funding")              # 0=one-time, 1=rolling
    pool_start_date_key = Bytes("start_date")         # Unix timestamp
    pool_end_date_key = Bytes("end_date")             # Unix timestamp (informational)
    
    # Epoch and accounting
    current_epoch_id_key = Bytes("epoch_id")          # Last published epoch
    total_deposited_key = Bytes("deposited")          # Total rewards funded
    total_claimed_key = Bytes("claimed")              # Total rewards claimed (delta sum)
    
    # Control flags
    paused_key = Bytes("paused")                      # 0=active, 1=paused
    deprecated_key = Bytes("deprecated")              # 0=active, 1=deprecated
    
    # Platform fee management
    platform_fee_address_key = Bytes("fee_addr")      # Platform fee recipient address
    pending_fee_address_key = Bytes("pending_fee")    # Proposed new fee address
    fee_update_time_key = Bytes("fee_update_time")    # When fee address can be updated

    # Automated publishing (V1.4.0)
    publisher_key = Bytes("publisher")                # Address allowed to call set_root (admin or delegated publisher)


class Constants:
    """Contract constants"""
    
    # Time constants
    seconds_per_day = Int(86400)
    fee_update_delay = Int(604800)     # 7 days in seconds
    
    # Fee constants
    min_fee = Global.min_txn_fee()
    platform_fee_amount = Int(800000)  # 0.8 ALGO in microAlgos
    
    # Box MBR costs
    box_mbr_cost = Int(21700)          # User box: 2500 + 400*(32+16) = 21,700 µA
    # Epoch root box MBR: 18,500 µA (2500 + 400*(8+32))
    # Not enforced on-chain - admin must ensure contract is funded
    
    # Box sizes
    user_box_size = Int(16)            # [epoch (8) || cumulative (8)]
    epoch_root_size = Int(32)          # Merkle root hash
    
    # Merkle proof limits
    # OPCODE OPTIMIZATION: Reduced from 14 to 12 (saves ~70 opcodes)
    # Still supports 4,096 users (2^12) - plenty for most pools
    max_proof_depth = Int(12)          # Was 14, now 12 for opcode budget