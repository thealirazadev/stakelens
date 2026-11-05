# stakelens

A backend for on-chain staking data: a Solidity staking vault, an event indexer that survives chain reorganisations, wallet sign-in, and a read-only assistant that answers questions from the indexed data.

**Status: MVP.** It runs end to end on a local EVM chain. It has not been deployed to a public network, and the contracts have not been audited.

![Dashboard showing total stake, largest stakers, a handled reorg, and vault events with confirmation counts](docs/dashboard.png)

## How it fits together

```
StakingVault.sol  --events-->  indexer  --writes-->  Postgres  <--reads--  FastAPI  -->  dashboard
                                                                              |
                                                               assistant (Claude + read-only tools)
```

| Part | Where | What it does |
|---|---|---|
| Contracts | [contracts/src](contracts/src) | ERC-20 token and a staking vault with pro rata rewards. No owner or admin functions. |
| Indexer | [app/indexer.py](app/indexer.py) | Follows the chain block by block and rolls back on forks. |
| Read layer | [app/queries.py](app/queries.py) | Derives balances from events in SQL. Used by both the API and the assistant. |
| Sign-in | [app/auth.py](app/auth.py) | Sign-In with Ethereum (EIP-4361) with single-use nonces. |
| Assistant | [app/assistant.py](app/assistant.py) | Claude with five read-only tools. It holds no keys and cannot write anything. |

## Design decisions

**Reorgs are detected, not avoided.** Waiting for finality alone would hide data for minutes and still needs a recovery path. Each new block's parent hash is compared with the stored hash of the previous block. On a mismatch, the indexer walks back to the common ancestor, deletes everything above it, records the reorg, and carries on. A fork deeper than `MAX_REORG_DEPTH` stops the indexer for an operator instead of unwinding automatically.

**Pending and confirmed are separate numbers.** The API shows new events immediately and reports both `staked` and `confirmed_staked`, so a client can decide which one to trust.

**One transaction per block.** A block's events, its hash, and the cursor commit together, so a crash cannot skip a block or duplicate events. A Postgres advisory lock stops a second indexer from racing the first.

**Logs are fetched by block hash, not number.** The node then either answers for exactly that block or returns an error. It can't return logs from a competing fork.

**Amounts are integers.** Wei is stored as `NUMERIC(78,0)` and formatted with integer maths.

**The server writes the sign-in message.** It stores the exact text the wallet signs and recovers the signer from that copy, so no client-supplied message is ever parsed. A failed attempt still uses up the nonce.

**The assistant doesn't do arithmetic.** Every figure comes from a tool backed by the same queries as the API. The tool loop is capped, questions are limited per wallet per hour, and token usage is recorded for each call.

**Principal is ring-fenced from rewards.** Rewards are paid only from tokens above `totalStaked`, so `unstake` still works if the reward pool runs dry. An invariant test checks that staked principal stays fully backed.

## Run it

Needs Docker, [uv](https://docs.astral.sh/uv/), and Python 3.12.

```sh
git clone --recurse-submodules https://github.com/thealirazadev/stakelens
cd stakelens
cp .env.example .env     # add ANTHROPIC_API_KEY to use the assistant
uv sync
make up                  # Postgres and migrations
make chain               # local chain, in its own terminal
make deploy
make indexer             # in its own terminal
make api                 # in its own terminal, then open http://localhost:8010
```

Then generate activity:

```sh
make simulate    # stakes, unstakes, and a claim from three wallets
make reorg       # stakes 999 STK, then forks the chain so that stake never happened
make check       # compares every indexed balance with the contract
```

Signing in from the dashboard needs a browser wallet such as MetaMask, connected to the local chain (`http://127.0.0.1:8545`, chain ID 31337).

## Tests

```sh
make test-contracts   # unit, fuzz, and invariant tests
make chain-test       # instant-mining chain for end-to-end tests, in its own terminal
make build
make test             # backend tests
```

The backend tests use real Postgres. Reorg logic is covered twice: against a fake chain that can fork at exact points, and against a real EVM node with real forks. The end-to-end tests are skipped when no node is running.

The assistant's tests run the real Anthropic SDK against a local stub. They check the requests and the tool loop, not the quality of the answers.

## API

| Route | Auth | |
|---|---|---|
| `GET /health` | | Sync position |
| `GET /vault/stats` | | Totals, stakers, indexer lag |
| `GET /vault/events` | | Recent events with confirmation status |
| `GET /vault/stakers` | | Largest stakes |
| `GET /vault/reorgs` | | Reorgs handled |
| `GET /wallets/{address}/position` | | One wallet's balance |
| `GET /wallets/{address}/events` | | One wallet's events |
| `POST /auth/challenge` | | Get a message to sign |
| `POST /auth/verify` | | Exchange a signature for a session token |
| `GET /me/position` | Bearer | The signed-in wallet's balance |
| `POST /assistant/ask` | Bearer | Ask a question |

Interactive docs are at `/docs` while the API is running.

## Known limits

- Blocks are fetched one at a time, which is simple but slow when catching up over a long range. `eth_getLogs` over ranges would be the next step.
- All indexed blocks are kept. There is no pruning below the maximum reorg depth yet.
- Smart contract wallets (ERC-1271) can't sign in.
- It tracks one vault on one chain.
- `scripts/deploy.py` supports other networks through `RPC_URL`, `CHAIN_ID`, and `DEPLOYER_PRIVATE_KEY`, but has only been run against a local chain.

## License

MIT
