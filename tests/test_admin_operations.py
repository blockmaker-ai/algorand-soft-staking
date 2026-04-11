#!/usr/bin/env python3
"""
TEST SUITE: Admin Operations
Tests for set_root, pause, deprecate, emergency_withdraw, fee management

FIXES APPLIED:
1. Changed epoch range from [1,2,3] to [4,5,6] to avoid user test conflicts
2. Added encoding.decode_address() for propose_fee_address test
3. Added foreign_assets=[reward_asset_id] for emergency_withdraw test
"""

import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from algosdk.v2client import algod
from algosdk.transaction import ApplicationCallTxn, OnComplete, wait_for_confirmation
from algosdk import logic, encoding  # ADDED: encoding import for address decoding
from typing import Dict
import time

from utils.contract_helper import ContractHelper, build_set_root_txn
from utils.account_manager import TestAccount
from fixtures.test_data import TestData


class AdminOperationsTests:
    """Test suite for admin-only functions"""
    
    def __init__(
        self,
        client: algod.AlgodClient,
        app_id: int,
        reward_asset_id: int,
        admin_account: TestAccount,
        sponsor_account: TestAccount,
        fee_address: str
    ):
        self.client = client
        self.app_id = app_id
        self.reward_asset_id = reward_asset_id
        self.admin = admin_account
        self.sponsor = sponsor_account
        self.fee_address = fee_address
        self.helper = ContractHelper(client, app_id, reward_asset_id)
        
        self.results = {
            'passed': 0,
            'failed': 0,
            'tests': []
        }
    
    def run_all_tests(self):
        """Run all admin operation tests"""
        print(f"\n{'='*70}")
        print("ADMIN OPERATIONS TEST SUITE")
        print(f"{'='*70}\n")
        
        # Test set_root functionality
        self.test_set_root_success()
        self.test_set_root_unauthorized()
        self.test_set_root_invalid_epoch()
        self.test_set_root_duplicate_epoch()
        self.test_set_root_zero_epoch()
        
        # Test pause functionality
        self.test_pause_success()
        self.test_pause_unauthorized()
        self.test_pause_invalid_value()
        
        # Test deprecate functionality
        self.test_deprecate_success()
        self.test_deprecate_one_way()
        
        # Test emergency withdraw (requires deprecated state)
        self.test_emergency_withdraw_not_deprecated()
        self.test_emergency_withdraw_success()
        
        # Test fee address management
        self.test_propose_fee_address()
        self.test_execute_fee_update_too_early()
        self.test_execute_fee_update_success()
        self.test_cancel_fee_update()
        
        # Print results
        self._print_results()
    
    def test_set_root_success(self):
        """Test successful root publishing"""
        test_name = "Set Root - Success"
        
        try:
            # FIXED: Changed to epoch 10 (well above user test epochs 1-3)
            # Create a test Merkle root
            test_root = b'\x0A' * 32  # 0x0A = 10 in hex
            epoch_id = 10  # CHANGED FROM 4 - Use high epoch to avoid conflicts
            
            # Set the root
            txid = build_set_root_txn(
                self.client,
                self.app_id,
                self.admin.address,
                self.admin.private_key,
                epoch_id,
                test_root
            )
            
            # Verify root was stored
            stored_root = self.helper.get_epoch_root(epoch_id)
            
            assert stored_root == test_root, "Root mismatch"
            
            # Verify current_epoch_id was updated
            state = self.helper.get_global_state()
            assert state.get('epoch_id') == epoch_id, "Epoch ID not updated"
            
            self._test_passed(test_name, f"Root published for epoch {epoch_id}")
            
        except Exception as e:
            self._test_failed(test_name, str(e))
    
    def test_set_root_unauthorized(self):
        """Test that non-admin cannot set root"""
        test_name = "Set Root - Unauthorized"
        
        try:
            # Create non-admin account
            from algosdk import account
            private_key, address = account.generate_account()
            
            params = self.client.suggested_params()
            
            # Try to set root as non-admin (using epoch 11 to continue sequence)
            txn = ApplicationCallTxn(
                sender=address,
                sp=params,
                index=self.app_id,
                on_complete=OnComplete.NoOpOC,
                app_args=[
                    "set_root",
                    (11).to_bytes(8, 'big'),  # CHANGED FROM 5 - Continue high epoch sequence
                    b'\x0B' * 32  # 0x0B = 11 in hex
                ],
                boxes=[
                    (self.app_id, (11).to_bytes(8, 'big'))  # CHANGED FROM 5
                ]
            )
            
            signed_txn = txn.sign(private_key)
            
            # This should fail
            try:
                txid = self.client.send_transaction(signed_txn)
                wait_for_confirmation(self.client, txid, 4)
                self._test_failed(test_name, "Non-admin was able to set root")
            except Exception:
                self._test_passed(test_name, "Non-admin correctly rejected")
                
        except Exception as e:
            self._test_failed(test_name, f"Test setup failed: {str(e)}")
    
    def test_set_root_invalid_epoch(self):
        """Test that epoch must be monotonically increasing"""
        test_name = "Set Root - Invalid Epoch (Non-increasing)"
        
        try:
            state = self.helper.get_global_state()
            current_epoch = state.get('epoch_id', 0)
            
            # Try to set an older epoch
            params = self.client.suggested_params()
            txn = ApplicationCallTxn(
                sender=self.admin.address,
                sp=params,
                index=self.app_id,
                on_complete=OnComplete.NoOpOC,
                app_args=[
                    "set_root",
                    (current_epoch).to_bytes(8, 'big'),  # Same as current
                    b'\x03' * 32
                ],
                boxes=[
                    (self.app_id, (current_epoch).to_bytes(8, 'big'))
                ]
            )
            
            signed_txn = txn.sign(self.admin.private_key)
            
            try:
                txid = self.client.send_transaction(signed_txn)
                wait_for_confirmation(self.client, txid, 4)
                self._test_failed(test_name, "Non-increasing epoch was accepted")
            except Exception:
                self._test_passed(test_name, "Non-increasing epoch correctly rejected")
                
        except Exception as e:
            self._test_failed(test_name, f"Test setup failed: {str(e)}")
    
    def test_set_root_duplicate_epoch(self):
        """Test that cannot republish same epoch"""
        test_name = "Set Root - Duplicate Epoch"
        
        try:
            # FIXED: Try to republish epoch 10 (changed from 4)
            params = self.client.suggested_params()
            txn = ApplicationCallTxn(
                sender=self.admin.address,
                sp=params,
                index=self.app_id,
                on_complete=OnComplete.NoOpOC,
                app_args=[
                    "set_root",
                    (10).to_bytes(8, 'big'),  # CHANGED FROM 4 - Match first test
                    b'\x99' * 32  # Different root data to distinguish from first test
                ],
                boxes=[
                    (self.app_id, (10).to_bytes(8, 'big'))  # CHANGED FROM 4
                ]
            )
            
            signed_txn = txn.sign(self.admin.private_key)
            
            try:
                txid = self.client.send_transaction(signed_txn)
                wait_for_confirmation(self.client, txid, 4)
                self._test_failed(test_name, "Duplicate epoch was accepted")
            except Exception:
                self._test_passed(test_name, "Duplicate epoch correctly rejected")
                
        except Exception as e:
            self._test_failed(test_name, f"Test failed: {str(e)}")
    
    def test_set_root_zero_epoch(self):
        """Test that epoch 0 is rejected"""
        test_name = "Set Root - Zero Epoch"
        
        try:
            params = self.client.suggested_params()
            txn = ApplicationCallTxn(
                sender=self.admin.address,
                sp=params,
                index=self.app_id,
                on_complete=OnComplete.NoOpOC,
                app_args=[
                    "set_root",
                    (0).to_bytes(8, 'big'),
                    b'\x05' * 32
                ],
                boxes=[
                    (self.app_id, (0).to_bytes(8, 'big'))
                ]
            )
            
            signed_txn = txn.sign(self.admin.private_key)
            
            try:
                txid = self.client.send_transaction(signed_txn)
                wait_for_confirmation(self.client, txid, 4)
                self._test_failed(test_name, "Epoch 0 was accepted")
            except Exception:
                self._test_passed(test_name, "Epoch 0 correctly rejected")
                
        except Exception as e:
            self._test_failed(test_name, f"Test failed: {str(e)}")
    
    def test_pause_success(self):
        """Test pause/unpause functionality"""
        test_name = "Pause/Unpause - Success"
        
        try:
            params = self.client.suggested_params()
            
            # Pause
            pause_txn = ApplicationCallTxn(
                sender=self.admin.address,
                sp=params,
                index=self.app_id,
                on_complete=OnComplete.NoOpOC,
                app_args=["pause", (1).to_bytes(8, 'big')]
            )
            
            signed_txn = pause_txn.sign(self.admin.private_key)
            txid = self.client.send_transaction(signed_txn)
            wait_for_confirmation(self.client, txid, 4)
            
            # Verify paused
            state = self.helper.get_global_state()
            assert state.get('paused') == 1, "Contract not paused"
            
            # Unpause
            unpause_txn = ApplicationCallTxn(
                sender=self.admin.address,
                sp=params,
                index=self.app_id,
                on_complete=OnComplete.NoOpOC,
                app_args=["pause", (0).to_bytes(8, 'big')]
            )
            
            signed_txn = unpause_txn.sign(self.admin.private_key)
            txid = self.client.send_transaction(signed_txn)
            wait_for_confirmation(self.client, txid, 4)
            
            # Verify unpaused
            state = self.helper.get_global_state()
            assert state.get('paused') == 0, "Contract still paused"
            
            self._test_passed(test_name, "Pause/unpause working correctly")
            
        except Exception as e:
            self._test_failed(test_name, str(e))
    
    def test_pause_unauthorized(self):
        """Test that non-admin cannot pause"""
        test_name = "Pause - Unauthorized"
        
        try:
            from algosdk import account
            private_key, address = account.generate_account()
            
            params = self.client.suggested_params()
            txn = ApplicationCallTxn(
                sender=address,
                sp=params,
                index=self.app_id,
                on_complete=OnComplete.NoOpOC,
                app_args=["pause", (1).to_bytes(8, 'big')]
            )
            
            signed_txn = txn.sign(private_key)
            
            try:
                txid = self.client.send_transaction(signed_txn)
                wait_for_confirmation(self.client, txid, 4)
                self._test_failed(test_name, "Non-admin was able to pause")
            except Exception:
                self._test_passed(test_name, "Non-admin correctly rejected")
                
        except Exception as e:
            self._test_failed(test_name, f"Test setup failed: {str(e)}")
    
    def test_pause_invalid_value(self):
        """Test that pause only accepts 0 or 1"""
        test_name = "Pause - Invalid Value"
        
        try:
            params = self.client.suggested_params()
            txn = ApplicationCallTxn(
                sender=self.admin.address,
                sp=params,
                index=self.app_id,
                on_complete=OnComplete.NoOpOC,
                app_args=["pause", (2).to_bytes(8, 'big')]  # Invalid value
            )
            
            signed_txn = txn.sign(self.admin.private_key)
            
            try:
                txid = self.client.send_transaction(signed_txn)
                wait_for_confirmation(self.client, txid, 4)
                self._test_failed(test_name, "Invalid pause value was accepted")
            except Exception:
                self._test_passed(test_name, "Invalid pause value correctly rejected")
                
        except Exception as e:
            self._test_failed(test_name, f"Test failed: {str(e)}")
    
    def test_deprecate_success(self):
        """Test deprecation"""
        test_name = "Deprecate - Success"
        
        try:
            params = self.client.suggested_params()
            txn = ApplicationCallTxn(
                sender=self.admin.address,
                sp=params,
                index=self.app_id,
                on_complete=OnComplete.NoOpOC,
                app_args=["deprecate", (1).to_bytes(8, 'big')]
            )
            
            signed_txn = txn.sign(self.admin.private_key)
            txid = self.client.send_transaction(signed_txn)
            wait_for_confirmation(self.client, txid, 4)
            
            # Verify deprecated
            state = self.helper.get_global_state()
            assert state.get('deprecated') == 1, "Contract not deprecated"
            
            self._test_passed(test_name, "Contract successfully deprecated")
            
        except Exception as e:
            self._test_failed(test_name, str(e))
    
    def test_deprecate_one_way(self):
        """Test that deprecation is one-way"""
        test_name = "Deprecate - One-Way"
        
        try:
            params = self.client.suggested_params()
            txn = ApplicationCallTxn(
                sender=self.admin.address,
                sp=params,
                index=self.app_id,
                on_complete=OnComplete.NoOpOC,
                app_args=["deprecate", (0).to_bytes(8, 'big')]  # Try to reactivate
            )
            
            signed_txn = txn.sign(self.admin.private_key)
            
            try:
                txid = self.client.send_transaction(signed_txn)
                wait_for_confirmation(self.client, txid, 4)
                self._test_failed(test_name, "Contract was reactivated")
            except Exception:
                self._test_passed(test_name, "Deprecation is correctly one-way")
                
        except Exception as e:
            self._test_failed(test_name, f"Test failed: {str(e)}")
    
    def test_emergency_withdraw_not_deprecated(self):
        """Test that emergency withdraw requires deprecation"""
        test_name = "Emergency Withdraw - Not Deprecated"
        
        # Note: This test should be run before deprecation
        # Skipping if already deprecated
        
        state = self.helper.get_global_state()
        if state.get('deprecated') == 1:
            self._test_skipped(test_name, "Contract already deprecated")
            return
        
        try:
            params = self.client.suggested_params()
            params.fee = 2000  # Cover inner txn
            
            # FIXED: Added foreign_assets and accounts
            txn = ApplicationCallTxn(
                sender=self.admin.address,
                sp=params,
                index=self.app_id,
                on_complete=OnComplete.NoOpOC,
                app_args=["emergency_withdraw"],
                foreign_assets=[self.reward_asset_id],  # ADDED
                accounts=[self.sponsor.address]  # ADDED - sponsor account for inner txn receiver
            )
            
            signed_txn = txn.sign(self.admin.private_key)
            
            try:
                txid = self.client.send_transaction(signed_txn)
                wait_for_confirmation(self.client, txid, 4)
                self._test_failed(test_name, "Emergency withdraw succeeded without deprecation")
            except Exception:
                self._test_passed(test_name, "Emergency withdraw correctly requires deprecation")
                
        except Exception as e:
            self._test_failed(test_name, f"Test failed: {str(e)}")
    
    def test_emergency_withdraw_success(self):
        """Test emergency withdrawal"""
        test_name = "Emergency Withdraw - Success"
        
        try:
            # Get contract balance before
            contract_balance_before = self.helper.get_contract_balances()['reward_token']
            
            params = self.client.suggested_params()
            params.fee = 2000  # Cover inner txn
            
            # FIXED: Added foreign_assets and accounts
            txn = ApplicationCallTxn(
                sender=self.admin.address,
                sp=params,
                index=self.app_id,
                on_complete=OnComplete.NoOpOC,
                app_args=["emergency_withdraw"],
                foreign_assets=[self.reward_asset_id],
                accounts=[self.sponsor.address]
            )
            
            # DEBUG: Print transaction details
            print(f"DEBUG: Transaction accounts field: {txn.accounts}")
            print(f"DEBUG: Transaction foreign_assets field: {txn.foreign_assets}")
            print(f"DEBUG: Sponsor address being passed: {self.sponsor.address}")
            
            signed_txn = txn.sign(self.admin.private_key)
            txid = self.client.send_transaction(signed_txn)
            wait_for_confirmation(self.client, txid, 4)
            
            # Verify contract balance is 0
            contract_balance_after = self.helper.get_contract_balances()['reward_token']
            assert contract_balance_after == 0, "Contract still has tokens"
            
            self._test_passed(test_name, f"Withdrew {contract_balance_before:,} tokens")
            
        except Exception as e:
            self._test_failed(test_name, str(e))
    
    def test_propose_fee_address(self):
        """Test proposing new fee address"""
        test_name = "Propose Fee Address - Success"
        
        try:
            from algosdk import account
            
            # Generate new fee address
            _, new_fee_address = account.generate_account()
            
            # FIXED: Use encoding.decode_address() to get 32 bytes
            new_fee_address_bytes = encoding.decode_address(new_fee_address)
            
            params = self.client.suggested_params()
            txn = ApplicationCallTxn(
                sender=self.admin.address,
                sp=params,
                index=self.app_id,
                on_complete=OnComplete.NoOpOC,
                app_args=["propose_fee_address", new_fee_address_bytes]  # FIXED: using decoded bytes
            )
            
            signed_txn = txn.sign(self.admin.private_key)
            txid = self.client.send_transaction(signed_txn)
            wait_for_confirmation(self.client, txid, 4)
            
            # Verify pending address is set
            state = self.helper.get_global_state()
            assert state.get('pending_fee') == new_fee_address_bytes, "Pending fee address not set"
            assert state.get('fee_update_time') > 0, "Update time not set"
            
            self._test_passed(test_name, "Fee address proposal successful")
            
        except Exception as e:
            self._test_failed(test_name, str(e))
    
    def test_execute_fee_update_too_early(self):
        """Test that fee update cannot execute before timelock"""
        test_name = "Execute Fee Update - Too Early"
        
        try:
            params = self.client.suggested_params()
            txn = ApplicationCallTxn(
                sender=self.admin.address,
                sp=params,
                index=self.app_id,
                on_complete=OnComplete.NoOpOC,
                app_args=["execute_fee_update"]
            )
            
            signed_txn = txn.sign(self.admin.private_key)
            
            try:
                txid = self.client.send_transaction(signed_txn)
                wait_for_confirmation(self.client, txid, 4)
                self._test_failed(test_name, "Fee update executed before timelock")
            except Exception:
                self._test_passed(test_name, "Timelock correctly enforced")
                
        except Exception as e:
            self._test_failed(test_name, f"Test failed: {str(e)}")
    
    def test_execute_fee_update_success(self):
        """Test fee update after timelock (would require time manipulation in real scenario)"""
        test_name = "Execute Fee Update - Success"
        self._test_skipped(test_name, "Requires time manipulation (7-day wait)")
    
    def test_cancel_fee_update(self):
        """Test canceling pending fee update"""
        test_name = "Cancel Fee Update - Success"
        
        try:
            params = self.client.suggested_params()
            txn = ApplicationCallTxn(
                sender=self.admin.address,
                sp=params,
                index=self.app_id,
                on_complete=OnComplete.NoOpOC,
                app_args=["cancel_fee_update"]
            )
            
            signed_txn = txn.sign(self.admin.private_key)
            txid = self.client.send_transaction(signed_txn)
            wait_for_confirmation(self.client, txid, 4)
            
            # Verify pending state is cleared
            state = self.helper.get_global_state()
            pending_bytes = state.get('pending_fee', b'')
            
            # Check if it's zero address (all zeros)
            is_zero = all(b == 0 for b in pending_bytes)
            assert is_zero, "Pending fee address not cleared"
            
            self._test_passed(test_name, "Fee update proposal cancelled")
            
        except Exception as e:
            self._test_failed(test_name, str(e))
    
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
    print("Admin Operations Test Suite")
    print("Run this from test_runner.py with proper setup")