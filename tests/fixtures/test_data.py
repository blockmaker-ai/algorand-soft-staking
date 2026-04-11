#!/usr/bin/env python3
"""
TEST DATA FIXTURES - FIXED VERSION
Supports start_epoch parameter to avoid epoch conflicts

FIXED: Added start_epoch parameter to create_epoch_scenario()
This allows admin tests to use epochs 10-12 and user tests to use epochs 20-22
"""

import sys
import os
from typing import List, Dict, Tuple
import hashlib

# Add parent directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from utils.account_manager import TestAccount


def create_merkle_tree(leaves: List[bytes]) -> Tuple[bytes, List[List[bytes]]]:
    """
    Create a Merkle tree and return root + proofs for each leaf
    
    Args:
        leaves: List of leaf hashes
        
    Returns:
        (root_hash, list_of_proofs)
        where list_of_proofs[i] = proof for leaves[i]
    """
    if not leaves:
        raise ValueError("Cannot create tree from empty leaves")
    
    # Build tree level by level
    current_level = leaves[:]
    tree_levels = [current_level]
    
    while len(current_level) > 1:
        next_level = []
        
        # Process pairs
        for i in range(0, len(current_level), 2):
            left = current_level[i]
            
            if i + 1 < len(current_level):
                right = current_level[i + 1]
            else:
                # Odd number of nodes - duplicate last node
                right = current_level[i]
            
            # Sorted pair hashing (standard approach)
            if left <= right:
                parent = hashlib.sha256(left + right).digest()
            else:
                parent = hashlib.sha256(right + left).digest()
            
            next_level.append(parent)
        
        current_level = next_level
        tree_levels.append(current_level)
    
    root = current_level[0]
    
    # Generate proofs for each leaf
    proofs = []
    for leaf_index in range(len(leaves)):
        proof = []
        index = leaf_index
        
        for level in range(len(tree_levels) - 1):
            current = tree_levels[level]
            
            # Find sibling
            if index % 2 == 0:
                # Left node - sibling is right
                sibling_index = index + 1
            else:
                # Right node - sibling is left
                sibling_index = index - 1
            
            if sibling_index < len(current):
                proof.append(current[sibling_index])
            else:
                # Odd node - use itself as sibling
                proof.append(current[index])
            
            # Move to parent index
            index = index // 2
        
        proofs.append(proof)
    
    return root, proofs


def compute_leaf_hash(
    user_address: str,
    app_id: int,
    pool_id: int,
    epoch_id: int,
    cumulative_amount: int
) -> bytes:
    """
    Compute Merkle leaf hash for a user claim
    
    Matches the contract's compute_leaf_hash() function:
    SHA256(user_address || app_id || pool_id || epoch_id || amount)
    """
    from algosdk import encoding
    
    # Convert address to 32-byte public key
    user_bytes = encoding.decode_address(user_address)
    
    # Build leaf data
    leaf_data = (
        user_bytes +
        app_id.to_bytes(8, 'big') +
        pool_id.to_bytes(8, 'big') +
        epoch_id.to_bytes(8, 'big') +
        cumulative_amount.to_bytes(8, 'big')
    )
    
    return hashlib.sha256(leaf_data).digest()


def create_epoch_scenario(
    users: List[TestAccount],
    app_id: int,
    pool_id: int,
    scenario: str = "default",
    start_epoch: int = 1  # FIXED: Added start_epoch parameter
) -> Dict:
    """
    Create test data for multiple epochs with Merkle roots and proofs
    
    FIXED: Now supports start_epoch parameter to avoid epoch conflicts
    - Admin tests: start_epoch=10 → epochs 10, 11, 12
    - User tests: start_epoch=20 → epochs 20, 21, 22
    
    Args:
        users: List of test accounts
        app_id: Application ID
        pool_id: Pool ID
        scenario: Which reward distribution to use
        start_epoch: Starting epoch number (default=1)
        
    Returns:
        Dictionary of epochs with roots, leaves, and user data
    """
    scenarios = {
        "default": [
            # Epoch start_epoch: Initial rewards
            [
                {"user_index": 0, "cumulative": 10_000_000_000},   # 10k tokens
                {"user_index": 1, "cumulative": 15_000_000_000},   # 15k tokens
                {"user_index": 2, "cumulative": 20_000_000_000},   # 20k tokens
                {"user_index": 3, "cumulative": 5_000_000_000},    # 5k tokens
            ],
            # Epoch start_epoch+1: Accumulated rewards
            [
                {"user_index": 0, "cumulative": 25_000_000_000},   # +15k = 25k total
                {"user_index": 1, "cumulative": 35_000_000_000},   # +20k = 35k total
                {"user_index": 2, "cumulative": 50_000_000_000},   # +30k = 50k total
                {"user_index": 3, "cumulative": 15_000_000_000},   # +10k = 15k total
            ],
            # Epoch start_epoch+2: More accumulated rewards
            [
                {"user_index": 0, "cumulative": 45_000_000_000},   # +20k = 45k total
                {"user_index": 1, "cumulative": 60_000_000_000},   # +25k = 60k total
                {"user_index": 2, "cumulative": 80_000_000_000},   # +30k = 80k total
                {"user_index": 3, "cumulative": 30_000_000_000},   # +15k = 30k total
            ],
        ],
        "uniform": [
            # Equal rewards for all users
            [
                {"user_index": 0, "cumulative": 10_000_000_000},
                {"user_index": 1, "cumulative": 10_000_000_000},
                {"user_index": 2, "cumulative": 10_000_000_000},
                {"user_index": 3, "cumulative": 10_000_000_000},
            ],
            [
                {"user_index": 0, "cumulative": 20_000_000_000},
                {"user_index": 1, "cumulative": 20_000_000_000},
                {"user_index": 2, "cumulative": 20_000_000_000},
                {"user_index": 3, "cumulative": 20_000_000_000},
            ],
            [
                {"user_index": 0, "cumulative": 30_000_000_000},
                {"user_index": 1, "cumulative": 30_000_000_000},
                {"user_index": 2, "cumulative": 30_000_000_000},
                {"user_index": 3, "cumulative": 30_000_000_000},
            ],
        ],
    }
    
    epoch_data = scenarios.get(scenario, scenarios["default"])
    epochs = {}
    
    # FIXED: Use start_epoch instead of hardcoded 1, 2, 3
    for i, epoch_rewards in enumerate(epoch_data):
        epoch_id = start_epoch + i  # Epochs will be start_epoch, start_epoch+1, start_epoch+2
        
        # Create leaves for this epoch
        leaves = []
        user_data = []
        
        for reward_entry in epoch_rewards:
            user_idx = reward_entry["user_index"]
            user = users[user_idx]
            cumulative = reward_entry["cumulative"]
            
            # Compute leaf hash with actual epoch_id
            leaf = compute_leaf_hash(
                user.address,
                app_id,
                pool_id,
                epoch_id,  # Use actual epoch_id, not i+1
                cumulative
            )
            
            leaves.append(leaf)
            user_data.append({
                "user_index": user_idx,
                "address": user.address,
                "cumulative_amount": cumulative
            })
        
        # Build Merkle tree
        root, proofs = create_merkle_tree(leaves)
        
        # Store epoch data
        epochs[epoch_id] = {
            "root": root,
            "leaves": leaves,
            "users": [
                {
                    **user_data[i],
                    "proof": proofs[i],
                    "leaf": leaves[i]
                }
                for i in range(len(user_data))
            ]
        }
    
    return epochs


def calculate_expected_delta(cumulative_amount: int, last_cumulative: int) -> int:
    """
    Calculate expected delta payment
    
    Args:
        cumulative_amount: Current cumulative amount for this epoch
        last_cumulative: Last claimed cumulative amount
        
    Returns:
        Delta amount to be paid
    """
    return cumulative_amount - last_cumulative


class TestData:
    """Container for test data and helper methods"""
    
    def __init__(self, users: List[TestAccount], app_id: int, pool_id: int):
        self.users = users
        self.app_id = app_id
        self.pool_id = pool_id
    
    def get_epoch_scenario(self, scenario: str = "default", start_epoch: int = 1):
        """
        Get epoch data for a specific scenario
        
        FIXED: Now supports start_epoch parameter
        """
        return create_epoch_scenario(
            self.users,
            self.app_id,
            self.pool_id,
            scenario,
            start_epoch  # Pass through start_epoch
        )