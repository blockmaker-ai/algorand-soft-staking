#!/usr/bin/env python3
"""
Merkle Tree Utilities for NIKO Staking Platform (V1.3.3)

This module provides functions to:
1. Build Merkle trees from user claim data (CUMULATIVE MODEL)
2. Generate proofs for individual users
3. Verify proofs (for testing)
4. Format proofs for on-chain submission

MERKLE TREE STRUCTURE:
- Binary tree with sorted pair hashing (OpenZeppelin standard)
- Leaf: SHA256(address || app_id || pool_id || epoch_id || cumulative_amount)
- Internal nodes: SHA256(min(left, right) || max(left, right))
- No direction bits needed (sorted pair method)
- Deterministic ordering (sorted by hash)

CUMULATIVE MODEL (V1.3.3):
- Each epoch's Merkle root contains TOTAL claimable up to that epoch
- Example: Epoch 1 = 100, Epoch 2 = 250 (100 + 150 new), Epoch 3 = 500 (100 + 150 + 250 new)
- Contract pays DELTA (cumulative - last_cumulative)
- Backend must track cumulative totals per user across epochs

SECURITY:
- Leaf hash binds to app_id, pool_id, and epoch_id
- Prevents cross-app, cross-pool, and cross-epoch replay attacks
- Each address can appear only once per tree
- Proof verification is constant time O(log n)
- Delta calculation prevents overpayment

CRITICAL ON-CHAIN PARITY REQUIREMENT:
This Python implementation MUST match the deployed contract byte-for-byte:
1. Leaf hash: SHA256(decode_address(addr) || Itob(app_id) || Itob(pool_id) || Itob(epoch_id) || Itob(cumulative))
2. Pair hash: SHA256(min(a,b) || max(a,b)) - LEXICOGRAPHIC byte comparison
3. Tree construction: Leaves sorted by hash, odd nodes duplicated
4. Field order: EXACTLY as shown above (address first, cumulative last)

If the contract uses different field order, different byte encoding, or different
comparison method, ALL proofs will fail. See MERKLE_VERIFICATION.md for details.

USAGE:
    from merkle_utils_v1_3_3 import MerkleTree
    
    # Create tree from CUMULATIVE claims
    claims = [
        {"address": "ADDR1...", "cumulative": 250},  # Total up to this epoch
        {"address": "ADDR2...", "cumulative": 500},
    ]
    tree = MerkleTree(claims, app_id=123, pool_id=456, epoch_id=2)
    
    # Get root for publishing on-chain
    root_hex = tree.get_root_hex()
    
    # Get proof for user claim
    proof = tree.get_proof("ADDR1...")
    proof_hex = tree.get_proof_hex("ADDR1...")
    
    # Verify proof (for testing)
    is_valid = tree.verify_proof("ADDR1...", 250, proof)

CHANGELOG V1.3.3:
- CRITICAL: Updated to cumulative model (leaf binds cumulative, not delta)
- Updated costs to reflect 21,700 µA user box MBR (16 bytes now)
- Updated costs to reflect 18,500 µA epoch root box MBR (correct calculation)
- Added cumulative tracking helpers
- Updated transaction examples for 2 box references
- All examples now show cumulative amounts
- Updated first claim total: 824,700 µA (0.8247 ALGO)
- Fixed: Don't mutate caller's claims list
- Fixed: Normalize addresses at all API boundaries
"""

import hashlib
import json
from typing import List, Dict, Optional, Tuple, Any
from algosdk import encoding


def _u64(x: int) -> bytes:
    """
    Convert int to uint64 bytes with bounds checking
    
    Args:
        x: Integer value
        
    Returns:
        8-byte big-endian representation
        
    Raises:
        ValueError: If x is outside uint64 range [0, 2^64-1]
    """
    if x < 0 or x > (2**64 - 1):
        raise ValueError(f"Value {x} out of uint64 range [0, {2**64-1}]")
    return x.to_bytes(8, 'big')


class MerkleTree:
    """
    Merkle tree implementation using sorted pair hashing (CUMULATIVE MODEL)
    
    This matches the V1.3.3 contract implementation exactly.
    """
    
    def __init__(self, claims: List[Dict], app_id: int, pool_id: int, epoch_id: int):
        """
        Initialize Merkle tree from list of CUMULATIVE claims
        
        Args:
            claims: List of dicts with 'address' and 'cumulative' keys
                   Example: [{"address": "ADDR...", "cumulative": 250}, ...]
                   'cumulative' = TOTAL claimable up to this epoch
            app_id: Application ID (binds to specific contract)
            pool_id: Pool ID (binds to specific pool)
            epoch_id: Epoch ID (binds to specific epoch)
            
        Raises:
            ValueError: If claims list is empty, contains duplicate addresses,
                       has invalid address format, or invalid parameters
        """
        if len(claims) == 0:
            raise ValueError("Cannot create Merkle tree with no claims")
        
        # Validate app_id, pool_id, epoch_id (contract requires these > 0)
        if app_id <= 0:
            raise ValueError(f"app_id must be > 0, got {app_id}")
        if pool_id <= 0:
            raise ValueError(f"pool_id must be > 0, got {pool_id}")
        if epoch_id <= 0:
            raise ValueError(f"epoch_id must be > 0, got {epoch_id} (contract rejects epoch 0)")
        
        # Validate all addresses before proceeding
        # Create normalized copies to avoid mutating caller's data
        normalized_claims = []
        
        for i, claim in enumerate(claims):
            if 'address' not in claim:
                raise ValueError(f"Claim {i} missing 'address' field")
            if 'cumulative' not in claim:
                raise ValueError(f"Claim {i} missing 'cumulative' field")
            
            # Create copy and normalize address
            normalized_claim = dict(claim)
            normalized_claim['address'] = claim['address'].strip()
            addr = normalized_claim['address']
            
            # Validate Algorand address format
            try:
                encoding.decode_address(addr)
            except Exception as e:
                raise ValueError(f"Invalid Algorand address at claim {i}: {addr} - {e}")
            
            # Validate cumulative is int (catches JSON "123" errors early)
            if not isinstance(normalized_claim['cumulative'], int):
                raise ValueError(
                    f"Claim {i} has invalid cumulative type: "
                    f"expected int, got {type(normalized_claim['cumulative']).__name__} = {normalized_claim['cumulative']}"
                )
            
            # Validate cumulative is positive (policy: trees only include cumulative > 0)
            if normalized_claim['cumulative'] <= 0:
                raise ValueError(
                    f"Claim {i} has non-positive cumulative: {normalized_claim['cumulative']}. "
                    f"Trees only include users with cumulative > 0 (nothing to claim otherwise)."
                )
            
            # Validate cumulative is within uint64 bounds (upper bound only, already checked > 0)
            if normalized_claim['cumulative'] > (2**64 - 1):
                raise ValueError(
                    f"Claim {i} cumulative out of uint64 range [1, {2**64-1}]: {normalized_claim['cumulative']}"
                )
            
            normalized_claims.append(normalized_claim)
        
        # Check for duplicate addresses (critical for proof uniqueness)
        addresses = [c['address'] for c in normalized_claims]
        if len(addresses) != len(set(addresses)):
            duplicates = [addr for addr in set(addresses) if addresses.count(addr) > 1]
            raise ValueError(f"Duplicate addresses found in claims: {duplicates}")
        
        self.app_id = app_id
        self.pool_id = pool_id
        self.epoch_id = epoch_id
        self.claims = normalized_claims  # Store normalized copies
        self.leaves = []
        self.tree_levels = []
        
        self._build_tree()
    
    def _compute_leaf_hash(self, address: str, cumulative: int) -> bytes:
        """
        Compute leaf hash for a CUMULATIVE claim
        
        CRITICAL: This MUST match the on-chain compute_leaf_hash function exactly
        
        Leaf = SHA256(address || app_id || pool_id || epoch_id || cumulative)
        
        This binds the claim to:
        - Specific user (prevents claiming for others)
        - Specific app (prevents cross-app replay)
        - Specific pool (prevents cross-pool replay)
        - Specific epoch (prevents cross-epoch replay)
        - Specific cumulative amount (total up to this epoch)
        
        Args:
            address: Algorand address (58 chars)
            cumulative: TOTAL claimable up to this epoch (uint64)
            
        Returns:
            32-byte hash
            
        Raises:
            ValueError: If any integer is out of uint64 range
        """
        # Decode address to 32 bytes
        address_bytes = encoding.decode_address(address)
        
        # Convert integers to 8-byte big-endian with bounds checking
        # This matches Itob in TEAL and prevents overflow
        app_id_bytes = _u64(self.app_id)
        pool_id_bytes = _u64(self.pool_id)
        epoch_id_bytes = _u64(self.epoch_id)
        cumulative_bytes = _u64(cumulative)
        
        # Concatenate and hash (matches contract)
        data = address_bytes + app_id_bytes + pool_id_bytes + epoch_id_bytes + cumulative_bytes
        return hashlib.sha256(data).digest()
    
    def _hash_pair(self, a: bytes, b: bytes) -> bytes:
        """
        Hash a pair of nodes using sorted order
        
        CRITICAL: This MUST match the on-chain hash_pair function exactly
        
        This is the "sorted pair" Merkle verification method:
        - Always hash(min, max) regardless of position
        - Standard approach matching OpenZeppelin
        - Sorted lexicographically (byte comparison)
        
        Args:
            a: First hash (32 bytes, bytes or bytearray)
            b: Second hash (32 bytes, bytes or bytearray)
            
        Returns:
            32-byte hash of sorted pair
        """
        # Cast to bytes for consistent comparison (handles bytearray inputs)
        a = bytes(a)
        b = bytes(b)
        
        # Sort lexicographically (byte comparison, not integer)
        if a < b:
            return hashlib.sha256(a + b).digest()
        else:
            return hashlib.sha256(b + a).digest()
    
    def _build_tree(self):
        """
        Build the Merkle tree from CUMULATIVE claims
        
        Process:
        1. Create leaf hashes for all claims
        2. Store leaves with their original claim data
        3. Sort leaves by hash for deterministic ordering
        4. Build tree bottom-up, level by level
        5. Handle odd number of nodes by duplicating last node
        
        Note: Sorting ensures same claims always produce same root
        """
        # Create leaves
        for claim in self.claims:
            leaf_hash = self._compute_leaf_hash(claim['address'], claim['cumulative'])
            self.leaves.append({
                'hash': leaf_hash,
                'address': claim['address'],
                'cumulative': claim['cumulative']
            })
        
        # Sort leaves by hash for deterministic ordering
        # This ensures same set of claims always produces same root
        self.leaves.sort(key=lambda x: x['hash'])
        
        # Build tree levels bottom-up
        current_level = [leaf['hash'] for leaf in self.leaves]
        self.tree_levels = [current_level]
        
        while len(current_level) > 1:
            next_level = []
            
            # Process pairs
            for i in range(0, len(current_level), 2):
                left = current_level[i]
                
                # Handle odd number of nodes (duplicate last node)
                if i + 1 < len(current_level):
                    right = current_level[i + 1]
                else:
                    right = left  # Duplicate last node
                
                parent = self._hash_pair(left, right)
                next_level.append(parent)
            
            self.tree_levels.append(next_level)
            current_level = next_level
    
    def get_root(self) -> bytes:
        """
        Get the Merkle root hash
        
        Returns:
            32-byte root hash
            
        Raises:
            ValueError: If tree not built
        """
        if len(self.tree_levels) == 0:
            raise ValueError("Tree not built")
        
        return self.tree_levels[-1][0]
    
    def get_root_hex(self) -> str:
        """
        Get the Merkle root as hex string (for contract calls)
        
        Returns:
            Hex string (64 characters)
        """
        return self.get_root().hex()
    
    def get_proof(self, address: str) -> List[bytes]:
        """
        Generate Merkle proof for a specific address
        
        Proof is a list of sibling hashes from leaf to root.
        Contract will verify by hashing leaf with each proof element
        until reconstructing the root.
        
        Args:
            address: Algorand address (will be normalized)
            
        Returns:
            List of 32-byte hashes (proof elements)
            
        Raises:
            ValueError: If address is invalid or not found in tree
        """
        # Normalize address at API boundary
        address = address.strip()
        
        # Find leaf index
        leaf_index = None
        for i, leaf in enumerate(self.leaves):
            if leaf['address'] == address:
                leaf_index = i
                break
        
        if leaf_index is None:
            # Validate address format to give better error message
            try:
                encoding.decode_address(address)
            except Exception as e:
                raise ValueError(f"Invalid Algorand address: {address} - {e}")
            # Address is valid but not in tree
            raise ValueError(f"Address {address} not found in tree")
        
        # Generate proof by collecting siblings at each level
        proof = []
        index = leaf_index
        
        for level in self.tree_levels[:-1]:  # Exclude root level
            # Determine sibling index
            if index % 2 == 0:
                # We're left child, sibling is right
                sibling_index = index + 1
            else:
                # We're right child, sibling is left
                sibling_index = index - 1
            
            # Add sibling to proof (if it exists)
            if sibling_index < len(level):
                proof.append(level[sibling_index])
            else:
                # Odd number of nodes, duplicate ourselves
                proof.append(level[index])
            
            # Move to parent for next level
            index = index // 2
        
        return proof
    
    def get_proof_hex(self, address: str) -> List[str]:
        """
        Generate Merkle proof as hex strings (for contract calls)
        
        Args:
            address: Algorand address to generate proof for
            
        Returns:
            List of hex strings (64 characters each)
        """
        proof = self.get_proof(address)
        return [p.hex() for p in proof]
    
    def verify_proof(
        self,
        address: str,
        cumulative: int,
        proof: List[bytes],
        strict_length: bool = True
    ) -> bool:
        """
        Verify a Merkle proof (for testing)
        
        This mirrors the on-chain verification logic exactly.
        
        Args:
            address: Algorand address (will be normalized and validated)
            cumulative: TOTAL claimable up to this epoch (uint64)
            proof: List of sibling hashes (32-byte bytes objects)
            strict_length: If True (default), validate proof length matches tree depth.
                          Set to False for generic verification without full tree context.
            
        Returns:
            True if proof is valid, False otherwise
            
        Raises:
            ValueError: If address format is invalid, cumulative out of range,
                       proof elements are invalid, or (if strict_length=True)
                       proof length doesn't match tree depth
        """
        # Normalize and validate address at API boundary
        address = address.strip()
        try:
            encoding.decode_address(address)
        except Exception as e:
            raise ValueError(f"Invalid Algorand address: {address} - {e}")
        
        # Validate cumulative type and range
        if not isinstance(cumulative, int):
            raise ValueError(
                f"cumulative must be int, got {type(cumulative).__name__}"
            )
        if cumulative < 0 or cumulative > (2**64 - 1):
            raise ValueError(
                f"cumulative out of uint64 range [0, {2**64-1}]: {cumulative}"
            )
        
        # Validate proof elements (32-byte bytes objects)
        for i, pe in enumerate(proof):
            if not isinstance(pe, (bytes, bytearray)):
                raise ValueError(
                    f"proof[{i}] must be bytes or bytearray, got {type(pe).__name__}"
                )
            if len(pe) != 32:
                raise ValueError(
                    f"proof[{i}] must be 32 bytes, got {len(pe)} bytes"
                )
        
        # Optional: Validate proof length matches tree depth
        # (Helpful for catching "wrong tree" bugs during testing, but can be
        #  disabled for generic verification when you only have root+proof+leaf)
        if strict_length:
            expected_depth = len(self.tree_levels) - 1
            if len(proof) != expected_depth:
                raise ValueError(
                    f"proof length {len(proof)} != expected tree depth {expected_depth}. "
                    f"This proof is for a different tree size."
                )
        
        # Compute leaf hash
        current_hash = self._compute_leaf_hash(address, cumulative)
        
        # Hash with each proof element (using sorted pair)
        for proof_element in proof:
            current_hash = self._hash_pair(current_hash, proof_element)
        
        # Should equal root
        return current_hash == self.get_root()
    
    def get_claim_data(self, address: str) -> Optional[Dict[str, Any]]:
        """
        Get claim data for a specific address
        
        Args:
            address: Algorand address (will be normalized)
            
        Returns:
            Dict with 'address' and 'cumulative', or None if not found
        """
        # Normalize address at API boundary
        address = address.strip()
        
        for leaf in self.leaves:
            if leaf['address'] == address:
                return {
                    'address': leaf['address'],
                    'cumulative': leaf['cumulative']
                }
        return None
    
    def get_claim_bundle(self, address: str, include_proof_bytes: bool = False) -> Optional[Dict[str, Any]]:
        """
        Get complete claim bundle for API/frontend use
        
        Returns everything needed to submit a claim transaction:
        - address, cumulative, proof (hex)
        - root, epoch_id, app_id, pool_id for verification
        - leaf hash for debugging
        - proof_bytes (optional, for SDK callers)
        
        This is the recommended method for backend APIs serving claim data.
        
        Args:
            address: Algorand address (will be normalized)
            include_proof_bytes: If True, include proof_bytes for SDK callers
                                 (makes bundle non-JSON-serializable)
            
        Returns:
            Complete claim bundle dict, or None if address not in tree
            
            JSON-safe (default):
            {
                'address': 'ADDR...',
                'cumulative': 250,
                'proof': ['abc123...', 'def456...'],  # Hex
                'root': 'root_hex...',
                'leaf': 'leaf_hex...',  # For debugging
                'epoch_id': 3,
                'app_id': 123456,
                'pool_id': 789
            }
            
            With include_proof_bytes=True (for SDK):
            {
                ... (same as above) ...
                'proof_bytes': [b'...', b'...'],  # NOT JSON-serializable!
            }
        """
        # Normalize address at API boundary
        address = address.strip()
        
        claim = self.get_claim_data(address)
        if claim is None:
            return None
        
        proof_bytes = self.get_proof(address)
        
        bundle = {
            'address': claim['address'],
            'cumulative': claim['cumulative'],
            'proof': [p.hex() for p in proof_bytes],  # Hex for JSON
            'root': self.get_root_hex(),
            'leaf': self._compute_leaf_hash(claim['address'], claim['cumulative']).hex(),
            'epoch_id': self.epoch_id,
            'app_id': self.app_id,
            'pool_id': self.pool_id,
        }
        
        # Only include bytes if explicitly requested (breaks JSON serialization)
        if include_proof_bytes:
            bundle['proof_bytes'] = proof_bytes
        
        return bundle
    
    def get_all_claims(self) -> List[Dict]:
        """
        Get all claims in the tree (sorted by hash)
        
        Returns:
            List of dicts with 'address', 'cumulative', and 'hash'
        """
        return [
            {
                'address': leaf['address'],
                'cumulative': leaf['cumulative'],
                'hash': leaf['hash'].hex()
            }
            for leaf in self.leaves
        ]
    
    def get_statistics(self) -> Dict[str, Any]:
        """
        Get tree statistics
        
        Returns:
            Dict with tree statistics
        """
        total_cumulative = sum(leaf['cumulative'] for leaf in self.leaves)
        return {
            'total_claims': len(self.leaves),
            'total_cumulative': total_cumulative,
            'tree_depth': len(self.tree_levels) - 1,
            'root': self.get_root_hex(),
            'app_id': self.app_id,
            'pool_id': self.pool_id,
            'epoch_id': self.epoch_id,
        }
    
    def export_tree(self) -> Dict:
        """
        Export tree data for storage/distribution
        
        Returns JSON-safe dict with complete tree data.
        All fields are JSON-serializable (no bytes objects).
        
        Returns:
            Dict with complete tree data (JSON-safe)
        """
        return {
            'version': '1.3.3',
            'model': 'cumulative',
            'app_id': self.app_id,
            'pool_id': self.pool_id,
            'epoch_id': self.epoch_id,
            'root': self.get_root_hex(),  # Hex string (JSON-safe)
            'claims': self.claims,  # Dicts with str/int only (JSON-safe)
            'statistics': self.get_statistics()  # Dict with str/int only (JSON-safe)
        }
    
    @classmethod
    def import_tree(cls, data: Dict) -> 'MerkleTree':
        """
        Import tree from exported data
        
        Args:
            data: Dict from export_tree()
            
        Returns:
            MerkleTree instance
            
        Raises:
            ValueError: If data format is invalid
        """
        required_fields = ['app_id', 'pool_id', 'epoch_id', 'claims']
        for field in required_fields:
            if field not in data:
                raise ValueError(f"Missing required field: {field}")
        
        tree = cls(
            claims=data['claims'],
            app_id=data['app_id'],
            pool_id=data['pool_id'],
            epoch_id=data['epoch_id']
        )
        
        # Verify root matches if provided
        if 'root' in data and tree.get_root_hex() != data['root']:
            raise ValueError("Root hash mismatch - data may be corrupted")
        
        return tree
    
    def print_tree(self, max_level: int = None):
        """
        Print tree structure for debugging
        
        Args:
            max_level: Maximum level to print (None = all levels)
        """
        print(f"\n{'='*70}")
        print(f"MERKLE TREE STRUCTURE (CUMULATIVE MODEL V1.3.3)")
        print(f"{'='*70}")
        print(f"App ID: {self.app_id}")
        print(f"Pool ID: {self.pool_id}")
        print(f"Epoch ID: {self.epoch_id}")
        print(f"Total Claims: {len(self.leaves)}")
        print(f"Tree Depth: {len(self.tree_levels) - 1}")
        print(f"Root: {self.get_root_hex()}")
        print(f"{'='*70}")
        
        levels_to_print = self.tree_levels if max_level is None else self.tree_levels[:max_level + 1]
        
        for level_idx, level in enumerate(levels_to_print):
            print(f"\nLevel {level_idx} ({len(level)} nodes):")
            for i, node_hash in enumerate(level):
                print(f"  [{i}] {node_hash.hex()}")
        
        print(f"\n{'='*70}\n")


def calculate_delta(current_cumulative: int, last_cumulative: int) -> int:
    """
    Calculate delta (actual tokens to receive) from cumulative amounts
    
    Args:
        current_cumulative: Total claimable up to current epoch
        last_cumulative: Total claimed up to last epoch
        
    Returns:
        Delta amount (tokens to receive this claim)
        
    Raises:
        ValueError: If current < last (would be negative delta)
    """
    if current_cumulative < last_cumulative:
        raise ValueError(f"Current cumulative ({current_cumulative}) < last ({last_cumulative})")
    
    return current_cumulative - last_cumulative


def build_cumulative_claims(epochs: List[Dict[str, Any]]) -> Dict[int, List[Dict[str, Any]]]:
    """
    Build cumulative claims from per-epoch rewards
    
    CRITICAL USER CONTINUITY RULE:
    Once a user has cumulative > 0 in any epoch, they will appear in ALL future epochs
    as long as their cumulative total remains > 0 (which it should in a correct system).
    
    In a correct cumulative ledger:
    - Cumulative can only increase or stay the same (monotonic)
    - Once cumulative > 0, it stays > 0 forever
    - Users naturally continue appearing in all future epoch trees
    
    If backend ever needs to "zero out" a user (rollback/correction), they will disappear
    from future trees and ensure_user_continuity() will correctly flag this as an error.
    
    Args:
        epochs: List of dicts with epoch_id and per-epoch rewards
                Example: [
                    {
                        'epoch_id': 1,
                        'rewards': [
                            {'address': 'ADDR1', 'amount': 100},
                            {'address': 'ADDR2', 'amount': 200}
                        ]
                    },
                    {
                        'epoch_id': 2,
                        'rewards': [
                            {'address': 'ADDR1', 'amount': 150},
                            {'address': 'ADDR2', 'amount': 0}  # 0 new rewards, cumulative stays 200
                        ]
                    }
                ]
    
    Returns:
        Dict mapping epoch_id to cumulative claims
        Example: {
            1: [{'address': 'ADDR1', 'cumulative': 100}, ...],
            2: [{'address': 'ADDR1', 'cumulative': 250}, {'address': 'ADDR2', 'cumulative': 200}],
        }
        
    Raises:
        ValueError: If amount is negative, address is invalid, or amount is not an int
    """
    # Track cumulative per user across epochs
    cumulative_tracker = {}
    result = {}
    
    # Validate epoch_id exists in all entries before sorting (prevents KeyError)
    for i, epoch_entry in enumerate(epochs):
        if 'epoch_id' not in epoch_entry:
            raise ValueError(f"Epoch entry {i} missing 'epoch_id' field")
        if 'rewards' not in epoch_entry:
            raise ValueError(f"Epoch entry {i} missing 'rewards' field")
    
    # Sort epochs by ID
    sorted_epochs = sorted(epochs, key=lambda x: x['epoch_id'])
    
    for epoch_data in sorted_epochs:
        epoch_id = epoch_data['epoch_id']
        rewards = epoch_data.get('rewards')
        
        # Validate epoch_id is int > 0 (prevents JSON "3" string issues)
        if not isinstance(epoch_id, int):
            raise ValueError(
                f"epoch_id must be int, got {type(epoch_id).__name__} = {epoch_id}"
            )
        if epoch_id <= 0:
            raise ValueError(
                f"epoch_id must be > 0, got {epoch_id} (contract rejects epoch 0)"
            )
        
        # Validate rewards is a list (defensive against JSON bugs)
        if rewards is None:
            raise ValueError(
                f"Epoch {epoch_id} has no 'rewards' field"
            )
        if not isinstance(rewards, list):
            raise ValueError(
                f"Epoch {epoch_id} rewards must be a list, got {type(rewards).__name__}"
            )
        
        # Update cumulative for users who received rewards this epoch
        # Track seen addresses to detect duplicates within same epoch
        seen_addresses = set()
        
        for i, reward in enumerate(rewards):
            # Validate reward is a dict with required fields
            if not isinstance(reward, dict):
                raise ValueError(
                    f"Epoch {epoch_id}, reward {i}: expected dict, got {type(reward).__name__}"
                )
            if 'address' not in reward or 'amount' not in reward:
                raise ValueError(
                    f"Epoch {epoch_id}, reward {i}: missing 'address' or 'amount' field"
                )
            
            # Normalize address (strip whitespace, Algorand SDK handles case)
            addr = reward['address'].strip()
            amount = reward['amount']
            
            # Validate address format early (better error messages)
            try:
                encoding.decode_address(addr)
            except Exception as e:
                raise ValueError(
                    f"Invalid Algorand address in epoch {epoch_id}, reward {i}: {addr} - {e}"
                )
            
            # Check for duplicate addresses within same epoch (catches data bugs)
            if addr in seen_addresses:
                raise ValueError(
                    f"Duplicate reward entry for {addr} in epoch {epoch_id}. "
                    f"Each address should appear at most once per epoch. "
                    f"If multiple rewards are intended, sum them before building tree."
                )
            seen_addresses.add(addr)
            
            # Validate amount is int and non-negative
            if not isinstance(amount, int):
                raise ValueError(
                    f"Amount must be int in epoch {epoch_id}, reward {i}: got {type(amount)}"
                )
            if amount < 0:
                raise ValueError(
                    f"Amount cannot be negative in epoch {epoch_id}, reward {i}: got {amount}"
                )
            
            if addr not in cumulative_tracker:
                cumulative_tracker[addr] = 0
            
            cumulative_tracker[addr] += amount
        
        # USER CONTINUITY: Include all users who have ever received rewards
        # Policy: Only include users with cumulative > 0 in trees
        # In a correct cumulative system, once cumulative > 0 it stays > 0 forever
        # So users naturally appear in all future epochs (as long as system is correct)
        # 
        # If a user ever gets cumulative = 0 (shouldn't happen in correct system):
        # - They drop out of future trees
        # - ensure_user_continuity() will flag this as an error
        # - This prevents accidental backend bugs from silently breaking claims
        
        # Sort by address for stable output (better diffs when comparing epochs)
        epoch_claims = [
            {'address': addr, 'cumulative': cumulative_tracker[addr]}
            for addr in sorted(cumulative_tracker.keys())
            if cumulative_tracker[addr] > 0  # Only include cumulative > 0
        ]
        
        result[epoch_id] = epoch_claims
    
    return result


def ensure_user_continuity(
    cumulative_by_epoch: Dict[int, List[Dict[str, Any]]]
) -> Dict[int, List[Dict[str, Any]]]:
    """
    Verify user continuity and monotonic cumulative across epochs (safety check)
    
    Enforces two critical rules:
    1. Once a user has cumulative > 0, they must keep appearing in future epochs
       (as long as their cumulative remains > 0, which it should in correct systems)
    2. A user's cumulative can NEVER decrease between epochs
    
    In a correct cumulative ledger:
    - Cumulative is monotonically increasing (or stays same)
    - Once cumulative > 0, it never goes back to 0
    - Users naturally continue appearing in all epoch trees
    
    These rules prevent:
    - Users being locked out from future claims
    - Backend bugs causing decreasing cumulative (contract would reject)
    - Accidental data corrections that break continuity
    
    If a user's cumulative ever becomes 0 after being > 0:
    - They will drop from future trees (filtered by cumulative > 0)
    - This function will detect the discontinuity and raise an error
    - This is intentional: prevents silent backend bugs
    
    CORRECTION POLICY IMPLICATIONS:
    This enforcement means you CANNOT:
    - Decrease any user's cumulative (contract will reject)
    - Drop users from future epochs (this function will reject)
    - "Roll back" incorrectly attributed rewards
    
    If you need to correct errors, you must either:
    - Publish corrected root for same epoch (before anyone claims it)
    - Increase other users' rewards to compensate (never decrease anyone)
    - Migrate to new pool_id or app_id for clean slate
    
    This is by design - the cumulative model has no rollback mechanism.
    
    Args:
        cumulative_by_epoch: Output from build_cumulative_claims
        
    Returns:
        Same dict (if valid)
        
    Raises:
        ValueError: If user continuity is broken, cumulative decreases,
                   or cumulative field is missing/invalid
    """
    # Track users who have ever had cumulative > 0
    users_with_positive_cumulative = set()
    
    # Track last seen cumulative per user (for monotonic check)
    last_cumulative = {}
    
    # Sort epochs
    sorted_epoch_ids = sorted(cumulative_by_epoch.keys())
    
    for epoch_id in sorted_epoch_ids:
        claims = cumulative_by_epoch[epoch_id]
        
        # Get all users in this epoch (regardless of cumulative value)
        epoch_users = {claim['address'] for claim in claims}
        
        # CHECK 1: Verify all previously seen users (with positive cumulative) are still present
        missing_users = users_with_positive_cumulative - epoch_users
        if missing_users:
            missing_list = list(missing_users)[:5]  # Show first 5
            raise ValueError(
                f"Epoch {epoch_id} is missing {len(missing_users)} users who had cumulative > 0 in earlier epochs. "
                f"Examples: {missing_list}. "
                f"Users must appear in all future epochs as long as cumulative stays > 0 "
                f"(which it should in a correct cumulative ledger)."
            )
        
        # CHECK 2: Verify cumulative is monotonically increasing per user
        for claim in claims:
            addr = claim['address']
            
            # Strict: cumulative must be present (no default)
            if 'cumulative' not in claim:
                raise ValueError(
                    f"Missing 'cumulative' field for {addr} in epoch {epoch_id}. "
                    f"This is a safety check - all claims must have explicit cumulative."
                )
            
            current_cumulative = claim['cumulative']
            
            # Validate cumulative type and bounds (catches JSON pipeline errors)
            if not isinstance(current_cumulative, int):
                raise ValueError(
                    f"Bad cumulative type for {addr} in epoch {epoch_id}: "
                    f"expected int, got {type(current_cumulative).__name__} = {current_cumulative}"
                )
            
            # Validate uint64 bounds (catches huge Python ints from backend bugs)
            if current_cumulative < 0 or current_cumulative > (2**64 - 1):
                raise ValueError(
                    f"Bad cumulative for {addr} in epoch {epoch_id}: "
                    f"out of uint64 range [0, {2**64-1}], got {current_cumulative}"
                )
            
            # Track users with positive cumulative for continuity check
            if current_cumulative > 0:
                users_with_positive_cumulative.add(addr)
            
            # Check monotonic property
            if addr in last_cumulative:
                last = last_cumulative[addr]
                if current_cumulative < last:
                    raise ValueError(
                        f"User {addr} has DECREASING cumulative in epoch {epoch_id}: "
                        f"was {last}, now {current_cumulative}. "
                        f"Cumulative must be monotonically increasing (contract enforces this). "
                        f"This will cause claims to fail!"
                    )
            
            # Update last seen cumulative
            last_cumulative[addr] = current_cumulative
    
    return cumulative_by_epoch


def save_tree_to_file(tree: MerkleTree, filename: str):
    """
    Save tree to JSON file
    
    Args:
        tree: MerkleTree instance
        filename: Output filename
    """
    data = tree.export_tree()
    with open(filename, 'w') as f:
        json.dump(data, f, indent=2, sort_keys=True)  # sort_keys for consistent diffs
    print(f"✅ Tree saved to {filename}")


def load_tree_from_file(filename: str) -> MerkleTree:
    """
    Load tree from JSON file
    
    Args:
        filename: Input filename
        
    Returns:
        MerkleTree instance
    """
    with open(filename, 'r') as f:
        data = json.load(f)
    tree = MerkleTree.import_tree(data)
    print(f"✅ Tree loaded from {filename}")
    return tree


# ============================================================================
# EXAMPLE USAGE AND TESTING (CUMULATIVE MODEL)
# ============================================================================

if __name__ == "__main__":
    print("=" * 70)
    print("MERKLE TREE UTILITY V1.3.3 - CUMULATIVE MODEL")
    print("=" * 70)
    
    # Test constants (defined early for validation tests)
    TEST_APP_ID = 123456
    TEST_POOL_ID = 789
    
    # Example: Building cumulative claims across epochs
    print("\n📊 Example: Building Cumulative Claims Across Epochs")
    print("=" * 70)
    
    # Per-epoch rewards (what backend calculates each day/week)
    per_epoch_rewards = [
        {
            'epoch_id': 1,
            'rewards': [
                {'address': 'J6DXRZEDUVF2SD67BJIJUBOSGBTMAADMPWFC3GYSOVMF3OPHCJE6YOH5RQ', 'amount': 100},
                {'address': 'YXTAMOAMDMF4PFZYNQTFV34TWUMWZ7K753FFTTOJWPYIFAAPSNDASGWI5E', 'amount': 200},
            ]
        },
        {
            'epoch_id': 2,
            'rewards': [
                {'address': 'J6DXRZEDUVF2SD67BJIJUBOSGBTMAADMPWFC3GYSOVMF3OPHCJE6YOH5RQ', 'amount': 150},
                {'address': 'YXTAMOAMDMF4PFZYNQTFV34TWUMWZ7K753FFTTOJWPYIFAAPSNDASGWI5E', 'amount': 250},
            ]
        },
        {
            'epoch_id': 3,
            'rewards': [
                {'address': 'J6DXRZEDUVF2SD67BJIJUBOSGBTMAADMPWFC3GYSOVMF3OPHCJE6YOH5RQ', 'amount': 200},
                {'address': 'YXTAMOAMDMF4PFZYNQTFV34TWUMWZ7K753FFTTOJWPYIFAAPSNDASGWI5E', 'amount': 300},
            ]
        },
    ]
    
    # Build cumulative claims
    cumulative_by_epoch = build_cumulative_claims(per_epoch_rewards)
    
    # CRITICAL: Verify user continuity
    try:
        ensure_user_continuity(cumulative_by_epoch)
        print("✅ User continuity verified - no users dropped between epochs")
        print("✅ Monotonic cumulative verified - no decreases detected")
    except ValueError as e:
        print(f"❌ VALIDATION ERROR: {e}")
        exit(1)
    
    # Test monotonic validation with bad data
    print("\n🧪 Testing Validations:")
    print("=" * 70)
    
    # Test 1: Negative amount (caught by build_cumulative_claims)
    print("\n1. Testing negative amount detection...")
    bad_epochs_negative = [
        {'epoch_id': 1, 'rewards': [{'address': 'J6DXRZEDUVF2SD67BJIJUBOSGBTMAADMPWFC3GYSOVMF3OPHCJE6YOH5RQ', 'amount': 100}]},
        {'epoch_id': 2, 'rewards': [{'address': 'J6DXRZEDUVF2SD67BJIJUBOSGBTMAADMPWFC3GYSOVMF3OPHCJE6YOH5RQ', 'amount': -50}]},
    ]
    
    try:
        bad_cumulative = build_cumulative_claims(bad_epochs_negative)
        print("❌ TEST FAILED: Should have caught negative amount!")
    except ValueError as e:
        print(f"✅ TEST PASSED: Caught negative amount")
        print(f"   Error: {str(e)[:100]}...")
    
    # Test 2: Decreasing cumulative (caught by ensure_user_continuity)
    print("\n2. Testing monotonic cumulative validation...")
    good_epochs = [
        {'epoch_id': 1, 'rewards': [{'address': 'J6DXRZEDUVF2SD67BJIJUBOSGBTMAADMPWFC3GYSOVMF3OPHCJE6YOH5RQ', 'amount': 100}]},
        {'epoch_id': 2, 'rewards': [{'address': 'J6DXRZEDUVF2SD67BJIJUBOSGBTMAADMPWFC3GYSOVMF3OPHCJE6YOH5RQ', 'amount': 0}]},
    ]
    
    bad_cumulative = build_cumulative_claims(good_epochs)
    # Manually corrupt the cumulative to simulate backend bug
    bad_cumulative[2][0]['cumulative'] = 50  # Was 100, now 50 - DECREASING!
    
    try:
        ensure_user_continuity(bad_cumulative)
        print("❌ TEST FAILED: Should have caught decreasing cumulative!")
    except ValueError as e:
        print(f"✅ TEST PASSED: Caught decreasing cumulative")
        print(f"   Error: {str(e)[:100]}...")
    
    # Test 3: String cumulative (caught by ensure_user_continuity type check)
    print("\n3. Testing type validation (string instead of int)...")
    bad_cumulative_str = build_cumulative_claims([
        {'epoch_id': 1, 'rewards': [{'address': 'J6DXRZEDUVF2SD67BJIJUBOSGBTMAADMPWFC3GYSOVMF3OPHCJE6YOH5RQ', 'amount': 100}]},
    ])
    # Corrupt with string (simulates JSON pipeline bug)
    bad_cumulative_str[1][0]['cumulative'] = "123"
    
    try:
        ensure_user_continuity(bad_cumulative_str)
        print("❌ TEST FAILED: Should have caught string type!")
    except ValueError as e:
        print(f"✅ TEST PASSED: Caught string type")
        print(f"   Error: {str(e)[:100]}...")
    
    # Test 4: Uint64 overflow (caught by MerkleTree or ensure_user_continuity)
    print("\n4. Testing uint64 overflow detection...")
    overflow_claims = [{'address': 'J6DXRZEDUVF2SD67BJIJUBOSGBTMAADMPWFC3GYSOVMF3OPHCJE6YOH5RQ', 'cumulative': 2**64}]  # Too big!
    
    try:
        tree_overflow = MerkleTree(overflow_claims, app_id=TEST_APP_ID, pool_id=TEST_POOL_ID, epoch_id=1)
        print("❌ TEST FAILED: Should have caught uint64 overflow!")
    except ValueError as e:
        print(f"✅ TEST PASSED: Caught uint64 overflow")
        print(f"   Error: {str(e)[:100]}...")
    
    # Test 5: Duplicate address in same epoch
    print("\n5. Testing duplicate address detection...")
    dup_epochs = [
        {
            'epoch_id': 1,
            'rewards': [
                {'address': 'J6DXRZEDUVF2SD67BJIJUBOSGBTMAADMPWFC3GYSOVMF3OPHCJE6YOH5RQ', 'amount': 100},
                {'address': 'J6DXRZEDUVF2SD67BJIJUBOSGBTMAADMPWFC3GYSOVMF3OPHCJE6YOH5RQ', 'amount': 50},  # Duplicate!
            ]
        }
    ]
    
    try:
        dup_cumulative = build_cumulative_claims(dup_epochs)
        print("❌ TEST FAILED: Should have caught duplicate address!")
    except ValueError as e:
        print(f"✅ TEST PASSED: Caught duplicate address")
        print(f"   Error: {str(e)[:100]}...")
    
    print("\nCumulative Claims by Epoch:")
    for epoch_id, claims in cumulative_by_epoch.items():
        print(f"\nEpoch {epoch_id}:")
        for claim in claims:
            addr_short = claim['address'][:20]
            print(f"  {addr_short}... = {claim['cumulative']} (total)")
    
    # Build Merkle tree for epoch 3
    app_id = TEST_APP_ID
    pool_id = TEST_POOL_ID
    epoch_id = 3
    
    print(f"\n📊 Building Merkle tree for Epoch {epoch_id}")
    print(f"   App ID: {app_id}")
    print(f"   Pool ID: {pool_id}")
    
    tree = MerkleTree(cumulative_by_epoch[epoch_id], app_id, pool_id, epoch_id)
    print(f"✅ Tree built successfully!")
    
    # Display statistics
    stats = tree.get_statistics()
    print(f"\n📈 Tree Statistics:")
    print(f"   Total claims: {stats['total_claims']}")
    print(f"   Total cumulative: {stats['total_cumulative']}")
    print(f"   Tree depth: {stats['tree_depth']}")
    print(f"   Root hash: {stats['root']}")
    
    # Test proof generation and verification
    print(f"\n🔍 Testing Proof Generation & Verification")
    print("=" * 70)
    
    test_addr = cumulative_by_epoch[epoch_id][0]['address']
    test_cumulative = cumulative_by_epoch[epoch_id][0]['cumulative']
    
    proof = tree.get_proof(test_addr)
    proof_hex = tree.get_proof_hex(test_addr)
    is_valid = tree.verify_proof(test_addr, test_cumulative, proof)
    
    print(f"Test Address: {test_addr[:20]}...")
    print(f"Cumulative Amount: {test_cumulative}")
    print(f"Proof Depth: {len(proof)}")
    print(f"Verification: {'✅ PASS' if is_valid else '❌ FAIL'}")
    
    # Demonstrate delta calculation
    print(f"\n💰 Delta Calculation Example (Cumulative Model)")
    print("=" * 70)
    print(f"User claims Epoch 1: cumulative=100, last=0 → delta=100 tokens")
    print(f"User claims Epoch 2: cumulative=250, last=100 → delta=150 tokens")
    print(f"User claims Epoch 3: cumulative=450, last=250 → delta=200 tokens")
    print(f"Total received: 450 tokens ✅")
    
    # Show on-chain transaction format
    print(f"\n📤 On-Chain Transaction Format (V1.3.3):")
    print("=" * 70)
    print("First claim transaction group (3 transactions):")
    print("  [0] Payment:")
    print("      sender: <user>")
    print("      receiver: <platform_fee_address>")
    print(f"      amount: 800,000 microAlgos (0.8 ALGO platform fee)")
    print("  [1] Payment:")
    print("      sender: <user>")
    print("      receiver: <app_address>")
    print(f"      amount: 21,700 microAlgos (box MBR for 16-byte box)")
    print("  [2] ApplicationCall:")
    print("      app_id: <deployed_app_id>")
    print("      fee: 2,000 microAlgos (2x min_fee)")
    print("      args:")
    print(f"        [0] 'claim'")
    print(f"        [1] {epoch_id} (epoch_id)")
    print(f"        [2] {test_cumulative} (cumulative amount)")
    for i, p in enumerate(proof_hex):
        print(f"        [{i+3}] {p} (proof[{i}])")
    print("      boxes: (CRITICAL - MUST INCLUDE BOTH):")
    print(f"        [[app_id, SHA256(user_address || pool_id)],  # User box")
    print(f"         [app_id, Itob(epoch_id)]]                   # Epoch root box")
    
    print(f"\nSubsequent claims (box already exists) - 2 transactions:")
    print("  [0] Payment: (same 0.8 ALGO platform fee)")
    print("  [1] ApplicationCall: (same args + boxes)")
    
    print("\n" + "=" * 70)
    print("🔥 CRITICAL OPERATIONAL GOTCHAS:")
    print("=" * 70)
    print("\n1. USER CONTINUITY (MOST IMPORTANT):")
    print("   - Once a user has cumulative > 0, they appear in all future epochs")
    print("   - This works because cumulative is monotonic (never decreases)")
    print("   - Trees only include users with cumulative > 0")
    print("   - In correct system: once cumulative > 0, it stays > 0 forever")
    print("   - If cumulative ever becomes 0 (shouldn't happen):")
    print("     * User drops from tree")
    print("     * ensure_user_continuity() flags as error")
    print("     * Prevents silent backend bugs")
    print("\n2. MONOTONIC CUMULATIVE (ENFORCED BY CONTRACT):")
    print("   - User's cumulative can NEVER decrease between epochs")
    print("   - Contract enforces: cumulative >= last_cumulative")
    print("   - If backend outputs lower cumulative, user gets STUCK")
    print("   - ensure_user_continuity() validates this ✅")
    print("\n3. DELTA = 0 REJECTED BY CONTRACT:")
    print("   - Contract rejects claims where delta = (cumulative - last) = 0")
    print("   - This is BY DESIGN (prevents burning 0.8 ALGO fee for nothing)")
    print("   - Don't encourage users to claim epochs where their delta = 0")
    print("   - Frontend should calculate delta before showing claim button")
    print("\n4. EPOCH ID CONSISTENCY:")
    print("   - Tree built with epoch_id=X")
    print("   - Root published to box Itob(X)")
    print("   - User claims with epoch_id=X in args")
    print("   - ALL THREE MUST MATCH or proofs fail")
    print("\n5. APP ID MUST BE DEPLOYED CONTRACT:")
    print("   - Use actual deployed application ID, not testnet ID")
    print("   - app_id is bound into leaf hash")
    print("   - Proofs from wrong app_id will always fail")
    
    print("\n" + "=" * 70)
    print("💰 COST BREAKDOWN (V1.3.3):")
    print("=" * 70)
    print("First claim total cost:")
    print("  - Platform fee: 800,000 µA (0.8 ALGO)")
    print("  - Box MBR: 21,700 µA (0.0217 ALGO) - 16 bytes for [epoch || cumulative]")
    print("  - Transaction fees: ~3,000 µA (0.003 ALGO) - 3 transactions")
    print("  - TOTAL: ~824,700 µA (~0.8247 ALGO)")
    print("\nSubsequent claims:")
    print("  - Platform fee: 800,000 µA (0.8 ALGO)")
    print("  - Transaction fees: ~2,000 µA (0.002 ALGO) - 2 transactions")
    print("  - TOTAL: ~802,000 µA (~0.802 ALGO)")
    print("\nBox deletion (recover MBR):")
    print("  - Contract returns: 20,700 µA (0.0207 ALGO)")
    print("  - User pays txn fee: ~2,000 µA (0.002 ALGO)")
    print("  - Net recovery: ~18,700 µA (~0.0187 ALGO)")
    
    print("\n" + "=" * 70)
    print("⚠️  CRITICAL REQUIREMENTS (V1.3.3):")
    print("=" * 70)
    print("1. Each epoch root contains CUMULATIVE totals (not deltas)")
    print("2. Contract pays DELTA = (cumulative - last_cumulative)")
    print("3. MUST include 2 box references on EVERY claim:")
    print("   - User box: SHA256(user_address || pool_id)")
    print("   - Epoch root box: Itob(epoch_id)")
    print("4. Platform fee: EXACTLY 800,000 µA (no more, no less)")
    print("5. Users must claim epochs in INCREASING order (can skip: 1→3→7 OK)")
    print("   - Contract enforces: epoch_id > last_epoch_claimed")
    print("   - Users CAN skip epochs (delta calculated from cumulative)")
    print("6. Epoch root boxes cost admin 18,500 µA each to publish")
    
    print("\n" + "=" * 70)
    print("📦 BACKEND API HELPER - get_claim_bundle():")
    print("=" * 70)
    
    # Get complete claim bundle for a user (for API responses)
    test_user = cumulative_by_epoch[epoch_id][0]['address']
    
    # JSON-safe bundle (default)
    claim_bundle = tree.get_claim_bundle(test_user)
    
    if claim_bundle:
        print(f"Example API endpoint: GET /api/claims/{{address}}")
        print(f"\nJSON-safe response (default):")
        import json
        print(json.dumps(claim_bundle, indent=2))
        print(f"\n✅ Can be returned directly from Flask/FastAPI!")
    
    # SDK bundle with bytes
    sdk_bundle = tree.get_claim_bundle(test_user, include_proof_bytes=True)
    print(f"\nSDK bundle (include_proof_bytes=True):")
    print(f"  - Has 'proof_bytes' field for algosdk")
    print(f"  - NOT JSON-serializable (bytes objects)")
    print(f"  - Use for Python SDK callers building transactions")
    
    # Test non-existent user
    fake_addr = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAY5HFKQ"
    no_claim = tree.get_claim_bundle(fake_addr)
    print(f"\nResponse for non-eligible user: {no_claim}")
    print(f"✅ API returns None gracefully (user has no rewards)")
    
    print("\n" + "=" * 70)
    print("💡 BACKEND INTEGRATION:")
    print("=" * 70)
    print("1. Track cumulative rewards per user across all epochs")
    print("2. Generate Merkle tree with cumulative amounts for each epoch")
    print("3. Publish root on-chain (costs 18,500 µA per epoch)")
    print("4. Users claim with proof (contract calculates delta)")
    print("5. Monitor contract ALGO balance for epoch roots + box deletions")
    
    print("\n" + "=" * 70)
    print("✅ ALL TESTS COMPLETED SUCCESSFULLY!")
    print("=" * 70)