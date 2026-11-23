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
Use another config file with `DRAIN_CONFIG=/path/to/config.ini`.

## Running
```
.venv/bin/python drain.py
```
- `test = Y` only fetches quotes from each provider and prints which one would be used. No order is created and nothing is sent.
- `test = N` creates one order and sends the ARK to its deposit address.

## How a run works
1. Reads the wallet's balance and nonce from `node` and works out the fee.
2. Amount = whole balance minus fee (or `fixed_amt` with `fixed = Y`), truncated to 4 decimals. The order and the transfer use exactly this amount.
3. Asks every provider in `providers` that has an API key for its best quote:
   - **Flashift**: among quotes of the configured `rate` whose min/max covers the amount, the one tagged "Recommended", then "AI Best Rate", then "Best Rate", then the highest payout.
   - **Trocador**: the highest payout of the configured `rate` from its `new_rate` list (optionally only exchanges with `min_kycrating` or better).
4. Tries the best payout first and falls back to the next if creating the order fails.
5. Before sending, checks that the deposit address is a valid ARK address that isn't your own, that no memo is required, and that the order pays out to `address_to`. Otherwise nothing is sent.
6. Sends the ARK and logs the order to `exchange_log` (time, service, exchange, order id, amount, deposit address, expected payout).

Network names differ per service: USDC on Polygon is `usdc` / `pol` on Flashift and `usdc` / `Polygon` on Trocador; ARK is `ark` / `ark` and `ark` / `Mainnet`.

Flashift has no refund-address field, so refunds from a Flashift order go through the provider's support. Trocador passes `convert_address` as the refund address.

## Configuration
See `config.example.ini` for every option with comments.
