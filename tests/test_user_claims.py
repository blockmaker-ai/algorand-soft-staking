#!/usr/bin/env python3
"""
TEST SUITE: User Claim Operations - FIXED VERSION
Tests for user opt-in, claims, delta payments, Merkle proof verification

FIXED: Changed to use start_epoch=20 for proper Merkle root generation
This ensures epochs 20, 21, 22 have roots computed with correct epoch IDs
"""

import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from algosdk.v2client import algod
from algosdk.transaction import (
    ApplicationCallTxn,
    PaymentTxn,
    OnComplete,
    assign_group_id,
    wait_for_confirmation
)
from algosdk import logic
from typing import Dict, List
import hashlib

from utils.contract_helper import ContractHelper, build_claim_txn
from utils.account_manager import TestAccount
from fixtures.test_data import TestData, create_epoch_scenario, calculate_expected_delta
from algosdk import mnemonic, account
import os
from dotenv import load_dotenv


class UserClaimTests:
    """Test suite for user claim operations"""
    
    def __init__(
        self,
        client: algod.AlgodClient,
        app_id: int,
        pool_id: int,
        reward_asset_id: int,
        fee_address: str,
        test_users: List[TestAccount]
    ):
        self.client = client
        self.app_id = app_id
        self.pool_id = pool_id
        self.reward_asset_id = reward_asset_id
        self.fee_address = fee_address
        self.test_users = test_users
        self.helper = ContractHelper(client, app_id, reward_asset_id)
        
        self.results = {
            'passed': 0,
            'failed': 0,
            'tests': []
        }
    

    def _publish_epoch_roots(self, epochs: Dict, admin_pk: str):
        """Publish epoch roots for testing"""
        load_dotenv('../.env')
        
        for epoch_id, epoch_data in epochs.items():
            root = epoch_data['root']
            
            try:
                params = self.client.suggested_params()
                txn = ApplicationCallTxn(
                    sender=account.address_from_private_key(admin_pk),
                    sp=params,
                    index=self.app_id,
                    on_complete=OnComplete.NoOpOC,
                    app_args=[
                        "set_root",
                        epoch_id.to_bytes(8, 'big'),
                        root
                    ],
                    boxes=[
                        (self.app_id, epoch_id.to_bytes(8, 'big'))
                    ]
                )
                
                signed = txn.sign(admin_pk)
                txid = self.client.send_transaction(signed)
                wait_for_confirmation(self.client, txid, 4)
                print(f"  ✓ Published root for epoch {epoch_id}")
            except Exception as e:
                # Root might already be published
                if "duplicate" not in str(e).lower():
                    print(f"  ⚠️  Epoch {epoch_id}: {str(e)[:50]}")

    def run_all_tests(self):
        """Run all user claim tests"""
        print(f"\n{'='*70}")
        print("USER CLAIM OPERATIONS TEST SUITE")
        print(f"{'='*70}\n")
        
        # Setup: Create epoch scenarios
        # FIXED: Use start_epoch=20 to create epochs 20, 21, 22 with proper Merkle roots
        print("FIXED: Using epochs 20, 21, 22 for user tests (admin uses 10-12)")
        
        epochs = create_epoch_scenario(
            self.test_users,
            self.app_id,
            self.pool_id,
            "default",
            start_epoch=20  # FIXED: Creates epochs 20, 21, 22 with correct roots
        )
        
        # Publish roots for these test users
        print("Publishing epoch roots for test users...")
        load_dotenv('../.env')
        admin_pk = mnemonic.to_private_key(os.getenv('ADMIN_MNEMONIC'))
        self._publish_epoch_roots(epochs, admin_pk)
        print()
        
        # Test user opt-in
        self.test_user_optin_success()
        
        # Test first claim (requires box payment) - EPOCH 20
        self.test_first_claim_success(epochs, 20)
        
        # Test subsequent claim (delta payment) - EPOCH 21
        self.test_subsequent_claim_delta(epochs, 21)
        
        # Test zero-delta rejection
        self.test_zero_delta_rejected(epochs)
        
        # Test invalid proof rejection - EPOCH 20
        self.test_invalid_proof_rejected(epochs, 20)
        
        # Test missing fee payment - EPOCH 20
        self.test_missing_fee_payment(epochs, 20)
        
        # Test missing box payment on first claim - EPOCH 20
        self.test_missing_box_payment_first_claim(epochs, 20)
        
        # Test backwards epoch claim
        self.test_backwards_epoch_rejected(epochs)
        
        # Test paused contract
        self.test_claim_while_paused()
        
        # Print results
        self._print_results()
    
    def test_user_optin_success(self):
        """Test user opt-in"""
        test_name = "User Opt-In - Success"
        
        try:
            user = self.test_users[0]
            params = self.client.suggested_params()
            
            # Opt-in transaction
            txn = ApplicationCallTxn(
                sender=user.address,
                sp=params,
                index=self.app_id,
                on_complete=OnComplete.OptInOC
            )
            
            signed_txn = txn.sign(user.private_key)
            txid = self.client.send_transaction(signed_txn)
            wait_for_confirmation(self.client, txid, 4)
            
            # Verify opt-in
            account_info = self.client.account_info(user.address)
            is_opted_in = any(
                app['id'] == self.app_id
                for app in account_info.get('apps-local-state', [])
            )
            
            assert is_opted_in, "User not opted in"
            
            self._test_passed(test_name, f"User {user.name} opted in successfully")
            
        except Exception as e:
            self._test_failed(test_name, str(e))
    
    def test_first_claim_success(self, epochs: Dict, epoch_num: int = 20):
        """Test first claim with box payment"""
        test_name = "First Claim - Success"
        
        try:
            user = self.test_users[0]
            epoch_data = epochs[epoch_num]
            user_data = epoch_data['users'][0]
            
            # Opt in user if not already
            self._ensure_user_opted_in(user)
            
            # Build and send claim
            txid = build_claim_txn(
                self.client,
                self.app_id,
                self.pool_id,
                user.address,
                user.private_key,
                epoch_id=epoch_num,
                cumulative_amount=user_data['cumulative_amount'],
                proof=user_data['proof'],
                fee_address=self.fee_address,
                is_first_claim=True
            )
            
            # Verify box was created
            box_data = self.helper.get_user_box_data(user.address, self.pool_id)
            assert box_data is not None, "Box not created"
            assert box_data[0] == epoch_num, "Epoch not stored correctly"
            assert box_data[1] == user_data['cumulative_amount'], "Cumulative not stored correctly"
            
            self._test_passed(
                test_name,
                f"{user.name} claimed {user_data['cumulative_amount']:,} tokens from epoch {epoch_num}"
            )
            
        except Exception as e:
            self._test_failed(test_name, str(e))
    
    def test_subsequent_claim_delta(self, epochs: Dict, epoch_num: int = 21):
        """Test subsequent claim with delta payment"""
        test_name = "Subsequent Claim - Delta Payment"
        
        try:
            user = self.test_users[0]
            epoch_data = epochs[epoch_num]
            user_data = epoch_data['users'][0]
            
            # Get previous cumulative
            box_data_before = self.helper.get_user_box_data(user.address, self.pool_id)
            assert box_data_before is not None, "User has not claimed before"
            
            prev_cumulative = box_data_before[1]
            expected_delta = calculate_expected_delta(
                user_data['cumulative_amount'],
                prev_cumulative
            )
            
            # Get reward token balance before
            balance_before = self._get_user_asset_balance(user.address)
            
            # Build and send claim (no box payment for subsequent claim)
            txid = build_claim_txn(
                self.client,
                self.app_id,
                self.pool_id,
                user.address,
                user.private_key,
                epoch_id=epoch_num,
                cumulative_amount=user_data['cumulative_amount'],
                proof=user_data['proof'],
                fee_address=self.fee_address,
                is_first_claim=False
            )
            
            # Get balance after
            balance_after = self._get_user_asset_balance(user.address)
            actual_delta = balance_after - balance_before
            
            # Verify delta payment
            assert actual_delta == expected_delta, \
                f"Delta mismatch: expected {expected_delta}, got {actual_delta}"
            
            # Verify box updated
            box_data_after = self.helper.get_user_box_data(user.address, self.pool_id)
            assert box_data_after[0] == epoch_num, "Epoch not updated"
            assert box_data_after[1] == user_data['cumulative_amount'], "Cumulative not updated"
            
            self._test_passed(
                test_name,
                f"Paid delta of {actual_delta:,} tokens (epoch {epoch_num})"
            )
            
        except Exception as e:
            self._test_failed(test_name, str(e))
    
    def test_zero_delta_rejected(self, epochs: Dict):
        """Test that zero-delta claims are rejected"""
        test_name = "Zero Delta - Rejected"
        
        try:
            user = self.test_users[0]
            
            # Get current box data
            box_data = self.helper.get_user_box_data(user.address, self.pool_id)
            assert box_data is not None, "User has not claimed"
            
            last_epoch, last_cumulative = box_data
            
            # Try to claim same epoch again with same cumulative
            # This would result in zero delta
            
            params = self.client.suggested_params()
            
            # Create dummy proof (won't matter since it should fail on zero delta)
            dummy_proof = [b'\x00' * 32]
            
            app_args = [
                "claim",
                last_epoch.to_bytes(8, 'big'),
                last_cumulative.to_bytes(8, 'big')
            ] + dummy_proof
            
            # Fee payment
            fee_txn = PaymentTxn(
                sender=user.address,
                receiver=self.fee_address,
                amt=800_000,
                sp=params
            )
            
            # User box name
            user_bytes = self.helper._address_to_bytes(user.address)
            user_box_name = hashlib.sha256(user_bytes + self.pool_id.to_bytes(8, 'big')).digest()
            
            # App call
            claim_txn = ApplicationCallTxn(
                sender=user.address,
                sp=params,
                index=self.app_id,
                on_complete=OnComplete.NoOpOC,
                app_args=app_args,
                boxes=[
                    (self.app_id, user_box_name),
                    (self.app_id, last_epoch.to_bytes(8, 'big'))
                ]
            )
            
            # Assign group ID
            txns = assign_group_id([fee_txn, claim_txn])
            signed_txns = [txn.sign(user.private_key) for txn in txns]
            
            try:
                txid = self.client.send_transactions(signed_txns)
                wait_for_confirmation(self.client, txid, 4)
                self._test_failed(test_name, "Zero-delta claim was accepted")
            except Exception:
                self._test_passed(test_name, "Zero-delta claim correctly rejected")
                
        except Exception as e:
            self._test_failed(test_name, f"Test setup failed: {str(e)}")
    
    def test_invalid_proof_rejected(self, epochs: Dict, epoch_num: int = 20):
        """Test that invalid Merkle proofs are rejected"""
        test_name = "Invalid Proof - Rejected"
        
        try:
            user = self.test_users[1]  # Use different user
            self._ensure_user_opted_in(user)
            
            epoch_data = epochs[epoch_num]
            user_data = epoch_data['users'][1]
            
            # Use wrong proof (user 0's proof for user 1's claim)
            wrong_proof = epochs[epoch_num]['users'][0]['proof']
            
            try:
                txid = build_claim_txn(
                    self.client,
                    self.app_id,
                    self.pool_id,
                    user.address,
                    user.private_key,
                    epoch_id=epoch_num,
                    cumulative_amount=user_data['cumulative_amount'],
                    proof=wrong_proof,  # Wrong proof!
                    fee_address=self.fee_address,
                    is_first_claim=True
                )
                self._test_failed(test_name, "Invalid proof was accepted")
            except Exception:
                self._test_passed(test_name, "Invalid proof correctly rejected")
                
        except Exception as e:
            self._test_failed(test_name, f"Test failed: {str(e)}")
    
    def test_missing_fee_payment(self, epochs: Dict, epoch_num: int = 20):
        """Test that claim without fee payment is rejected"""
        test_name = "Missing Fee Payment - Rejected"
        
        try:
            user = self.test_users[2]
            self._ensure_user_opted_in(user)
            
            epoch_data = epochs[epoch_num]
            user_data = epoch_data['users'][2]
            
            params = self.client.suggested_params()
            
            # User box name
            user_bytes = self.helper._address_to_bytes(user.address)
            user_box_name = hashlib.sha256(user_bytes + self.pool_id.to_bytes(8, 'big')).digest()
            
            # Box payment (for first claim)
            box_payment_txn = PaymentTxn(
                sender=user.address,
                receiver=self.helper.app_address,
                amt=21_700,
                sp=params
            )
            
            # App call WITHOUT fee payment
            app_args = [
                "claim",
                epoch_num.to_bytes(8, 'big'),
                user_data['cumulative_amount'].to_bytes(8, 'big')
            ] + user_data['proof']
            
            claim_txn = ApplicationCallTxn(
                sender=user.address,
                sp=params,
                index=self.app_id,
                on_complete=OnComplete.NoOpOC,
                app_args=app_args,
                boxes=[
                    (self.app_id, user_box_name),
                    (self.app_id, epoch_num.to_bytes(8, 'big'))
                ]
            )
            
            # Group without fee payment
            txns = assign_group_id([box_payment_txn, claim_txn])
            signed_txns = [txn.sign(user.private_key) for txn in txns]
            
            try:
                txid = self.client.send_transactions(signed_txns)
                wait_for_confirmation(self.client, txid, 4)
                self._test_failed(test_name, "Claim without fee payment was accepted")
            except Exception:
                self._test_passed(test_name, "Missing fee payment correctly rejected")
                
        except Exception as e:
            self._test_failed(test_name, f"Test failed: {str(e)}")
    
    def test_missing_box_payment_first_claim(self, epochs: Dict, epoch_num: int = 20):
        """Test that first claim without box payment is rejected"""
        test_name = "Missing Box Payment - Rejected"
        
        try:
            user = self.test_users[3]
            self._ensure_user_opted_in(user)
            
            # Try to claim without box payment (will fail on box create)
            epoch_data = epochs[epoch_num]
            user_data = epoch_data['users'][3]
            
            try:
                # This should fail because box payment is missing
                txid = build_claim_txn(
                    self.client,
                    self.app_id,
                    self.pool_id,
                    user.address,
                    user.private_key,
                    epoch_id=epoch_num,
                    cumulative_amount=user_data['cumulative_amount'],
                    proof=user_data['proof'],
                    fee_address=self.fee_address,
                    is_first_claim=False  # Wrong! Should be True
                )
                self._test_failed(test_name, "First claim without box payment was accepted")
            except Exception:
                self._test_passed(test_name, "Missing box payment correctly rejected")
                
        except Exception as e:
            self._test_failed(test_name, f"Test failed: {str(e)}")
    
    def test_backwards_epoch_rejected(self, epochs: Dict):
        """Test that claiming older epoch is rejected"""
        test_name = "Backwards Epoch - Rejected"
        
        # This requires user to have claimed epoch 21 already
        # Skip if not applicable
        
        try:
            user = self.test_users[0]
            box_data = self.helper.get_user_box_data(user.address, self.pool_id)
            
            if box_data is None or box_data[0] < 21:
                self._test_skipped(test_name, "User hasn't claimed epoch 21 yet")
                return
            
            # Try to claim epoch 20 (older than last claimed)
            epoch_data = epochs[20]
            user_data = epoch_data['users'][0]
            
            try:
                txid = build_claim_txn(
                    self.client,
                    self.app_id,
                    self.pool_id,
                    user.address,
                    user.private_key,
                    epoch_id=20,
                    cumulative_amount=user_data['cumulative_amount'],
                    proof=user_data['proof'],
                    fee_address=self.fee_address,
                    is_first_claim=False
                )
                self._test_failed(test_name, "Backwards epoch claim was accepted")
            except Exception:
                self._test_passed(test_name, "Backwards epoch correctly rejected")
                
        except Exception as e:
            self._test_failed(test_name, f"Test failed: {str(e)}")
    
    def test_claim_while_paused(self):
        """Test that claims are rejected when contract is paused"""
        test_name = "Claim While Paused - Rejected"
        self._test_skipped(test_name, "Requires admin pause action (test in integrated suite)")
    
    def _ensure_user_opted_in(self, user: TestAccount):
        """Ensure user is opted in to the contract"""
        account_info = self.client.account_info(user.address)
        is_opted_in = any(
            app['id'] == self.app_id
            for app in account_info.get('apps-local-state', [])
        )
        
        if not is_opted_in:
            params = self.client.suggested_params()
            txn = ApplicationCallTxn(
                sender=user.address,
                sp=params,
                index=self.app_id,
                on_complete=OnComplete.OptInOC
            )
            signed_txn = txn.sign(user.private_key)
            txid = self.client.send_transaction(signed_txn)
            wait_for_confirmation(self.client, txid, 4)
    
    def _get_user_asset_balance(self, address: str) -> int:
        """Get user's reward token balance"""
        account_info = self.client.account_info(address)
        for asset in account_info.get('assets', []):
            if asset['asset-id'] == self.reward_asset_id:
                return asset['amount']
        return 0
    
    def _test_passed(self, test_name: str, message: str = ""):
        """Record test pass"""
        self.results['passed'] += 1
        self.results['tests'].append({
            'name': test_name,
            'status': 'PASSED',
            'message': message
        })
        print(f"✅ {test_name}")
        if message:
            print(f"   {message}")
    
    def _test_failed(self, test_name: str, error: str):
        """Record test failure"""
        self.results['failed'] += 1
        self.results['tests'].append({
            'name': test_name,
            'status': 'FAILED',
            'error': error
        })
        print(f"❌ {test_name}")
        print(f"   Error: {error}")
    
    def _test_skipped(self, test_name: str, reason: str):
        """Record test skip"""
        self.results['tests'].append({
            'name': test_name,
            'status': 'SKIPPED',
            'reason': reason
        })
        print(f"⏭️  {test_name}")
        print(f"   Reason: {reason}")
    
    def _print_results(self):
        """Print test results summary"""
        print(f"\n{'='*70}")
        print("TEST RESULTS SUMMARY")
        print(f"{'='*70}")
        print(f"Total Tests: {len(self.results['tests'])}")
        print(f"Passed: {self.results['passed']}")
        print(f"Failed: {self.results['failed']}")
        print(f"Skipped: {len([t for t in self.results['tests'] if t['status'] == 'SKIPPED'])}")
        print(f"{'='*70}\n")


if __name__ == "__main__":
    print("User Claim Operations Test Suite")
    print("Run this from test_runner.py with proper setup")