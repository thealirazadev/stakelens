# Security

## Reporting a vulnerability

Report vulnerabilities privately through GitHub: **Security > Report a vulnerability** on this
repository. Do not open a public issue. You will get a reply within five working days.

The contracts have not been audited. Do not deploy them to a network that holds real value.

## Security model

- **Contracts have no admin.** The vault has no owner, pause, or upgrade function. Rewards are
  paid only from tokens above `totalStaked`, and an invariant test checks that staked principal
  stays fully backed.
- **Sign-in.** Sign-In with Ethereum (EIP-4361): the server writes the message, stores the exact
  text, recovers the signer from that copy, and burns the nonce on any attempt.
- **The assistant is read-only.** It holds no keys, its five tools only read the database through
  the same queries as the API, the tool loop is capped, and questions are rate limited per wallet.
- **Secrets.** RPC URLs often embed an API key, so endpoints are logged only by position (a test
  checks this). The transaction signing key lives only in the worker, behind a signer interface
  meant for a KMS.
- **Data integrity.** The indexer detects reorgs by parent hash and rolls back; a reconciliation
  job compares indexed balances with the contract and alerts on any mismatch.
