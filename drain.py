"""Drain an ARK wallet into another coin (e.g. USDC on Polygon) via Flashift and/or Trocador.

Config is read from $DRAIN_CONFIG, or config.ini next to this script (see config.example.ini).
With test = Y it only fetches quotes; no order is created and nothing is sent.
"""
import logging
import os
import sys
from configparser import ConfigParser
from datetime import datetime
from pathlib import Path

from client import ArkClient
from crypto.configuration.network import set_custom_network
from crypto.transactions.builder.transfer import Transfer

from modules.exchange import ARK_ADDRESS, ATOMIC, Exchange, truncate

HERE = Path(__file__).resolve().parent
log = logging.getLogger('drain')


def get_config():
    path = Path(os.environ.get('DRAIN_CONFIG', HERE / 'config.ini'))
    if not path.exists():
        sys.exit(f'Config not found: {path} (copy config.example.ini to config.ini)')
    parser = ConfigParser(inline_comment_prefixes=(';', '#'))
    parser.read(path)
    s = parser['static']
    config = {
        'node': s.get('node', 'http://localhost:4003/api'),
        'test': s.get('test', 'Y').upper() == 'Y',
        'passphrase': os.environ.get('DRAIN_PASSPHRASE') or s.get('passphrase'),
        'secondphrase': os.environ.get('DRAIN_SECONDPHRASE') or s.get('secondphrase', 'None'),
        'convert_from': s.get('convert_from', 'ark'),
        'convert_address': s.get('convert_address'),
        'convert_to': s.get('convert_to', 'usdc'),
        'address_to': s.get('address_to'),
        'providers': [p.strip() for p in s.get('providers', 'Flashift, Trocador').split(',') if p.strip()],
        'fixed': s.get('fixed', 'N').upper() == 'Y',
        'fixed_amt': float(s.get('fixed_amt', '0')),
        'test_amt': float(s.get('test_amt', '750')),
        'exchange_log': str(HERE / s.get('exchange_log', 'exchange.txt')),
    }
    for section in ('flashift', 'trocador'):
        config[section] = dict(parser[section]) if parser.has_section(section) else {}
    if config['secondphrase'] in (None, '', 'None'):
        config['secondphrase'] = None
    if not ARK_ADDRESS.match(config['convert_address'] or ''):
        sys.exit('convert_address must be the ARK address being drained')
    return config


def get_client(node):
    set_custom_network(datetime(2017, 3, 21, 13, 0, 0), 23, 170)  # ARK mainnet epoch, version, wif
    return ArkClient(node)


def get_fee(client):
    dynamic = client.node.configuration()['data']['transactionPool']['dynamicFees']
    if str(dynamic.get('enabled')).lower() == 'false':
        return int(0.1 * ATOMIC)
    tx_size = 230 + len('drain')
    return int((dynamic['addonBytes']['transfer'] + tx_size) * dynamic['minFeePool'])


def build_transfer(config, recipient, amount, fee, nonce):
    tx = Transfer(recipientId=recipient, amount=amount, vendorField='drain', fee=fee)
    tx.set_nonce(nonce + 1)
    tx.schnorr_sign(config['passphrase'])
    if config['secondphrase']:
        tx.second_sign(config['secondphrase'])
    return tx.to_dict()


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    config = get_config()
    exchange = Exchange(config)

    if config['test']:
        amount = config['fixed_amt'] if config['fixed'] and config['fixed_amt'] else config['test_amt']
        log.info('Test mode: quotes only for %s %s, no order is created', amount, config['convert_from'])
        quotes = exchange.quotes(amount)
        if not quotes:
            sys.exit('No quotes')
        best = quotes[0][0]
        log.info('Would use %s via %s (%s): ~%.6f %s', best.service, best.provider, best.rate_type,
                 best.amount_to, config['convert_to'])
        return

    client = get_client(config['node'])
    fee = get_fee(client)
    wallet = client.wallets.get(config['convert_address'])['data']
    nonce, balance = int(wallet['nonce']), int(wallet['balance'])

    if config['fixed']:
        wanted = int(config['fixed_amt'] * ATOMIC)
        if balance - fee < wanted:
            log.info('Balance below fixed amount, draining what is there')
            wanted = balance - fee
    else:
        wanted = balance - fee

    # Order and transfer use the same amount, truncated to 4 decimals; the dust stays in the wallet.
    amount = truncate(wanted / ATOMIC, 4)
    if amount <= 0:
        sys.exit(f'Nothing to drain (balance {balance / ATOMIC}, fee {fee / ATOMIC})')
    log.info('Draining %s %s (fee %s)', amount, config['convert_from'], fee / ATOMIC)

    order = exchange.create(amount, refund_address=config['convert_address'])
    if order is None:
        sys.exit('Failed exchange - nothing sent')

    tx = build_transfer(config, order.deposit_address, int(round(amount * ATOMIC)), fee, nonce)
    log.info('Transfer: %s', tx)
    log.info('Broadcast: %s', client.transactions.create([tx]))


if __name__ == '__main__':
    main()
