# Changelog

## 2026-11-09

- Reorg records list the wallets whose events were removed (`wallets_affected`), in
  `/vault/reorgs` and the assistant's reorg tool, so "did that reorg touch my balance?" has an
  answer. Found by asking the assistant exactly that after a real fork on a local chain.
- `make assistant-eval`: checks the assistant's answers with a real model against the live
  indexed data. Opus 5.5 and Sonnet 5.5 both pass 8 of 8.
- Security policy.

## MVP

Staking vault and token, reorg-safe indexer with RPC failover, reconciliation, transaction
queue, Sign-In with Ethereum, metrics and alerts, and the read-only assistant.
