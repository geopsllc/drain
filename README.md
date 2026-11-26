# Drain (ARK → any coin, personal version)

Drains an ARK wallet (or a fixed amount of it) into USDC or USDT on Polygon, Base, Ethereum, Arbitrum,
Optimism or BNB Chain. Swaps go through [Flashift](https://docs.flashift.app) and
[Trocador](https://trocador.app/docs) via the **ARK Exchange Worker** at `https://xc.geops.net`, the same
service the ARK Wallet's Exchange tab uses. The Worker holds the aggregators' API keys, so **drain needs no API
keys**. It only quotes and creates orders: it never sees your passphrase and never moves funds. drain checks
every order itself, signs the ARK transfer locally, and links to the exchange's own order page.

Prefer calling Flashift and Trocador directly with your own API keys? That version is on the
[`ark-static`](https://github.com/geopsllc/drain/tree/ark-static) branch.

Based on [drain](https://github.com/galperins4/drain) by [galperins4](https://github.com/galperins4)
(`ark` branch), who wrote the original tool; see Credits below.

## Setup (Ubuntu)
Needs Python 3.10 or newer. Tested on clean Ubuntu 22.04, 24.04, 26.04 and 26.10 (x86-64 and arm64)
and Debian 13.

```
sudo apt install python3 python3-venv
cd drain
python3 -m venv .venv
.venv/bin/pip install --no-deps -r requirements.txt
cp config.example.ini config.ini && chmod 600 config.ini
.venv/bin/python -m unittest     # optional: offline self-test
```
Keep `--no-deps`: ARK's crypto library lists `coincurve` as a dependency, but coincurve can't be
installed on Python 3.14+ (Ubuntu 26.04). There drain uses a pure-Python stand-in
(`modules/coincurve_compat.py`) that produces byte-identical keys and signatures. Likewise, Ubuntu
22.04's OpenSSL lacks the RIPEMD-160 hash ARK addresses need, so drain falls back to
`modules/ripemd160.py`. The tests check both against fixed vectors.

Fill in `config.ini` (git-ignored). The phrases can come from the environment instead: `DRAIN_PASSPHRASE`,
`DRAIN_SECONDPHRASE`; `DRAIN_WORKER` overrides the Worker URL.

### Coins and networks
Set `convert_to` and `network`; `address_to` is your 0x address there.

| `network`  | `convert_to`  |
| ---------- | ------------- |
| `polygon`  | `usdc`, `usdt` |
| `base`     | `usdc` (neither aggregator offers ARK → USDT on Base) |
| `ethereum` | `usdc`, `usdt` |
| `arbitrum` | `usdc`, `usdt` |
| `optimism` | `usdc`, `usdt` |
| `bsc`      | `usdc`, `usdt` (BNB Chain) |

## Running
```
.venv/bin/python drain.py            # uses config.ini next to drain.py
.venv/bin/python drain.py --test     # checks and quotes only, whatever config.ini says
.venv/bin/python drain.py --yes      # don't ask before sending
.venv/bin/python drain.py --config other.ini
```
- `test = Y` (the default) or `--test` runs every check below and fetches quotes, but creates no order and sends nothing. It quotes `fixed_amt` when `fixed = Y`, otherwise `test_amt`, never the wallet balance. It still logs what a live run would send, and warns if a live run would find nothing to drain or skip below `min_amt`.
- From a terminal, a live run shows the order and asks before sending. From cron (no terminal) it doesn't ask.
- Exit code 0 = done, 1 = stopped before sending (reason in the log), 2 = unexpected error.

## How a run works
1. Reads the wallet from `node` and checks, before any order exists:
   - the passphrase belongs to `convert_address`, and the second passphrase matches if the wallet has one (multisignature wallets are refused);
   - the wallet has no transactions still waiting in the pool;
   - there is something left to drain after the fee and `keep`.
2. Amount = balance − fee − `keep` (or `fixed_amt` with `fixed = Y`, capped at what's available), truncated to
   4 decimals. The order and the transfer use exactly this amount. With `min = Y`, a run whose amount is below
   `min_amt` stops here and exits 0 (test mode warns and still quotes). E.g. `fixed_amt = 1000`,
   `min_amt = 500`: 1500 ARK available → send 1000; 700 → send 700; 300 → skip.
3. Asks the Worker for quotes (floating rate) and keeps those from the services in `providers` at or above
   `min_rate`. The Worker ranks them: each service's pick (Flashift: the quote it tags "Recommended", then
   "AI Best Rate", then "Best Rate"; Trocador: its best payout), best payout first, then the rest by payout.
4. Has the Worker create an order for the first quote and falls back to the next if that fails, up to 3. Order
   creation is never retried, so a timeout can't produce two orders for one quote.
5. Refuses the order unless the deposit address is a checksum-valid ARK address that isn't yours, no memo is
   required, the payout goes to `address_to` in `convert_to` on the quoted network, the order is for exactly
   our amount, and its expected payout is within 3% of the quote.
6. Signs and broadcasts the transfer and checks that the node accepted it.

Every order is logged to `exchange_log` (default `exchange.txt`):
`time,status,service,exchange,order id,amount,deposit address,expected payout,transaction id,tracking`.
Status is `created`, then `sent`, `declined` or `broadcast-failed`. Orders that were never funded simply expire.

Each created order also prints a link to follow it on the exchange itself, using the order's id there:
Changelly `https://changelly.com/track/<id>`, ChangeNOW `https://changenow.io/exchange/txs/<id>`,
SimpleSwap `https://simpleswap.io/exchange?id=<id>`, StealthEX `https://stealthex.io/exchange/?id=<id>`.
For other exchanges it falls back to the aggregator: Trocador's `https://trocador.app/checkout/<order id>`, or
for Flashift (no per-order page) `https://exchange.flashift.app` → History → Track Your Swap with the order id.

Only one run can happen at a time (`.drain.lock`).

The Worker allows 10 requests a minute per IP: a run uses one for the quote and one per order attempt. If it
is busy, drain waits out a short `Retry-After` once for the quote and gives up on orders.

Flashift has no refund-address field, so refunds from a Flashift order go through the provider's support. Trocador passes `convert_address` as the refund address.

## Security notes
- `config.ini` holds your passphrase: keep it `chmod 600`. drain refuses a live run if a config file
  containing a passphrase is readable by other users. Or leave `passphrase` empty and set `DRAIN_PASSPHRASE`.
- Set `min_rate` to a floor you'd accept (USDC per ARK). Without it drain takes the best quote it gets,
  however bad; the only protection is that the order must stay within 3% of that quote.
- The default node is the public `https://api.ark.io/api`; your own node (`http://localhost:4003/api`) or any https URL works too. drain checks the node is on ARK
  mainnet (nethash) and warns about plain http to a non-local host. A node can't redirect funds (drain
  chooses the recipient), but it can misreport balance or nonce.
- The deposit address comes from the exchange through the Worker; drain can only check that it is a valid ARK
  address that isn't yours, needs no memo, and that the order pays `address_to` the right coin on the right
  network for exactly the amount sent. That's why the Worker URL must be https (plain http is only allowed
  for a local test Worker), and why the order page link goes straight to the exchange.
- Dependencies are installed from PyPI with exact pins for small packages and security-patched minimums
  for the HTTP/TLS stack; run `pip-audit` occasionally.

## Tests
```
.venv/bin/python -m unittest -v
```
Runs offline against a fake ARK node and a faked Worker.

## Configuration
See `config.example.ini` for every option with comments.

## Credits
drain was created by **[galperins4](https://github.com/galperins4)**: https://github.com/galperins4/drain.
This repository keeps the original commit history. The personal version replaces the original's
Lambda-based exchange calls with the ARK Exchange Worker (or, on the `ark-static` branch, direct Flashift/Trocador
clients), and adds the safety checks, tests and Ubuntu install support described above.

