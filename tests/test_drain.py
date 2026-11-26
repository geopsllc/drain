"""Offline tests for drain: fake ARK node, faked exchange worker HTTP. Run: .venv/bin/python -m unittest -v"""
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from decimal import Decimal as D
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import drain  # noqa: E402
from modules import exchange as ex  # noqa: E402
from modules.exchange import Exchange, Order, Quote, fmt, is_ark_address  # noqa: E402

drain.get_client('http://unused')  # sets the ARK mainnet network for the crypto library

PASS = 'this is a throwaway test passphrase only'
ME = drain.address_from_passphrase(PASS, network_version=23)
ME_PK = drain.PublicKey.from_passphrase(PASS)
SECOND = 'another throwaway phrase for second signature'
DEP = 'AQEJ6EC3UxyRoHVrzCt9vMrABknR5hhiwR'          # real, valid ARK address
DEP_BADSUM = 'AQEJ6EC3UxyRoHVrzCt9vMrABknR5hhiwX'    # matches the old regex, wrong checksum
OUT = '0x1111111111111111111111111111111111111111'


def make_config(**over):
    cfg = {
        'node': 'http://node', 'test': False, 'passphrase': PASS, 'secondphrase': '',
        'convert_from': 'ark', 'convert_address': ME, 'convert_to': 'usdc', 'network': 'polygon',
        'address_to': OUT, 'worker': 'https://worker.test', 'providers': ['Flashift', 'Trocador'], 'fixed': False, 'fixed_amt': D(0), 'min': False, 'min_amt': D(0), 'test_amt': D(750),
        'min_rate': D(0), 'keep': D(0), 'exchange_log': os.devnull,
    }
    cfg.update(over)
    return cfg


class Resp:
    def __init__(self, status, body, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        if isinstance(self._body, str):
            raise ValueError('not json')
        return self._body


class FakeNode:
    """Just enough of arkecosystem-client's ArkClient."""
    def __init__(self, balance=1000 * 10**8 + 1_005_000, nonce=7, attributes=None, pool=None, accept=True,
                 fees=None, wallet_error=None, broadcast_error=None, nethash=drain.MAINNET_NETHASH):
        self.broadcasts = []
        outer = self

        class Node:
            def configuration(self):
                if fees == 'error':
                    raise RuntimeError('node down')
                return {'data': {'nethash': nethash, 'transactionPool': {'dynamicFees': fees or {
                    'enabled': True, 'minFeePool': 3000, 'addonBytes': {'transfer': 100}}}}}

        class Wallets:
            def get(self, address):
                if wallet_error:
                    raise RuntimeError(wallet_error)
                return {'data': {'address': address, 'publicKey': ME_PK, 'balance': str(balance),
                                 'nonce': str(nonce), 'attributes': attributes or {}}}

        class Transactions:
            def all_unconfirmed(self, limit=100, page=1, **kw):
                return {'data': (pool or []) if page == 1 else []}

            def create(self, txs):
                outer.broadcasts.append(txs)
                if broadcast_error:
                    raise RuntimeError(broadcast_error)
                ids = [t['id'] for t in txs]
                return {'data': {'accept': ids if accept else [], 'broadcast': ids if accept else [],
                                 'invalid': [] if accept else ids},
                        'errors': None if accept else {ids[0]: [{'type': 'ERR_LOW_FEE'}]}}

        self.node, self.wallets, self.transactions = Node(), Wallets(), Transactions()


class FakeHTTP:
    """Routes requests.get/post by URL suffix to handlers returning Resp; records calls."""
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, method):
        def handler(url, **kw):
            self.calls.append((method, url.rsplit('/', 1)[-1], kw))
            for suffix, fn in self.routes.items():
                if url.endswith(suffix):
                    out = fn(kw) if callable(fn) else fn
                    if isinstance(out, Exception):
                        raise out
                    return out
            return Resp(404, {'error': 'no route'})
        return handler

    def count(self, name):
        return sum(1 for c in self.calls if c[1] == name)


W_QUOTES = {'amount': '1000', 'network': 'polygon', 'coin': 'usdc', 'errors': [], 'at': 1, 'quotes': [
    {'service': 'Trocador', 'provider': 'SimpleSwap', 'rateType': 'floating', 'amountTo': '128.5',
     'networkTo': 'MATIC', 'ref': {'id': 'R1'}, 'kyc': 'B', 'servicePick': True, 'best': True},
    {'service': 'Flashift', 'provider': 'Changelly', 'rateType': 'floating', 'amountTo': '127.9',
     'networkTo': 'pol', 'ref': {}, 'tags': ['Recommended'], 'servicePick': True, 'best': False},
    {'service': 'Trocador', 'provider': 'MoneroBridge', 'rateType': 'floating', 'amountTo': '126.9',
     'networkTo': 'MATIC', 'ref': {'id': 'R1'}, 'kyc': 'A', 'best': False}]}


def w_order(**over):
    body = {'service': 'Trocador', 'provider': 'SimpleSwap', 'orderId': 'T1', 'providerOrderId': 'ss123',
            'depositAddress': DEP, 'depositMemo': '', 'payoutAddress': OUT, 'amountTo': '128.5',
            'amountFrom': '1000', 'symbolTo': 'usdc', 'networkTo': 'MATIC'}
    body.update(over)
    return Resp(200, {'order': body})


def fl_order(**over):
    body = {'service': 'Flashift', 'provider': 'Changelly', 'orderId': 'Changelly_floating_x', 'providerOrderId': 'x',
            'depositAddress': DEP, 'depositMemo': '', 'payoutAddress': OUT, 'amountTo': '127.9',
            'amountFrom': '1000', 'symbolTo': 'usdc', 'networkTo': 'pol'}
    body.update(over)
    return Resp(200, {'order': body})


def default_routes(quote=None, trocador=None, flashift=None, monero=None):
    """The worker's /quote and /order; orders are answered per quoted service (MoneroBridge separately)."""
    answers = {'Trocador': trocador or w_order(), 'Flashift': flashift or fl_order(),
               'MoneroBridge': monero or w_order(provider='MoneroBridge', amountTo='126.9', providerOrderId='')}

    def order(kw):
        q = kw['json']['quote']
        out = answers['MoneroBridge' if q['provider'] == 'MoneroBridge' else q['service']]
        return out(kw) if callable(out) else out
    return {'/quote': quote or Resp(200, W_QUOTES), '/order': order}


def quoted_amounts(http):
    return [c[2]['json']['amount'] for c in http.calls if c[1] == 'quote']


class HTTPTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(ex.time, 'sleep', lambda s: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.http = None

    def use(self, routes):
        self.http = FakeHTTP(routes)
        for method in ('get', 'post'):
            p = mock.patch.object(ex.requests, method, self.http(method))
            p.start()
            self.addCleanup(p.stop)
        return self.http


# ---------------------------------------------------------------- helpers

class HelperTests(unittest.TestCase):
    def test_ark_address_checksum(self):
        self.assertTrue(is_ark_address(DEP))
        self.assertTrue(is_ark_address(ME))
        self.assertFalse(is_ark_address(DEP_BADSUM))
        for bad in ('', None, 123, OUT, DEP + 'A', DEP[:-1], 'D' + DEP[1:], DEP.lower()):
            self.assertFalse(is_ark_address(bad), bad)

    def test_fmt(self):
        self.assertEqual(fmt(D('1E+3')), '1000')
        self.assertEqual(fmt(D('300.1000')), '300.1')
        self.assertEqual(fmt(D('0.0001')), '0.0001')
        self.assertEqual(fmt(D(29_000_000) / 100_000_000), '0.29')
        self.assertEqual(fmt(D('5.00')), '5')

    def test_to_decimal_rejects_garbage(self):
        for bad in (None, '', 'abc', 'NaN', 'Infinity', [], {}):
            self.assertIsNone(ex.to_decimal(bad), bad)
        self.assertEqual(ex.to_decimal(' 1.5 '.strip()), D('1.5'))


# ---------------------------------------------------------------- config

class ConfigTests(unittest.TestCase):
    def write(self, body):
        f = tempfile.NamedTemporaryFile('w', suffix='.ini', delete=False)
        f.write(textwrap.dedent(body))
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def base(self, **static):
        values = {'convert_address': ME, 'address_to': OUT, 'passphrase': PASS}
        values.update(static)
        lines = '\n'.join(f'{k} = {v}' for k, v in values.items())
        return f'[static]\n{lines}\n'

    def test_valid_config_and_defaults(self):
        cfg = drain.get_config(self.write(self.base()))
        self.assertTrue(cfg['test'], 'test mode must be the default')
        self.assertEqual((cfg['convert_to'], cfg['network'], cfg['worker']), ('usdc', 'polygon', 'https://xc.geops.net'))
        self.assertEqual(cfg['providers'], ['Flashift', 'Trocador'])
        cfg = drain.get_config(self.write(self.base(exchange_log='100%.txt')))
        self.assertTrue(cfg['exchange_log'].endswith('100%.txt'), '% must not break parsing')
        self.assertEqual(cfg['secondphrase'], '')

    def test_passphrase_whitespace_normalized(self):
        cfg = drain.get_config(self.write(self.base(passphrase='  ' + PASS.replace(' ', '   ') + '  ')))
        self.assertEqual(cfg['passphrase'], PASS)

    def test_rejections(self):
        cases = {
            'missing file': None,
            'bad checksum': self.base(convert_address=DEP_BADSUM),
            'empty address_to': self.base(address_to=''),
            'non-0x address_to': self.base(address_to=DEP),
            'short 0x': self.base(address_to='0x1234'),
            'bad number': self.base(fixed_amt='lots'),
            'negative number': self.base(keep='-5'),
            'fixed without amount': self.base(fixed='Y', fixed_amt='0'),
            'min without amount': self.base(min='Y', min_amt='0'),
            'min above fixed': self.base(fixed='Y', fixed_amt='400', min='Y', min_amt='500'),
            'not ark': self.base(convert_from='btc'),
            'unknown network': self.base(network='tron'),
            'unknown coin': self.base(convert_to='btc'),
            'usdt on base': self.base(convert_to='usdt', network='base'),
            'worker over plain http': self.base(worker='http://xc.geops.net'),
            'worker not a url': self.base(worker='xc.geops.net'),
            'no static': '[flashift]\napi_key = x\n',
            'garbage': 'this is not an ini file',
        }
        for name, body in cases.items():
            with self.subTest(name), self.assertRaises(drain.Abort):
                drain.get_config('/nonexistent/config.ini' if body is None else self.write(body))

    def test_inline_comments(self):
        cfg = drain.get_config(self.write(self.base(test='N ; live', min_rate='0.1 # floor')))
        self.assertFalse(cfg['test'])
        self.assertEqual(cfg['min_rate'], D('0.1'))

    def test_world_readable_config_with_passphrase(self):
        path = self.write(self.base(test='N'))
        os.chmod(path, 0o644)
        with self.assertRaises(drain.Abort):
            drain.get_config(path)
        os.chmod(path, 0o644)
        drain.get_config(self.write(self.base()))  # test mode: only a warning
        loose = self.write(self.base())
        os.chmod(loose, 0o640)
        with self.assertLogs('drain', 'WARNING'):
            drain.get_config(loose)
        no_phrase = self.write(self.base(test='N', passphrase=''))
        os.chmod(no_phrase, 0o644)
        drain.get_config(no_phrase)  # passphrase from the environment: nothing secret in the file

    def test_node_url(self):
        for bad in ('ftp://node', 'localhost:4003', 'http://'):
            with self.subTest(bad), self.assertRaises(drain.Abort):
                drain.get_config(self.write(self.base(node=bad)))
        with self.assertLogs('drain', 'WARNING'):
            drain.get_config(self.write(self.base(node='http://node.example.com:4003/api')))
        for ok in ('http://localhost:4003/api', 'http://127.0.0.1:4003/api', 'http://192.168.1.9:4003/api',
                   'https://api.ark.io/api'):
            self.assertTrue(drain.get_config(self.write(self.base(node=ok)))['node'])

    def test_coins_and_networks(self):
        for net, coins in ex.NETWORKS.items():
            for coin in coins:
                with self.subTest(f'{coin} on {net}'):
                    cfg = drain.get_config(self.write(self.base(convert_to=coin.upper(), network=net.title())))
                    self.assertEqual((cfg['convert_to'], cfg['network']), (coin, net))

    def test_worker_url(self):
        for ok in ('https://xc.geops.net/', 'http://127.0.0.1:8787', 'http://localhost:8787'):
            self.assertEqual(drain.get_config(self.write(self.base(worker=ok)))['worker'], ok.rstrip('/'))
        with mock.patch.dict(os.environ, {'DRAIN_WORKER': 'https://other.example'}):
            self.assertEqual(drain.get_config(self.write(self.base()))['worker'], 'https://other.example')

    def test_old_key_sections_are_ignored(self):
        with self.assertLogs('drain', 'WARNING') as logs:
            drain.get_config(self.write(self.base() + '[flashift]\napi_key = x\n[trocador]\napi_key = y\n'))
        self.assertIn('ignored', ' '.join(logs.output))

    def test_env_overrides(self):
        with mock.patch.dict(os.environ, {'DRAIN_PASSPHRASE': 'from env'}):
            self.assertEqual(drain.get_config(self.write(self.base()))['passphrase'], 'from env')


# ---------------------------------------------------------------- amounts and keys

class PlanTests(unittest.TestCase):
    def test_amounts(self):
        A = 100_000_000
        c = make_config()
        self.assertEqual(drain.plan_amount(c, 1000 * A, A // 100), 999 * A + 99 * A // 100)
        self.assertEqual(drain.plan_amount(c, 5, 10), 0, 'balance below fee')
        self.assertEqual(drain.plan_amount(c, 10 + 9_999, 10), 0, 'less than 0.0001 after fee')
        self.assertEqual(drain.plan_amount(c, 29_000_000 + 7, 7), 29_000_000, 'no float rounding loss')
        self.assertEqual(drain.plan_amount(make_config(keep=D(5)), 10 * A, 0), 5 * A)
        self.assertEqual(drain.plan_amount(make_config(keep=D(50)), 10 * A, 0), 0, 'keep above balance')
        fx = make_config(fixed=True, fixed_amt=D('2.5'))
        self.assertEqual(drain.plan_amount(fx, 10 * A, 1), int(2.5 * A))
        self.assertEqual(drain.plan_amount(fx, 2 * A, 1), 2 * A - 10_000, 'fixed above balance drains the rest')

    def test_fee(self):
        self.assertEqual(drain.get_fee(FakeNode()), (100 + 235) * 3000)
        self.assertEqual(drain.get_fee(FakeNode(fees='error')), drain.STATIC_FEE)
        self.assertEqual(drain.get_fee(FakeNode(fees={'enabled': False})), drain.STATIC_FEE)
        self.assertEqual(drain.get_fee(FakeNode(fees={'enabled': True})), drain.STATIC_FEE, 'malformed')
        huge = {'enabled': True, 'minFeePool': 10**9, 'addonBytes': {'transfer': 100}}
        self.assertEqual(drain.get_fee(FakeNode(fees=huge)), drain.STATIC_FEE, 'capped at static fee')

    def test_keys(self):
        drain.check_keys(make_config(), {'attributes': {}})
        second_pk = drain.PublicKey.from_passphrase(SECOND)
        drain.check_keys(make_config(secondphrase=SECOND), {'attributes': {'secondPublicKey': second_pk}})
        bad = {
            'empty passphrase': (make_config(passphrase=''), {}),
            'wrong passphrase': (make_config(passphrase='some other words'), {}),
            'second missing': (make_config(), {'secondPublicKey': second_pk}),
            'second wrong': (make_config(secondphrase='nope'), {'secondPublicKey': second_pk}),
            'multisig': (make_config(), {'multiSignature': {'min': 2}}),
        }
        for name, (cfg, attrs) in bad.items():
            with self.subTest(name), self.assertRaises(drain.Abort):
                drain.check_keys(cfg, {'attributes': attrs})
        cfg = make_config(secondphrase=SECOND)
        drain.check_keys(cfg, {'attributes': {}})
        self.assertEqual(cfg['secondphrase'], '', 'unneeded second phrase is dropped')

    def test_transfer_signing(self):
        tx = drain.build_transfer(make_config(), DEP, 12_345_670_000, 1_005_000, 7)
        self.assertEqual((tx['recipientId'], int(tx['amount']), int(tx['fee']), int(tx['nonce'])),
                         (DEP, 12_345_670_000, 1_005_000, 8))
        self.assertEqual(tx['vendorField'], 'drain')
        self.assertTrue(tx['signature'] and tx['id'])
        self.assertNotIn('signSignature', tx)
        tx2 = drain.build_transfer(make_config(secondphrase=SECOND), DEP, 1, 1, 0)
        self.assertTrue(tx2.get('signSignature'))


# ---------------------------------------------------------------- provider selection

class ParseTests(unittest.TestCase):
    def test_quotes(self):
        q = Exchange.parse_quote(W_QUOTES['quotes'][0])
        self.assertEqual((q.service, q.provider, q.rate_type, q.amount_to, q.network_to, q.ref, q.kyc),
                         ('Trocador', 'SimpleSwap', 'floating', D('128.5'), 'MATIC', {'id': 'R1'}, 'B'))
        self.assertEqual(q.as_request(), {'service': 'Trocador', 'provider': 'SimpleSwap', 'rateType': 'floating',
                                          'ref': {'id': 'R1'}})
        good = W_QUOTES['quotes'][1]
        for bad in ('str', None, 5, {}, dict(good, service='Other'), dict(good, provider=''),
                    dict(good, amountTo='NaN'), dict(good, amountTo='-3'), dict(good, amountTo='0'),
                    dict(good, rateType='any')):
            self.assertIsNone(Exchange.parse_quote(bad), bad)
        self.assertEqual(Exchange.parse_quote(dict(good, ref='junk')).ref, {})

    def test_order(self):
        o = Exchange.parse_order(w_order()._body['order'])
        self.assertEqual((o.order_id, o.provider_order_id, o.amount_to, o.network_to), ('T1', 'ss123', D('128.5'), 'MATIC'))
        o = Exchange.parse_order({'orderId': 7, 'amountTo': None, 'depositMemo': None})
        self.assertEqual((o.order_id, o.amount_to, o.deposit_memo), ('7', None, ''))
        with self.assertRaises(RuntimeError):
            Exchange.parse_order(None)


# ---------------------------------------------------------------- exchange over a faked worker

class ExchangeTests(HTTPTest):
    def exchange(self, **over):
        return Exchange(make_config(**over))

    def test_best_quote_wins(self):
        self.use(default_routes())
        order = self.exchange().create(D(1000), ME)
        self.assertEqual((order.service, order.provider, order.deposit_address), ('Trocador', 'SimpleSwap', DEP))
        self.assertEqual(self.http.count('order'), 1, 'no second order once one succeeded')
        call = [c for c in self.http.calls if c[1] == 'order'][0][2]
        self.assertEqual(call['json'], {'amount': '1000', 'network': 'polygon', 'coin': 'usdc',
                                        'quote': {'service': 'Trocador', 'provider': 'SimpleSwap',
                                                  'rateType': 'floating', 'ref': {'id': 'R1'}},
                                        'payoutAddress': OUT, 'refundAddress': ME})
        self.assertEqual(call['headers']['User-Agent'], ex.USER_AGENT)
        self.assertTrue(call['timeout'])

    def test_fallback_when_best_fails(self):
        for name, bad in {'worker error': Resp(502, {'error': 'boom'}), 'html': Resp(200, '<html>'),
                          'no order in reply': Resp(200, {'ok': True}),
                          'timeout': ex.requests.Timeout('slow'), 'bad memo': w_order(depositMemo='123'),
                          'bad checksum': w_order(depositAddress=DEP_BADSUM),
                          'own address': w_order(depositAddress=ME),
                          'evm deposit': w_order(depositAddress=OUT),
                          'payout elsewhere': w_order(payoutAddress='0x' + '2' * 40),
                          'wrong amount': w_order(amountFrom='999'),
                          'wrong coin': w_order(symbolTo='usdt'),
                          'no coin': w_order(symbolTo=''),
                          'payout far below quote': w_order(amountTo='100'),
                          'no payout amount': w_order(amountTo=None),
                          'wrong network': w_order(networkTo='ERC20'),
                          'no network': w_order(networkTo=''),
                          'other service': w_order(service='Flashift'),
                          'no order id': w_order(orderId=None),
                          'non-string payout': w_order(payoutAddress={'x': 1})}.items():
            with self.subTest(name):
                self.use(default_routes(trocador=bad))
                order = self.exchange().create(D(1000), ME)
                self.assertEqual(order.service, 'Flashift')
                tried = [c[2]['json']['quote']['service'] for c in self.http.calls if c[1] == 'order']
                self.assertEqual(tried, ['Trocador', 'Flashift'], 'each order is tried once, never retried')

    def test_case_insensitive_matches(self):
        self.use(default_routes(trocador=w_order(networkTo='matic', symbolTo='USDC',
                                                 payoutAddress=OUT.upper().replace('0X', '0x'))))
        self.assertEqual(self.exchange().create(D(1000), ME).service, 'Trocador')

    def test_everything_fails(self):
        self.use(default_routes(trocador=Resp(502, {'error': 'x'}), flashift=fl_order(networkTo='eth'),
                                monero=w_order(provider='MoneroBridge', depositMemo='5')))
        self.assertIsNone(self.exchange().create(D(1000), ME))
        self.assertEqual(self.http.count('order'), 3)

    def test_at_most_three_orders(self):
        quotes = dict(W_QUOTES, quotes=W_QUOTES['quotes'] + [dict(W_QUOTES['quotes'][2], provider='Other')] * 2)
        self.use(default_routes(quote=Resp(200, quotes), trocador=Resp(502, {'error': 'x'}),
                                flashift=Resp(502, {'error': 'x'}), monero=Resp(502, {'error': 'x'})))
        self.assertIsNone(self.exchange().create(D(1000), ME))
        self.assertEqual(self.http.count('order'), ex.MAX_ORDER_ATTEMPTS)

    def test_rate_limited_order_stops(self):
        self.use(default_routes(trocador=Resp(429, {'error': 'Too many requests', 'retryAfter': 20})))
        self.assertIsNone(self.exchange().create(D(1000), ME))
        self.assertEqual(self.http.count('order'), 1, 'the next order would be refused too')

    def test_bad_request_is_not_retried(self):
        self.use(default_routes(quote=Resp(400, {'error': 'amount too large'})))
        self.assertEqual(self.exchange().quotes(D(1000)), [])
        self.assertEqual(self.http.count('quote'), 1)

    def test_quote_retries(self):
        for first in (Resp(502, '<html>'), ex.requests.ConnectionError('down'),
                      Resp(429, {'error': 'busy', 'retryAfter': 12}), Resp(429, 'busy', {'Retry-After': '5'})):
            with self.subTest(first), mock.patch.object(ex.time, 'sleep') as sleep:
                attempts = iter([first, Resp(200, W_QUOTES)])
                self.use(default_routes(quote=lambda kw: next(attempts)))
                self.assertEqual(len(self.exchange().quotes(D(1000))), 3)
                self.assertEqual(self.http.count('quote'), 2)
                self.assertEqual(sleep.call_count, 1)

    def test_quote_gives_up(self):
        for name, answer in {'down': ex.requests.ConnectionError('down'),
                             'long wait': Resp(429, {'error': 'busy', 'retryAfter': 600})}.items():
            with self.subTest(name):
                self.use(default_routes(quote=answer))
                self.assertEqual(self.exchange().quotes(D(1000)), [])
                self.assertEqual(self.http.count('quote'), 1 if name == 'long wait' else 2)

    def test_service_errors_are_logged(self):
        self.use(default_routes(quote=Resp(200, dict(W_QUOTES, errors=[{'service': 'Flashift', 'message': 'busy'}]))))
        with self.assertLogs('modules.exchange', 'WARNING') as logs:
            self.exchange().quotes(D(1000))
        self.assertIn('Flashift quote failed: busy', ' '.join(logs.output))

    def test_garbage_quotes_are_skipped(self):
        self.use(default_routes(quote=Resp(200, dict(W_QUOTES, quotes=['x', None, {'service': 'Flashift'}] + W_QUOTES['quotes']))))
        self.assertEqual(len(self.exchange().quotes(D(1000))), 3)
        self.use(default_routes(quote=Resp(200, {'quotes': None})))
        self.assertEqual(self.exchange().quotes(D(1000)), [])

    def test_min_rate(self):
        self.use(default_routes())
        self.assertEqual([q.provider for q in self.exchange(min_rate=D('0.1275')).quotes(D(1000))],
                         ['SimpleSwap', 'Changelly'])
        self.assertEqual(self.exchange(min_rate=D('0.2')).quotes(D(1000)), [])

    def test_request_body(self):
        self.use(default_routes())
        self.exchange(convert_to='usdt', network='bsc').quotes(D('0.29'))
        self.assertEqual(self.http.calls[0][2]['json'], {'amount': '0.29', 'network': 'bsc', 'coin': 'usdt'})
        self.assertEqual(self.http.calls[0][0], 'post')

    def test_provider_list(self):
        with self.assertRaises(ValueError):
            Exchange(make_config(providers=['ChangeNow']))
        self.assertEqual(Exchange(make_config(providers=['flashift', 'Flashift', 'TROCADOR'])).services,
                         ['Flashift', 'Trocador'])
        self.use(default_routes())
        quotes = self.exchange(providers=['Flashift']).quotes(D(1000))
        self.assertEqual([q.service for q in quotes], ['Flashift'])
        self.assertEqual(self.exchange(providers=['Flashift']).create(D(1000), ME).service, 'Flashift')

    def test_log_write_failure_is_harmless(self):
        self.use(default_routes())
        self.assertIsNotNone(self.exchange(exchange_log='/nonexistent/dir/log.txt').create(D(1000), ME))


# ---------------------------------------------------------------- full runs

class TrackingTests(unittest.TestCase):
    def order(self, service, provider, oid, pid=''):
        return ex.Order(service, provider, oid, DEP, '', OUT, D(1), provider_order_id=pid)

    def test_exchange_pages(self):
        cases = [  # ids from real unfunded test orders
            ('Changelly', 'gco7jsjggps6ka86', 'https://changelly.com/track/gco7jsjggps6ka86'),
            ('ChangeNOW', 'fed28fc2e80b29', 'https://changenow.io/exchange/txs/fed28fc2e80b29'),
            ('Simpleswap', 'q7oh8nn6izpppfmv', 'https://simpleswap.io/exchange?id=q7oh8nn6izpppfmv'),
            ('StealthEx', 'nkPZUaTLv', 'https://stealthex.io/exchange/?id=nkPZUaTLv'),
        ]
        for provider, pid, url in cases:
            with self.subTest(provider):
                self.assertEqual(self.order('Trocador', provider, 'T1', pid).tracking(), [(provider, url)])

    def test_fallbacks(self):
        self.assertEqual(self.order('Trocador', 'Exolix', '70Qwk1gyct', 'abc').tracking(),
                         [('Trocador', 'https://trocador.app/checkout/70Qwk1gyct')], 'unknown exchange')
        self.assertEqual(self.order('Trocador', 'Simpleswap', 'T1').tracking()[0][0], 'Trocador', 'no exchange id')
        self.assertEqual([l for l, _ in self.order('Flashift', 'Exolix', 'Exolix_floating_ab', 'ab').tracking()],
                         ['Flashift'])
        self.assertEqual(self.order('Trocador', 'StealthEx', 'T', 'a/b?c').tracking()[0][1],
                         'https://stealthex.io/exchange/?id=a%2Fb%3Fc')


class RunTests(HTTPTest):
    def run_drain(self, node, interactive=False, answer='y', **over):
        cfg = make_config(**over)
        with mock.patch('builtins.input', lambda p: answer):
            return drain.run(cfg, node, Exchange(cfg), interactive=interactive)

    def test_live_run(self):
        self.use(default_routes())
        node = FakeNode(balance=1000 * 10**8 + 1_005_000 + 1234)
        with tempfile.NamedTemporaryFile('r', suffix='.txt') as log:
            self.assertEqual(self.run_drain(node, exchange_log=log.name), 0)
            rows = log.read().strip().splitlines()
        tx = node.broadcasts[0][0]
        self.assertEqual((tx['recipientId'], int(tx['amount']), int(tx['nonce'])), (DEP, 1000 * 10**8, 8))
        self.assertEqual([r.split(',')[1] for r in rows], ['created', 'sent'])
        self.assertEqual(rows[1].split(',')[-2], tx['id'])
        self.assertEqual(rows[1].split(',')[-1], 'SimpleSwap: https://simpleswap.io/exchange?id=ss123')

    def test_aborts_before_ordering(self):
        second_pk = drain.PublicKey.from_passphrase(SECOND)
        cases = {
            'wrong passphrase': (FakeNode(), {'passphrase': 'wrong words'}),
            'second phrase missing': (FakeNode(attributes={'secondPublicKey': second_pk}), {}),
            'pending tx': (FakeNode(pool=[{'senderPublicKey': ME_PK, 'id': 'x'}]), {}),
            'empty wallet': (FakeNode(balance=0), {}),
            'dust only': (FakeNode(balance=1_005_000 + 9_999), {}),
            'wallet unreadable': (FakeNode(wallet_error='404 Wallet not found'), {}),
        }
        for name, (node, over) in cases.items():
            with self.subTest(name):
                self.use(default_routes())
                with self.assertRaises(drain.Abort):
                    self.run_drain(node, **over)
                self.assertEqual(self.http.count('order'), 0)
                self.assertEqual(node.broadcasts, [])

    def test_wrong_network_is_fatal_even_in_test_mode(self):
        for test in (False, True):
            with self.subTest(test=test):
                self.use(default_routes())
                node = FakeNode(nethash='2a44f340d76ffc3df204c5f38cd355b7496c9065a1ade2ef92071436bd72e867')
                with self.assertRaises(drain.WrongNetwork):
                    self.run_drain(node, test=test)
                self.assertEqual(self.http.calls, [])

    def test_min_amount(self):
        fee = 1_005_000
        over = dict(fixed=True, fixed_amt=D(1000), min=True, min_amt=D(500))
        self.use(default_routes())
        node = FakeNode(balance=1500 * 10**8 + fee)
        self.assertEqual(self.run_drain(node, **over), 0)
        self.assertEqual(int(node.broadcasts[0][0]['amount']), 1000 * 10**8, 'fixed_amt, not the whole balance')

        self.use(default_routes())
        node = FakeNode(balance=300 * 10**8 + fee)
        self.assertEqual(self.run_drain(node, **over), 0, 'below min_amt is a quiet skip')
        self.assertEqual(self.http.calls, [])
        self.assertEqual(node.broadcasts, [])

        self.use(default_routes())
        self.assertEqual(self.run_drain(FakeNode(balance=300 * 10**8 + fee), test=True, **over), 0)
        self.assertEqual(quoted_amounts(self.http), ['1000'], 'test mode still quotes (fixed_amt) below min_amt')

        self.use(default_routes())
        node = FakeNode(balance=500 * 10**8 + fee)
        with self.assertRaises(drain.Abort):  # exactly min_amt is sent; fails only because the fixture order is for 1000
            self.run_drain(node, **over)
        self.assertEqual(quoted_amounts(self.http), ['500'], 'not skipped')

    def test_others_in_pool_are_fine(self):
        self.use(default_routes())
        self.assertEqual(self.run_drain(FakeNode(pool=[{'senderPublicKey': '02' + 'ab' * 32}])), 0)

    def test_no_order_no_send(self):
        self.use(default_routes(trocador=Resp(502, {}), flashift=Resp(200, {'error': 'x'}), monero=Resp(500, {})))
        node = FakeNode()
        with self.assertRaises(drain.Abort):
            self.run_drain(node)
        self.assertEqual(node.broadcasts, [])

    def test_broadcast_rejected_or_failing(self):
        for name, node in {'rejected': FakeNode(accept=False), 'error': FakeNode(broadcast_error='timeout')}.items():
            with self.subTest(name), tempfile.NamedTemporaryFile('r', suffix='.txt') as log:
                self.use(default_routes())
                with self.assertRaises(drain.Abort):
                    self.run_drain(node, exchange_log=log.name)
                self.assertEqual(log.read().strip().splitlines()[-1].split(',')[1], 'broadcast-failed')

    def test_interactive_confirm(self):
        self.use(default_routes())
        node = FakeNode()
        with self.assertRaises(drain.Abort):
            self.run_drain(node, interactive=True, answer='n')
        self.assertEqual(node.broadcasts, [])
        self.assertEqual(self.run_drain(node, interactive=True, answer='yes'), 0)
        self.assertEqual(len(node.broadcasts), 1)

    def test_test_mode_never_orders_or_sends(self):
        for name, node in {'node ok': FakeNode(), 'node down': FakeNode(wallet_error='refused')}.items():
            with self.subTest(name):
                self.use(default_routes())
                self.assertEqual(self.run_drain(node, test=True, passphrase=''), 0)
                self.assertEqual(self.http.count('order'), 0)
                self.assertEqual(node.broadcasts, [])
                self.assertEqual(quoted_amounts(self.http), ['750'], 'test mode quotes test_amt, node up or down')

    def test_test_mode_amount(self):
        cases = {
            'test_amt, not the balance': ({}, FakeNode(balance=5000 * 10**8), '750'),
            'fixed_amt when fixed = Y': ({'fixed': True, 'fixed_amt': D(1000)}, FakeNode(balance=5000 * 10**8), '1000'),
            'fixed_amt above balance': ({'fixed': True, 'fixed_amt': D(1000)}, FakeNode(balance=10 * 10**8), '1000'),
            'empty wallet still quotes': ({}, FakeNode(balance=0), '750'),
        }
        for name, (over, node, want) in cases.items():
            with self.subTest(name):
                self.use(default_routes())
                self.assertEqual(self.run_drain(node, test=True, **over), 0)
                self.assertEqual(quoted_amounts(self.http), [want])
                self.assertEqual(node.broadcasts, [])

    def test_test_mode_still_checks_a_given_passphrase(self):
        self.use(default_routes())
        with self.assertRaises(drain.Abort):
            self.run_drain(FakeNode(), test=True, passphrase='wrong words')


def native_coincurve():
    """The real coincurve package, or None (drain may have put the compat module under that name)."""
    import importlib.machinery
    import importlib.util
    spec = importlib.machinery.PathFinder.find_spec('coincurve')
    if spec is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SigningBackendTests(unittest.TestCase):
    """Signing must be byte-identical with real coincurve and with modules/coincurve_compat (Python 3.14+)."""
    TX = ('94d3222d5d84030b41560f023599ad63c085c8a23d7019564d7f3d10b33fbd0e',
          '7d5a3349fc9de3503f2251feabc1efa1bde753ee3fb43980410296b12798e0b8520a0a0f8bc9e8c3ee21df72a084447f2599c4cbadc2f0c604ccbe50b7b1bba9',
          'd8d524e1ac5826f033768859147a6b1feae5bdbec1bae3b078b665c2bf07d38e832bdec76f04fd7b0938f1a2d951fa0585eb317fb3c9c1d9efe0a800f6f67acf')
    SCRIPT = (
        'import sys, json\n'
        'if sys.argv[1] == "compat": sys.modules["coincurve"] = None\n'
        'sys.path.insert(0, sys.argv[2]); import drain; drain.get_client("x")\n'
        'tx = drain.build_transfer({"passphrase": sys.argv[3], "secondphrase": sys.argv[4]}, sys.argv[5], 12345670000, 1005000, 7)\n'
        'print(json.dumps([sys.modules["coincurve"].__name__, tx["id"], tx["signature"], tx["signSignature"], tx["senderPublicKey"],'
        ' drain.address_from_passphrase(sys.argv[3], network_version=23)]))\n')

    def sign_with(self, backend):
        out = subprocess.run([sys.executable, '-W', 'ignore', '-c', self.SCRIPT, backend, str(ROOT), PASS, SECOND, DEP],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_known_vector_both_backends(self):
        backends = ['compat'] + (['native'] if native_coincurve() else [])
        for backend in backends:
            with self.subTest(backend):
                name, txid, sig, sig2, pk, addr = self.sign_with(backend)
                self.assertEqual(name, 'modules.coincurve_compat' if backend == 'compat' else 'coincurve')
                self.assertEqual((txid, sig, sig2), self.TX)
                self.assertEqual(pk, '03e74366a0e299113121209b19de37dcba6cfb430cdd89cd8c4e18e03a6c6cf0eb')
                self.assertEqual(addr, ME)

    def test_compat_keys(self):
        from modules import coincurve_compat as cc
        g = cc.PrivateKey.from_hex('00' * 31 + '01').public_key
        self.assertEqual(g.format().hex(), '0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798')
        self.assertEqual(cc.PublicKey(g.format(compressed=False)).format(), g.format())
        for bad in ('00' * 32, 'ff' * 32, '01' * 31):
            with self.assertRaises(ValueError):
                cc.PrivateKey.from_hex(bad)
        for bad in (b'', b'\x02' + b'\x00' * 31, b'\x05' + b'\x01' * 32, b'\x04' + b'\x01' * 64):
            with self.assertRaises(ValueError):
                cc.PublicKey(bad)
        coincurve = native_coincurve()
        if coincurve is None:
            return
        for _ in range(100):
            k = os.urandom(32)
            if 0 < int.from_bytes(k, 'big') < cc.n:
                self.assertEqual(cc.PrivateKey(k).public_key.format(), coincurve.PrivateKey(k).public_key.format())


class Ripemd160Tests(unittest.TestCase):
    VECTORS = {b'': '9c1185a5c5e9fc54612808977ee8f548b2258d31', b'abc': '8eb208f7e05d987a9b044a8e98c6b087f15a0bfc',
               b'message digest': '5d0689ef49d2fae572b881b123a85ffa21595f36',
               b'1234567890' * 8: '9b752e45573d4b39f4dbd3323cab82bf63326bfb',
               b'a' * 1000000: '52783243c1697bdbe16d37f97f68f08325dc1528'}

    def test_vectors(self):
        from modules.ripemd160 import RIPEMD160, ripemd160
        for msg, want in self.VECTORS.items():
            self.assertEqual(ripemd160(msg).hex(), want)
        h = RIPEMD160(b'message ')
        h.update(b'digest')
        self.assertEqual(h.hexdigest(), self.VECTORS[b'message digest'])

    def test_addresses_without_system_ripemd160(self):
        script = ('import hashlib, sys\n'
                  'orig = hashlib.new\n'
                  'def refuse(name, *a, **k):\n'
                  '    if name.lower() == "ripemd160": raise ValueError("unsupported hash type " + name)\n'
                  '    return orig(name, *a, **k)\n'
                  'hashlib.new = refuse\n'
                  'sys.path.insert(0, sys.argv[1]); import drain; drain.get_client("x")\n'
                  'print(drain.address_from_passphrase(sys.argv[2], network_version=23))\n')
        out = subprocess.run([sys.executable, '-W', 'ignore', '-c', script, str(ROOT), PASS],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), ME)


class LockTest(unittest.TestCase):
    def test_second_run_is_refused(self):
        import fcntl
        with open(ROOT / '.drain.lock', 'w') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with tempfile.NamedTemporaryFile('w', suffix='.ini', delete=False) as cfg:
                cfg.write(f'[static]\nconvert_address = {ME}\naddress_to = {OUT}\n[flashift]\napi_key = k\n')
            self.addCleanup(os.unlink, cfg.name)
            out = subprocess.run([sys.executable, '-W', 'ignore', str(ROOT / 'drain.py'), '--config', cfg.name],
                                 capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 1)
        self.assertIn('Another drain run is in progress', out.stderr)


if __name__ == '__main__':
    unittest.main()
