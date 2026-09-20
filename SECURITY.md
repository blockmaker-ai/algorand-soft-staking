# Security

The owner controls pause and publisher rotation. The funding operator registers deposits. The publisher assigns funded cumulative credits. These roles should use separately managed keys in a real deployment.

On-chain caps prevent unfunded allocations and repeated payment of the same cumulative credit. They do not independently attest to off-chain eligibility or prevent an authorised publisher from allocating available funding unfairly. Reward-ASAs with freeze or clawback authorities have additional issuer risk. Immutable credits and vaults also mean there is no administrative escape hatch for accidentally locked tokens.

The service-role credential can call accounting RPCs and must be treated as an operator credential. Do not grant it to a browser. Keep publisher mnemonics in the server secret store, restrict administrative access, and preserve private evidence and backups. Public clients have no direct stake or ledger write access.

Use complete, same-network Algod and Indexer providers. Missing history, lagged pages, inconsistent snapshots or modified evidence stop accounting. A pause does not erase credits. Resolve uncertain transaction outcomes from their saved identities before retrying.

The test suites cover contract and database accounting, wallet authorisation, changed transaction groups, holding-history reconstruction and interrupted publication. Tests are not an independent audit.

For a suspected vulnerability, use GitHub’s private vulnerability reporting feature when available. Do not include credentials, mnemonics or private wallet histories in a public issue. If private reporting is unavailable, contact the repository owner privately before sharing exploit details.
