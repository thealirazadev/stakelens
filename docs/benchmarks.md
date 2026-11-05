# Benchmarks

Measured with `make benchmark` on 2026-11-05: 12th Gen Intel(R) Core(TM) i5-1235U, 12 threads, Linux.

Everything ran on one laptop: a local chain, Postgres in Docker, and one API worker, with the load generator on the same machine. A public RPC adds network latency, so read these numbers as a comparison and a ceiling, not a production forecast.

## Catching up on history

About 5000 blocks holding 40 vault events.

| Mode | Blocks | Seconds | Blocks per second | RPC calls |
|---|---:|---:|---:|---:|
| Block by block | 5044 | 63.5 | 79 | 10091 |
| Log ranges | 5044 | 0.9 | 5494 | 177 |

RPC calls are what free provider plans limit, so that column matters most.

## API under load

200,000 events across 2,000 wallets, 16 concurrent clients, 10 seconds per endpoint.

| Endpoint | Requests per second | p50 ms | p95 ms | Errors |
|---|---:|---:|---:|---:|
| `/wallets/{wallet}/position` | 427 | 36 | 55 | 0 |
| `/vault/events?limit=25` | 412 | 37 | 58 | 0 |
| `/vault/stakers` | 1839 | 8 | 13 | 0 |
| `/vault/stats` | 1798 | 8 | 12 | 0 |
