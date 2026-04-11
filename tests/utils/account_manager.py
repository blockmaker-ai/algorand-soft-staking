#!/usr/bin/env python3
"""
Account Management Utilities for Testing
Handles test account creation, funding, and management.

Test user mnemonics are loaded from environment variables so no credentials
are ever stored in source code. Set these in your .env file (see .env.example):

    TEST_USER1_MNEMONIC=word1 word2 ... word25
    TEST_USER2_MNEMONIC=word1 word2 ... word25
    TEST_USER3_MNEMONIC=word1 word2 ... word25
    TEST_USER4_MNEMONIC=word1 word2 ... word25  # optional, defaults to TestUser1

Generate fresh testnet accounts with:
    python -c "from algosdk import account, mnemonic; pk, addr = account.generate_account(); print(addr); print(mnemonic.from_private_key(pk))"
Then fund them at https://bank.testnet.algorand.network/
"""

import os
from algosdk import account, mnemonic
from algosdk.v2client import algod
from algosdk.transaction import PaymentTxn, wait_for_confirmation
from typing import Dict, List, Tuple
import base64

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), '..', '..', '.env'))
except ImportError:
    pass


def _load_test_mnemonics() -> Dict[str, str]:
    """Load test mnemonics from environment variables. Fails clearly if any are missing."""
    mnemonics = {}
    for i in range(1, 4):
        key = f"TEST_USER{i}_MNEMONIC"
        val = os.getenv(key)
        if not val:
            raise EnvironmentError(
                f"Missing {key} environment variable.\n"
                "Set TEST_USER1_MNEMONIC, TEST_USER2_MNEMONIC, TEST_USER3_MNEMONIC in your .env\n"
                "Generate accounts: python -c \"from algosdk import account, mnemonic; "
                "pk, addr = account.generate_account(); print(addr); print(mnemonic.from_private_key(pk))\"\n"
                "Fund at: https://bank.testnet.algorand.network/"
            )
        mnemonics[f"TestUser{i}"] = val

    # TestUser4 is optional — defaults to TestUser1 if not set
    mnemonics["TestUser4"] = os.getenv("TEST_USER4_MNEMONIC", mnemonics["TestUser1"])
    return mnemonics


class TestAccount:
    """Represents a test account with keys and metadata"""
    
    def __init__(self, private_key: str, address: str, name: str = ""):
        self.private_key = private_key
        self.address = address
        self.name = name or f"Account_{address[:8]}"
    
    @classmethod
    def from_mnemonic(cls, name: str, mnemonic_phrase: str) -> 'TestAccount':
        """
        Create TestAccount from mnemonic phrase
        
        ADDED: Convenience method for creating TestAccount from mnemonic
        
        Args:
            name: Account name
            mnemonic_phrase: 25-word mnemonic
            
        Returns:
            TestAccount instance
        """
        private_key = mnemonic.to_private_key(mnemonic_phrase)
        address = account.address_from_private_key(private_key)
        return cls(private_key, address, name)
        
    def __repr__(self):
        return f"TestAccount(name={self.name}, address={self.address})"
    
    def to_dict(self) -> Dict:
        """Convert to dictionary for serialization"""
        return {
            "name": self.name,
            "address": self.address,
            "private_key": self.private_key,
            "mnemonic": mnemonic.from_private_key(self.private_key)
        }


class AccountManager:
    """Manages test accounts for the testing suite"""
    
    def __init__(self, algod_client: algod.AlgodClient):
        self.client = algod_client
        self.accounts: Dict[str, TestAccount] = {}
        
    def create_account(self, name: str) -> TestAccount:
        """
        Create a test account.

        For TestUser1–4 the mnemonic is loaded from the TEST_USERn_MNEMONIC
        environment variable so the same address is produced on every run.
        Any other name generates a fresh random account.

        Args:
            name: Human-readable name for the account

        Returns:
            TestAccount instance
        """
        known = ["TestUser1", "TestUser2", "TestUser3", "TestUser4"]
        if name in known:
            test_mnemonics = _load_test_mnemonics()
            mn = test_mnemonics[name]
            private_key = mnemonic.to_private_key(mn)
            address = account.address_from_private_key(private_key)
        else:
            private_key, address = account.generate_account()

        test_account = TestAccount(private_key, address, name)
        self.accounts[name] = test_account
        return test_account
    
    def create_accounts(self, names: List[str]) -> Dict[str, TestAccount]:
        """
        Create multiple test accounts
        
        Args:
            names: List of account names
            
        Returns:
            Dictionary of name -> TestAccount
        """
        return {name: self.create_account(name) for name in names}
    
    def fund_account(
        self,
        from_account: TestAccount,
        to_address: str,
        amount: int,
        note: str = ""
    ) -> str:
        """
        Fund an account with ALGO
        
        Args:
            from_account: Source account
            to_address: Destination address
            amount: Amount in microAlgos
            note: Optional transaction note
            
        Returns:
            Transaction ID
        """
        params = self.client.suggested_params()
        
        txn = PaymentTxn(
            sender=from_account.address,
            receiver=to_address,
            amt=amount,
            sp=params,
            note=note.encode() if note else None
        )
        
        signed_txn = txn.sign(from_account.private_key)
        txid = self.client.send_transaction(signed_txn)
        
        wait_for_confirmation(self.client, txid, 4)
        
        return txid
    
    def fund_accounts(
        self,
        from_account: TestAccount,
        accounts: List[TestAccount],
        amount_each: int
    ) -> List[str]:
        """
        Fund multiple accounts
        
        Args:
            from_account: Source account (must have sufficient balance)
            accounts: List of accounts to fund
            amount_each: Amount to send to each (in microAlgos)
            
        Returns:
            List of transaction IDs
        """
        txids = []
        for acc in accounts:
            txid = self.fund_account(
                from_account,
                acc.address,
                amount_each,
                f"Funding test account: {acc.name}"
            )
            txids.append(txid)
        
        return txids
    
    def get_balance(self, address: str) -> int:
        """
        Get ALGO balance for an address
        
        Args:
            address: Account address
            
        Returns:
            Balance in microAlgos
        """
        account_info = self.client.account_info(address)
        return account_info.get('amount', 0)
    
    def get_asset_balance(self, address: str, asset_id: int) -> int:
        """
        Get asset balance for an address
        
        Args:
            address: Account address
            asset_id: Asset ID
            
        Returns:
            Asset balance
        """
        account_info = self.client.account_info(address)
        assets = account_info.get('assets', [])
        
        for asset in assets:
            if asset['asset-id'] == asset_id:
                return asset['amount']
        
        return 0
    
    def decode_address(self, address: str) -> bytes:
        """
        Convert Algorand address to 32-byte format for contract calls
        
        Args:
            address: Base32 Algorand address
            
        Returns:
            32-byte address
        """
        return base64.b32decode(address + "=" * ((8 - len(address) % 8) % 8))
    
    def print_balances(self, include_assets: bool = False, asset_id: int = None):
        """
        Print balances for all managed accounts
        
        Args:
            include_assets: Whether to include asset balances
            asset_id: Specific asset ID to check
        """
        print(f"\n{'='*70}")
        print("ACCOUNT BALANCES")
        print(f"{'='*70}")
        
        for name, acc in self.accounts.items():
            algo_balance = self.get_balance(acc.address)
            print(f"\n{name}:")
            print(f"  Address: {acc.address}")
            print(f"  ALGO: {algo_balance / 1_000_000:.6f} ({algo_balance:,} µA)")
            
            if include_assets and asset_id:
                asset_balance = self.get_asset_balance(acc.address, asset_id)
                print(f"  Asset {asset_id}: {asset_balance:,}")
        
        print(f"{'='*70}\n")
    
    def export_accounts(self, filepath: str):
        """
        Export account information to a file
        
        WARNING: Contains private keys - for testing only!
        
        Args:
            filepath: Path to save account data
        """
        import json
        
        data = {name: acc.to_dict() for name, acc in self.accounts.items()}
        
        with open(filepath, 'w') as f:
            json.dump(data, f, indent=2)
        
        print(f"⚠️  Exported {len(self.accounts)} accounts to {filepath}")
        print("WARNING: This file contains private keys! Delete after testing.")
    
    def get_account(self, name: str) -> TestAccount:
        """Get account by name"""
        if name not in self.accounts:
            raise ValueError(f"Account '{name}' not found")
        return self.accounts[name]


def create_funded_accounts(
    client: algod.AlgodClient,
    funder: TestAccount,
    account_names: List[str],
    algo_amount: int = 10_000_000  # 10 ALGO default
) -> Dict[str, TestAccount]:
    """
    Create and fund test accounts in one call
    
    Args:
        client: Algod client
        funder: Account with funds (e.g., sandbox default account)
        account_names: List of names for new accounts
        algo_amount: Amount of ALGO to give each account (in microAlgos)
        
    Returns:
        Dictionary of name -> TestAccount (all funded and ready)
    """
    manager = AccountManager(client)
    accounts = manager.create_accounts(account_names)
    
    print(f"Creating {len(accounts)} test accounts...")
    
    # Fund all accounts
    manager.fund_accounts(funder, list(accounts.values()), algo_amount)
    
    print(f"✅ All accounts created and funded with {algo_amount / 1_000_000} ALGO each")
    
    return accounts


if __name__ == "__main__":
    print("Account Management Utilities")
    print("=" * 70)
    print("\nTest accounts are loaded from environment variables:")
    print("  TEST_USER1_MNEMONIC, TEST_USER2_MNEMONIC, TEST_USER3_MNEMONIC")
    print("  TEST_USER4_MNEMONIC  (optional — falls back to TestUser1)")
    print("\nGenerate a new testnet account:")
    print("  python -c \"from algosdk import account, mnemonic; pk, addr = account.generate_account(); print(addr); print(mnemonic.from_private_key(pk))\"")
    print("\nFund at: https://bank.testnet.algorand.network/")
    print("\nLoading accounts from .env...")
    try:
        test_mnemonics = _load_test_mnemonics()
        for name, mn in test_mnemonics.items():
            pk = mnemonic.to_private_key(mn)
            addr = account.address_from_private_key(pk)
            print(f"  {name}: {addr}")
    except EnvironmentError as e:
        print(f"\n{e}")