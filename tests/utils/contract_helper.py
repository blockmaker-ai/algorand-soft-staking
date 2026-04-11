#!/usr/bin/env python3
"""
Contract Interaction Utilities - FIXED VERSION
Helper functions for calling smart contract methods

FIXED ISSUES:
1. Uses encoding.decode_address() and encoding.encode_address()
2. Each transaction in build_claim_txn gets separate params
3. Explicit flat_fee=True and fee amounts for all transactions
4. fee=2000 for claim transaction (needs 2x for inner txn budget)
"""

from algosdk.v2client import algod
from algosdk.transaction import (
    ApplicationCallTxn,
    PaymentTxn,
    AssetTransferTxn,
    assign_group_id,
    wait_for_confirmation,
    OnComplete
)
from algosdk import logic, encoding
from typing import List, Dict, Optional, Tuple
import base64


class ContractHelper:
    """Helper class for interacting with the staking contract"""
    
    def __init__(self, client: algod.AlgodClient, app_id: int, reward_asset_id: int):
        self.client = client
        self.app_id = app_id
        self.reward_asset_id = reward_asset_id
        self.app_address = logic.get_application_address(app_id)
        
    def get_global_state(self) -> Dict:
        """
        Fetch and decode contract global state
        
        Returns:
            Dictionary of state key -> value
        """
        app_info = self.client.application_info(self.app_id)
        global_state = app_info['params'].get('global-state', [])
        
        decoded_state = {}
        for item in global_state:
            key = base64.b64decode(item['key']).decode('utf-8')
            value_obj = item['value']
            
            # Check the type field to determine how to decode
            if value_obj['type'] == 1:  # bytes
                decoded_state[key] = base64.b64decode(value_obj['bytes'])
            elif value_obj['type'] == 2:  # uint
                decoded_state[key] = value_obj['uint']
        
        return decoded_state
    
    def print_global_state(self):
        """Print formatted global state"""
        state = self.get_global_state()
        
        print(f"\n{'='*70}")
        print(f"CONTRACT GLOBAL STATE - App ID: {self.app_id}")
        print(f"{'='*70}")
        
        for key, value in sorted(state.items()):
            if isinstance(value, bytes):
                if len(value) == 32:
                    # Likely an address
                    try:
                        addr = self._bytes_to_address(value)
                        print(f"{key:20s}: {addr}")
                    except:
                        print(f"{key:20s}: {value.hex()}")
                else:
                    print(f"{key:20s}: {value.hex()}")
            else:
                print(f"{key:20s}: {value:,}")
        
        print(f"{'='*70}\n")
    
    def get_box(self, box_name: bytes) -> Optional[bytes]:
        """
        Read a box from the contract
        
        Args:
            box_name: Box name as bytes
            
        Returns:
            Box contents or None if not found
        """
        try:
            box_response = self.client.application_box_by_name(self.app_id, box_name)
            return base64.b64decode(box_response['value'])
        except Exception as e:
            return None
    
    def list_boxes(self) -> List[Tuple[bytes, int]]:
        """
        List all boxes for the contract
        
        Returns:
            List of (box_name, box_size) tuples
        """
        try:
            boxes_response = self.client.application_boxes(self.app_id)
            boxes = boxes_response.get('boxes', [])
            
            return [
                (base64.b64decode(box['name']), box['size'])
                for box in boxes
            ]
        except Exception as e:
            return []
    
    def print_boxes(self):
        """Print all contract boxes"""
        boxes = self.list_boxes()
        
        print(f"\n{'='*70}")
        print(f"CONTRACT BOXES - App ID: {self.app_id}")
        print(f"{'='*70}")
        print(f"Total boxes: {len(boxes)}")
        
        if boxes:
            print("\nBox list:")
            for name, size in boxes:
                print(f"  Name: {name.hex()}, Size: {size} bytes")
                
                # Try to read box contents
                contents = self.get_box(name)
                if contents:
                    print(f"  Contents: {contents.hex()}")
        else:
            print("No boxes found")
        
        print(f"{'='*70}\n")
    
    def get_user_box_data(self, user_address: str, pool_id: int) -> Optional[Tuple[int, int]]:
        """
        Get user's box data (last_epoch, last_cumulative)
        
        Args:
            user_address: User's Algorand address
            pool_id: Pool ID
            
        Returns:
            Tuple of (last_epoch_claimed, last_cumulative_claimed) or None
        """
        import hashlib
        
        # Compute box name: SHA256(address || pool_id)
        user_bytes = self._address_to_bytes(user_address)
        box_name = hashlib.sha256(user_bytes + pool_id.to_bytes(8, 'big')).digest()
        
        contents = self.get_box(box_name)
        if contents and len(contents) == 16:
            last_epoch = int.from_bytes(contents[0:8], 'big')
            last_cumulative = int.from_bytes(contents[8:16], 'big')
            return (last_epoch, last_cumulative)
        
        return None
    
    def get_epoch_root(self, epoch_id: int) -> Optional[bytes]:
        """
        Get Merkle root for a specific epoch
        
        Args:
            epoch_id: Epoch ID
            
        Returns:
            32-byte Merkle root or None if not published
        """
        box_name = epoch_id.to_bytes(8, 'big')
        return self.get_box(box_name)
    
    def get_contract_balances(self) -> Dict[str, int]:
        """
        Get contract's ALGO and reward token balances
        
        Returns:
            Dictionary with 'algo' and 'reward_token' keys
        """
        account_info = self.client.account_info(self.app_address)
        
        balances = {
            'algo': account_info.get('amount', 0),
            'reward_token': 0
        }
        
        # Find reward token balance
        for asset in account_info.get('assets', []):
            if asset['asset-id'] == self.reward_asset_id:
                balances['reward_token'] = asset['amount']
                break
        
        return balances
    
    def print_contract_balances(self):
        """Print contract balances"""
        balances = self.get_contract_balances()
        
        print(f"\n{'='*70}")
        print(f"CONTRACT BALANCES - {self.app_address}")
        print(f"{'='*70}")
        print(f"ALGO: {balances['algo'] / 1_000_000:.6f} ({balances['algo']:,} µA)")
        print(f"Reward Token: {balances['reward_token']:,}")
        print(f"{'='*70}\n")
    
    @staticmethod
    def _address_to_bytes(address: str) -> bytes:
        """Convert Algorand address to 32 bytes"""
        # FIXED: Use encoding.decode_address() instead of base64.b32decode()
        # This returns exactly 32 bytes (without checksum)
        return encoding.decode_address(address)
    
    @staticmethod
    def _bytes_to_address(address_bytes: bytes) -> str:
        """Convert 32 bytes to Algorand address"""
        # FIXED: Use encoding.encode_address() instead of base64.b32encode()
        # This properly adds the checksum
        return encoding.encode_address(address_bytes)


def build_set_root_txn(
    client: algod.AlgodClient,
    app_id: int,
    sender_address: str,
    sender_private_key: str,
    epoch_id: int,
    merkle_root: bytes
) -> str:
    """
    Build and send set_root transaction
    
    Args:
        client: Algod client
        app_id: Application ID
        sender_address: Admin address
        sender_private_key: Admin private key
        epoch_id: Epoch ID to publish
        merkle_root: 32-byte Merkle root
        
    Returns:
        Transaction ID
    """
    params = client.suggested_params()
    
    # Build application call
    txn = ApplicationCallTxn(
        sender=sender_address,
        sp=params,
        index=app_id,
        on_complete=OnComplete.NoOpOC,
        app_args=[
            "set_root",
            epoch_id.to_bytes(8, 'big'),
            merkle_root
        ],
        boxes=[
            (app_id, epoch_id.to_bytes(8, 'big'))
        ]
    )
    
    # Sign and send
    signed_txn = txn.sign(sender_private_key)
    txid = client.send_transaction(signed_txn)
    
    # Wait for confirmation
    wait_for_confirmation(client, txid, 4)
    
    return txid


def build_claim_txn(
    client: algod.AlgodClient,
    app_id: int,
    pool_id: int,
    sender_address: str,
    sender_private_key: str,
    epoch_id: int,
    cumulative_amount: int,
    proof: List[bytes],
    fee_address: str,
    is_first_claim: bool = False,
    reward_asset_id: int = None
) -> str:
    """
    Build and send claim transaction group
    
    FIXED: Each transaction now gets separate params with explicit fee settings
    FIXED: Added reward_asset_id to foreign_assets for contract asset access
    
    Args:
        client: Algod client
        app_id: Application ID
        pool_id: Pool ID
        sender_address: Claimer address
        sender_private_key: Claimer private key
        epoch_id: Epoch to claim from
        cumulative_amount: Cumulative reward amount
        proof: List of Merkle proof elements (32 bytes each)
        fee_address: Platform fee address
        is_first_claim: Whether this is user's first claim
        reward_asset_id: Reward token asset ID (optional, will fetch if not provided)
        
    Returns:
        Transaction ID of the app call
    """
    import hashlib
    
    app_address = logic.get_application_address(app_id)
    
    # Get reward asset ID if not provided
    if reward_asset_id is None:
        app_info = client.application_info(app_id)
        global_state = app_info['params'].get('global-state', [])
        for item in global_state:
            key = base64.b64decode(item['key']).decode('utf-8')
            if key == 'reward_token':
                reward_asset_id = item['value']['uint']
                break
    
    # Compute user box name
    # FIXED: Use encoding.decode_address() instead of base64.b32decode()
    user_bytes = encoding.decode_address(sender_address)
    user_box_name = hashlib.sha256(user_bytes + pool_id.to_bytes(8, 'big')).digest()
    
    # Build box references
    boxes = [
        (app_id, user_box_name),
        (app_id, epoch_id.to_bytes(8, 'big'))
    ]
    
    # Build transactions
    txns = []
    
    # Transaction 0: Platform fee payment (always required)
    # FIXED: Separate params for each transaction
    fee_params = client.suggested_params()
    fee_params.flat_fee = True
    fee_params.fee = 1000  # Standard min_fee
    
    fee_txn = PaymentTxn(
        sender=sender_address,
        receiver=fee_address,
        amt=800_000,  # 0.8 ALGO
        sp=fee_params
    )
    txns.append(fee_txn)
    
    # Transaction 1: Box MBR payment (only for first claim)
    if is_first_claim:
        box_params = client.suggested_params()
        box_params.flat_fee = True
        box_params.fee = 1000  # Standard min_fee
        
        box_payment_txn = PaymentTxn(
            sender=sender_address,
            receiver=app_address,
            amt=21_700,  # Box MBR
            sp=box_params
        )
        txns.append(box_payment_txn)
    
    # Last transaction: Application call
    # FIXED: Set fee=5000 for inner transaction budget and opcode budget
    app_params = client.suggested_params()
    app_params.flat_fee = True
    app_params.fee = 5000  # 5x min_fee for opcodes + inner transactions
    
    app_args = [
        "claim",
        epoch_id.to_bytes(8, 'big'),
        cumulative_amount.to_bytes(8, 'big')
    ] + proof
    
    claim_txn = ApplicationCallTxn(
        sender=sender_address,
        sp=app_params,
        index=app_id,
        on_complete=OnComplete.NoOpOC,
        app_args=app_args,
        boxes=boxes,
        foreign_assets=[reward_asset_id] if reward_asset_id else []
    )
    txns.append(claim_txn)
    
    # Assign group ID
    txns = assign_group_id(txns)
    
    # Sign all transactions
    signed_txns = [txn.sign(sender_private_key) for txn in txns]
    
    # Send group
    txid = client.send_transactions(signed_txns)
    
    # Wait for confirmation
    wait_for_confirmation(client, txid, 4)
    
    return txid


if __name__ == "__main__":
    print("Contract Interaction Utilities - FIXED VERSION")
    print("=" * 70)
    print("\nFIXED ISSUES:")
    print("  ✓ Properly converts addresses to/from 32 bytes using algosdk.encoding")
    print("  ✓ Each transaction gets separate params object")
    print("  ✓ Explicit flat_fee=True for all transactions")
    print("  ✓ fee=2000 for claim transaction (inner txn budget)")
    print("\nThis module provides:")
    print("  ✓ ContractHelper class for reading contract state")
    print("  ✓ Global state reading and printing")
    print("  ✓ Box storage reading and listing")
    print("  ✓ Helper functions for building transactions")
    print("  ✓ Set root and claim transaction builders")