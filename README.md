# Drain (ARK → any coin, personal version)

Drains an ARK wallet (or a fixed amount of it) into another coin, by default USDC on Polygon.
Swaps go through [Flashift](https://docs.flashift.app) and/or [Trocador](https://trocador.app/docs),
called directly with your own API keys. No Lambda or other intermediary is involved.

## Setup
```
cd drain
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.example.ini config.ini && chmod 600 config.ini
```
Fill in `config.ini` (git-ignored). API keys and phrases can come from the environment instead:
`FLASHIFT_API_KEY`, `TROCADOR_API_KEY`, `DRAIN_PASSPHRASE`, `DRAIN_SECONDPHRASE`.

## Running
```
.venv/bin/python drain.py            # uses config.ini next to drain.py
.venv/bin/python drain.py --test     # checks and quotes only, whatever config.ini says
.venv/bin/python drain.py --yes      # don't ask before sending
.venv/bin/python drain.py --config other.ini
```
- `test = Y` (the default) or `--test` runs every check below and fetches quotes for the real amount, but creates no order and sends nothing. If the node can't be reached it quotes `test_amt` instead.
- From a terminal, a live run shows the order and asks before sending. From cron (no terminal) it doesn't ask.
- Exit code 0 = done, 1 = stopped before sending (reason in the log), 2 = unexpected error.

## How a run works
1. Reads the wallet from `node` and checks, before any order exists:
   - the passphrase belongs to `convert_address`, and the second passphrase matches if the wallet has one (multisignature wallets are refused);
   - the wallet has no transactions still waiting in the pool;
   - there is something left to drain after the fee and `keep`.
2. Amount = balance − fee − `keep` (or `fixed_amt` with `fixed = Y`), truncated to 4 decimals. The order and the transfer use exactly this amount.
3. Asks every provider in `providers` that has an API key for its best quote, and ignores quotes below `min_rate`:
   - **Flashift**: among quotes of the configured `rate` whose min/max covers the amount, the one tagged "Recommended", then "AI Best Rate", then "Best Rate", then the highest payout.
   - **Trocador**: the highest payout of the configured `rate` from its `new_rate` list (optionally only exchanges with `min_kycrating` or better).
4. Creates an order with the best payout and falls back to the next if that fails. Order creation is never retried, so a timeout can't produce two orders with the same service.
5. Refuses the order unless the deposit address is a checksum-valid ARK address that isn't yours, no memo is required, the payout goes to `address_to` in `convert_to`, the order is for exactly our amount, and its expected payout is within 3% of the quote.
6. Signs and broadcasts the transfer and checks that the node accepted it.

Every order is logged to `exchange_log` (default `exchange.txt`):
`time,status,service,exchange,order id,amount,deposit address,expected payout,transaction id`.
Status is `created`, then `sent`, `declined` or `broadcast-failed`. Orders that were never funded simply expire.

Only one run can happen at a time (`.drain.lock`).

Network names differ per service: USDC on Polygon is `usdc` / `pol` on Flashift and `usdc` / `MATIC` on Trocador; USDC on Base is `base` on both; ARK is `ark` / `ark` and `ark` / `Mainnet`. To change the target, set `convert_to`, `address_to` and each service's `network_to`.

Flashift has no refund-address field, so refunds from a Flashift order go through the provider's support. Trocador passes `convert_address` as the refund address.

## Tests
```
.venv/bin/python -m unittest -v
```
Runs offline against a fake ARK node and faked Flashift/Trocador responses.

## Configuration
See `config.example.ini` for every option with comments.
