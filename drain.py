"""Drain an ARK wallet into another coin (e.g. USDC on Polygon) via Flashift and/or Trocador.

Config is read from --config, $DRAIN_CONFIG, or config.ini next to this script
(see config.example.ini). With test = Y (or --test) it runs every check and fetches
quotes, but creates no order and sends nothing.
"""
import argparse
import fcntl
import logging
import os
import sys
from configparser import ConfigParser, Error as ConfigError
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from client import ArkClient
from crypto.configuration.network import set_custom_network
from crypto.identity.address import address_from_passphrase
from crypto.identity.public_key import PublicKey
from crypto.transactions.builder.transfer import Transfer

from modules.exchange import ATOMIC, EVM_ADDRESS, EVM_NETWORKS, Exchange, fmt, is_ark_address, to_decimal

HERE = Path(__file__).resolve().parent
UNIT = 10_000              # amounts are truncated to 4 decimals (1 ARK = 100000000)
STATIC_FEE = 10_000_000    # ARK static transfer fee, used when the node's fee config can't be read
VENDOR_FIELD = 'drain'
log = logging.getLogger('drain')


class Abort(Exception):
    """Stop the run with a message; nothing was sent."""


def get_config(path=None):
    path = Path(path or os.environ.get('DRAIN_CONFIG') or HERE / 'config.ini')
    if not path.exists():
        raise Abort(f'Config not found: {path} (copy config.example.ini to config.ini)')
    parser = ConfigParser(inline_comment_prefixes=(';', '#'), interpolation=None)
    try:
        parser.read(path)
    except ConfigError as e:
        raise Abort(f'Cannot parse {path}: {e}')
    if not parser.has_section('static'):
        raise Abort(f'{path} has no [static] section')
    s = parser['static']

    def number(key, default):
        value = to_decimal(s.get(key) or default)
        if value is None or value < 0:
            raise Abort(f'{key} must be a non-negative number, got {s.get(key)!r}')
        return value

    config = {
        'node': (s.get('node') or 'http://localhost:4003/api').rstrip('/'),
        'test': (s.get('test') or 'Y').strip().upper() == 'Y',
        'passphrase': os.environ.get('DRAIN_PASSPHRASE') or s.get('passphrase') or '',
        'secondphrase': os.environ.get('DRAIN_SECONDPHRASE') or s.get('secondphrase') or '',
        'convert_from': (s.get('convert_from') or 'ark').strip().lower(),
        'convert_address': (s.get('convert_address') or '').strip(),
        'convert_to': (s.get('convert_to') or 'usdc').strip().lower(),
        'address_to': (s.get('address_to') or '').strip(),
        'providers': [p.strip() for p in (s.get('providers') or 'Flashift, Trocador').split(',') if p.strip()],
        'fixed': (s.get('fixed') or 'N').strip().upper() == 'Y',
        'fixed_amt': number('fixed_amt', '0'),
        'test_amt': number('test_amt', '750'),
        'min_rate': number('min_rate', '0'),
        'keep': number('keep', '0'),
        'exchange_log': str(HERE / (s.get('exchange_log') or 'exchange.txt')),
    }
    for section in ('flashift', 'trocador'):
        config[section] = dict(parser[section]) if parser.has_section(section) else {}
    config['passphrase'] = ' '.join(config['passphrase'].split())
    config['secondphrase'] = ' '.join(config['secondphrase'].split())
    if config['secondphrase'] == 'None':
        config['secondphrase'] = ''

    if config['convert_from'] != 'ark':
        raise Abort('Only ARK can be drained (convert_from = ark)')
    if not is_ark_address(config['convert_address']):
        raise Abort(f'convert_address is not a valid ARK address: {config["convert_address"]!r}')
    if not config['address_to']:
        raise Abort('address_to is empty')
    networks = {(config[s].get('network_to') or '').strip().lower() for s in ('flashift', 'trocador')}
    if networks & EVM_NETWORKS and not EVM_ADDRESS.match(config['address_to']):
        raise Abort(f'address_to must be a 0x address for {", ".join(sorted(networks & EVM_NETWORKS))}')
    if config['fixed'] and config['fixed_amt'] <= 0:
        raise Abort('fixed = Y needs fixed_amt > 0')
    return config


def get_client(node):
    set_custom_network(datetime(2017, 3, 21, 13, 0, 0), 23, 170)  # ARK mainnet epoch, version, wif
    return ArkClient(node)


def get_fee(client):
    """Dynamic fee from the node, conservatively rounded up; static fee if it can't be read."""
    try:
        dynamic = client.node.configuration()['data']['transactionPool']['dynamicFees']
        if not dynamic.get('enabled'):
            return STATIC_FEE
        # Generous size estimate for a v2 transfer with vendor field and one or two signatures.
        size = 230 + len(VENDOR_FIELD)
        fee = int((int(dynamic['addonBytes']['transfer']) + size) * int(dynamic['minFeePool']))
        return min(max(fee, 1), STATIC_FEE)
    except Exception as e:
        log.warning('Could not read dynamic fees (%s); using static fee', e)
        return STATIC_FEE


def get_wallet(client, address):
    try:
        wallet = client.wallets.get(address)['data']
    except Exception as e:
        raise Abort(f'Cannot read wallet {address} from node: {e}')
    return wallet


def pending_from(client, public_key, pages=5):
    """Our transactions still in the node's pool (the API can't filter unconfirmed by sender)."""
    found = []
    for page in range(1, pages + 1):
        body = client.transactions.all_unconfirmed(limit=100, page=page)
        data = body.get('data') or []
        found += [t for t in data if t.get('senderPublicKey') == public_key]
        if len(data) < 100:
            break
    return found


def check_keys(config, wallet):
    """The passphrases must sign for this wallet, or the transfer would be rejected after ordering."""
    if not config['passphrase']:
        raise Abort('passphrase is empty (set it in config.ini or DRAIN_PASSPHRASE)')
    if address_from_passphrase(config['passphrase'], network_version=23) != config['convert_address']:
        raise Abort('passphrase does not belong to convert_address')
    attributes = wallet.get('attributes') or {}
    if attributes.get('multiSignature'):
        raise Abort('multisignature wallets are not supported')
    second = attributes.get('secondPublicKey')
    if second and not config['secondphrase']:
        raise Abort('wallet has a second passphrase; set secondphrase')
    if second and PublicKey.from_passphrase(config['secondphrase']) != second:
        raise Abort('secondphrase does not match the wallet')
    if not second and config['secondphrase']:
        log.warning('secondphrase is set but the wallet has none; ignoring it')
        config['secondphrase'] = ''


def plan_amount(config, balance, fee):
    """Atomic amount to drain, truncated to 4 decimals, or 0 if there is nothing to do."""
    available = balance - fee - int(config['keep'] * ATOMIC)
    wanted = min(int(config['fixed_amt'] * ATOMIC), available) if config['fixed'] else available
    wanted -= wanted % UNIT
    return max(wanted, 0)


def build_transfer(config, recipient, amount, fee, nonce):
    tx = Transfer(recipientId=recipient, amount=amount, vendorField=VENDOR_FIELD, fee=fee)
    tx.set_nonce(nonce + 1)
    tx.schnorr_sign(config['passphrase'])
    if config['secondphrase']:
        tx.second_sign(config['secondphrase'])
    return tx.to_dict()


def broadcast(client, tx):
    """Returns the accepted transaction id or raises Abort with the node's reason."""
    try:
        body = client.transactions.create([tx])
    except Exception as e:
        raise Abort(f'Broadcast failed: {e}')
    data = body.get('data') or {}
    if tx['id'] in (data.get('accept') or []):
        return tx['id']
    raise Abort(f'Node did not accept the transfer: {body.get("errors") or data}')


def confirm(prompt):
    try:
        return input(f'{prompt} [y/N] ').strip().lower() in ('y', 'yes')
    except EOFError:
        return False


def run(config, client, exchange, assume_yes=False, interactive=False):
    """One drain run. Returns 0 on success; raises Abort when it stops before sending."""
    if config['test']:
        log.info('TEST MODE: checks and quotes only, no order is created and nothing is sent')

    wallet = fee = None
    try:
        wallet = get_wallet(client, config['convert_address'])
        fee = get_fee(client)
    except Abort as e:
        if not config['test']:
            raise
        log.warning('%s; quoting test_amt instead', e)

    if wallet is not None:
        if config['passphrase'] or not config['test']:
            check_keys(config, wallet)
        balance, nonce = int(wallet['balance']), int(wallet['nonce'])
        if wallet.get('publicKey'):
            try:
                pending = pending_from(client, wallet['publicKey'])
            except Exception as e:
                raise Abort(f'Cannot check the transaction pool: {e}')
            if pending:
                raise Abort('This wallet has transactions waiting in the pool; run again once they confirm')
        units = plan_amount(config, balance, fee)
        log.info('Balance %s ARK, fee %s ARK, keep %s ARK -> drain %s ARK', fmt(Decimal(balance) / ATOMIC),
                 fmt(Decimal(fee) / ATOMIC), fmt(config['keep']), fmt(Decimal(units) / ATOMIC))
        if units <= 0:
            raise Abort('Nothing to drain')
    else:
        units = int((config['fixed_amt'] if config['fixed'] else config['test_amt']) * ATOMIC)
        units -= units % UNIT
        nonce = None
    amount = Decimal(units) / ATOMIC

    if config['test']:
        quotes = exchange.quotes(amount)
        if not quotes:
            raise Abort('No usable quotes')
        best = quotes[0][0]
        log.info('Would use %s via %s (%s): %s ARK -> ~%s %s to %s', best.service, best.provider, best.rate_type,
                 fmt(amount), fmt(best.amount_to), config['convert_to'], config['address_to'])
        return 0

    order = exchange.create(amount, refund_address=config['convert_address'])
    if order is None:
        raise Abort('No exchange order could be created; nothing sent')

    tx = build_transfer(config, order.deposit_address, units, fee, nonce)
    summary = (f'Send {fmt(amount)} ARK (+{fmt(Decimal(fee) / ATOMIC)} fee) to {order.deposit_address} '
               f'for ~{fmt(order.amount_to)} {config["convert_to"]} via {order.service}/{order.provider} '
               f'(order {order.order_id})')
    if interactive and not assume_yes and not confirm(summary + '?'):
        exchange.record(order, amount, 'declined')
        raise Abort('Declined; the order was created but is unfunded and will expire')
    log.info(summary)
    try:
        txid = broadcast(client, tx)
    except Abort:
        exchange.record(order, amount, 'broadcast-failed')
        raise
    exchange.record(order, amount, 'sent', txid)
    log.info('Sent: transaction %s', txid)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--config', help='config file (default: $DRAIN_CONFIG or config.ini next to drain.py)')
    ap.add_argument('--test', action='store_true', help='checks and quotes only, whatever config says')
    ap.add_argument('--yes', action='store_true', help='do not ask for confirmation before sending')
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        config = get_config(args.config)
        if args.test:
            config['test'] = True
        lock = open(HERE / '.drain.lock', 'w')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Abort('Another drain run is in progress')
        exchange = Exchange(config)
        return run(config, get_client(config['node']), exchange, assume_yes=args.yes,
                   interactive=sys.stdin.isatty())
    except (Abort, ValueError) as e:
        log.error('%s', e)
        return 1
    except Exception:
        log.exception('Unexpected error; check exchange.txt for an order created in this run (it is only funded once logged as sent)')
        return 2


if __name__ == '__main__':
    sys.exit(main())
