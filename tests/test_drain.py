"""Offline tests for drain: fake ARK node, faked Flashift/Trocador HTTP. Run: .venv/bin/python -m unittest -v"""
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
from modules.exchange import Exchange, FlashiftClient, Order, Quote, TrocadorClient, fmt, is_ark_address  # noqa: E402

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
        'convert_from': 'ark', 'convert_address': ME, 'convert_to': 'usdc', 'address_to': OUT,
        'providers': ['Flashift', 'Trocador'], 'fixed': False, 'fixed_amt': D(0), 'test_amt': D(750),
        'min_rate': D(0), 'keep': D(0), 'exchange_log': os.devnull,
        'flashift': {'api_key': 'fk'}, 'trocador': {'api_key': 'tk'},
    }
    cfg.update(over)
    return cfg


class Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        if isinstance(self._body, str):
            raise ValueError('not json')
        return self._body


class FakeNode:
    """Just enough of arkecosystem-client's ArkClient."""
    def __init__(self, balance=1000 * 10**8 + 1_005_000, nonce=7, attributes=None, pool=None, accept=True,
                 fees=None, wallet_error=None, broadcast_error=None):
        self.broadcasts = []
        outer = self

        class Node:
            def configuration(self):
                if fees == 'error':
                    raise RuntimeError('node down')
                return {'data': {'transactionPool': {'dynamicFees': fees or {
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


FL_QUOTES = {'message': 'OK', 'data': [
    {'provider_name': 'Changelly', 'exchange_type': 'floating', 'amount': '127.9', 'min_amount': '232',
     'max_amount': '136786', 'tags': ['Recommended', 'AI Best Rate']},
    {'provider_name': 'Changelly', 'exchange_type': 'fixed', 'amount': '126.4', 'min_amount': '387',
     'max_amount': '136786', 'tags': []}]}
TR_RATE = {'trade_id': 'R1', 'provider': 'SimpleSwap', 'fixed': 'False', 'amount_to': '128.5',
           'quotes': {'quotes': [{'provider': 'SimpleSwap', 'amount_to': '128.5', 'kycrating': 'B'},
                                 {'provider': 'MoneroBridge', 'amount_to': '126.9', 'kycrating': 'A'}]}}


def fl_info(amount='1000', address_from=DEP, address_to=OUT, extra=None):
    return Resp(200, {'symbol_from': 'ark', 'network_from': 'ark', 'symbol_to': 'usdc', 'network_to': 'pol',
                      'amount_from': amount, 'amount_to': '127.9', 'address_from': address_from,
                      'address_to': address_to, 'status': 'waiting', 'extra_from': extra})


def tr_trade(**over):
    body = {'trade_id': 'T1', 'provider': 'SimpleSwap', 'ticker_to': 'usdc', 'network_to': 'Polygon',
            'amount_from': '1000', 'amount_to': '128.5', 'address_provider': DEP,
            'address_provider_memo': None, 'address_user': OUT}
    body.update(over)
    return Resp(200, body)


def default_routes(**over):
    routes = {'getEstimatedAmount': Resp(200, FL_QUOTES),
              'createTransaction': Resp(200, {'status': 'OK', 'exchange_id': 'Changelly_floating_x'}),
              'getTransactionInfo': fl_info(),
              'new_rate': Resp(200, TR_RATE),
              'new_trade': tr_trade()}
    routes.update(over)
    return routes


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
        return f'[static]\n{lines}\n[flashift]\napi_key = 23|abc%def\nnetwork_to = pol\n[trocador]\nnetwork_to = MATIC\n'

    def test_valid_config_and_defaults(self):
        cfg = drain.get_config(self.write(self.base()))
        self.assertTrue(cfg['test'], 'test mode must be the default')
        self.assertEqual(cfg['flashift']['api_key'], '23|abc%def', '% must not break parsing')
        self.assertEqual(cfg['providers'], ['Flashift', 'Trocador'])
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
            'not ark': self.base(convert_from='btc'),
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

class PickTests(unittest.TestCase):
    def test_flashift_pick(self):
        pick = FlashiftClient.pick
        self.assertEqual(pick(FL_QUOTES['data'], D(1000), 'floating')['amount'], '127.9')
        self.assertEqual(pick(FL_QUOTES['data'], D(1000), 'fixed')['amount'], '126.4')
        self.assertIsNone(pick(FL_QUOTES['data'], D(300), 'fixed'), 'below fixed minimum')
        self.assertIsNone(pick(FL_QUOTES['data'], D(200000), 'any'), 'above maximum')
        garbage = ['str', None, 5, {}, {'provider_name': 'X', 'exchange_type': 'floating', 'amount': 'NaN'},
                   {'provider_name': 'Y', 'exchange_type': 'floating', 'amount': '-3'},
                   {'provider_name': 'Z', 'exchange_type': 'floating', 'amount': '0'},
                   {'exchange_type': 'floating', 'amount': '500'},
                   {'provider_name': 'W', 'exchange_type': 'floating', 'amount': '50', 'min_amount': 'abc'}]
        self.assertIsNone(pick(garbage, D(1000), 'floating'))
        ok = garbage + [{'provider_name': 'Ok', 'exchange_type': 'floating', 'amount': '1', 'max_amount': None,
                         'tags': None}]
        self.assertEqual(pick(ok, D(1000), 'floating')['provider_name'], 'Ok')

    def test_flashift_tag_beats_higher_untagged(self):
        qs = [{'provider_name': 'A', 'exchange_type': 'floating', 'amount': '130', 'tags': []},
              {'provider_name': 'B', 'exchange_type': 'floating', 'amount': '120', 'tags': ['best rate']}]
        self.assertEqual(FlashiftClient.pick(qs, D(1), 'floating')['provider_name'], 'B')

    def test_trocador_pick(self):
        pick = TrocadorClient.pick
        self.assertEqual(pick(TR_RATE, 'floating')['provider'], 'SimpleSwap')
        self.assertIsNone(pick(TR_RATE, 'fixed'))
        mixed = dict(TR_RATE, quotes=[{'provider': 'F', 'amount_to': '140', 'fixed': True},
                                      {'provider': 'G', 'amount_to': 'garbage'}, 'junk', None])
        self.assertEqual(pick(mixed, 'fixed')['provider'], 'F')
        self.assertEqual(pick(mixed, 'any')['provider'], 'F')
        self.assertIsNone(pick({'provider': None, 'quotes': None}, 'any'))
        self.assertIsNone(pick({'provider': 'X', 'amount_to': '0', 'quotes': []}, 'any'))


# ---------------------------------------------------------------- exchange over faked HTTP

class ExchangeTests(HTTPTest):
    def exchange(self, **over):
        return Exchange(make_config(**over))

    def test_best_payout_wins(self):
        self.use(default_routes())
        order = self.exchange().create(D(1000), ME)
        self.assertEqual((order.service, order.deposit_address), ('Trocador', DEP))
        self.assertEqual(self.http.count('createTransaction'), 0, 'no second order once one succeeded')
        trade = [c for c in self.http.calls if c[1] == 'new_trade'][0][2]['params']
        self.assertEqual((trade['refund'], trade['address'], trade['amount_from'], trade['fixed'], trade['id']),
                         (ME, OUT, '1000', 'False', 'R1'))
        self.assertEqual(trade['network_to'], 'MATIC')

    def test_fallback_when_best_fails(self):
        for name, bad in {'http error': Resp(500, {'error': 'boom'}), 'html': Resp(200, '<html>'),
                          'timeout': ex.requests.Timeout('slow'), 'bad memo': tr_trade(address_provider_memo='123'),
                          'bad checksum': tr_trade(address_provider=DEP_BADSUM),
                          'own address': tr_trade(address_provider=ME),
                          'evm deposit': tr_trade(address_provider=OUT),
                          'payout elsewhere': tr_trade(address_user='0x' + '2' * 40),
                          'wrong amount': tr_trade(amount_from='999'),
                          'wrong coin': tr_trade(ticker_to='usdt'),
                          'payout far below quote': tr_trade(amount_to='100')}.items():
            with self.subTest(name):
                self.use(default_routes(new_trade=bad))
                order = self.exchange().create(D(1000), ME)
                self.assertEqual(order.service, 'Flashift')
                self.assertEqual(self.http.count('new_trade'), 1, 'order creation is never retried')

    def test_payout_address_case_insensitive(self):
        self.use(default_routes(new_trade=tr_trade(address_user=OUT.upper().replace('0X', '0x'))))
        self.assertEqual(self.exchange().create(D(1000), ME).service, 'Trocador')

    def test_everything_fails(self):
        self.use(default_routes(new_trade=Resp(500, {}), createTransaction=Resp(200, {'status': 'Error'})))
        self.assertIsNone(self.exchange().create(D(1000), ME))

    def test_flashift_no_deposit_address(self):
        self.use(default_routes(new_rate=Resp(200, {'error': 'Pair not available'}),
                                getTransactionInfo=Resp(200, {'status': 'Error'})))
        self.assertIsNone(self.exchange().create(D(1000), ME))
        self.assertEqual(self.http.count('getTransactionInfo'), 5)

    def test_quote_retries(self):
        attempts = iter([Resp(429, 'slow down'), Resp(502, '<html>'), Resp(200, FL_QUOTES)])
        self.use(default_routes(getEstimatedAmount=lambda kw: next(attempts)))
        q = self.exchange(providers=['Flashift']).quotes(D(1000))
        self.assertEqual(q[0][0].amount_to, D('127.9'))

    def test_quote_gives_up(self):
        self.use(default_routes(getEstimatedAmount=ex.requests.ConnectionError('down'),
                                new_rate=Resp(200, {'error': 'Minimum amount is 250'})))
        self.assertEqual(self.exchange().quotes(D(1000)), [])
        self.assertEqual(self.http.count('getEstimatedAmount'), 3)

    def test_min_rate(self):
        self.use(default_routes())
        self.assertEqual([q.service for q, _ in self.exchange(min_rate=D('0.128')).quotes(D(1000))], ['Trocador'])
        self.assertEqual(self.exchange(min_rate=D('0.2')).quotes(D(1000)), [])

    def test_amount_strings(self):
        self.use(default_routes())
        self.exchange().quotes(D('0.29'))
        sent = [c[2]['params'] for c in self.http.calls]
        self.assertEqual(sent[0]['amount'], '0.29')
        self.assertEqual(sent[1]['amount_from'], '0.29')

    def test_provider_list(self):
        with self.assertRaises(ValueError):
            Exchange(make_config(providers=['ChangeNow']))
        with self.assertRaises(ValueError):
            Exchange(make_config(flashift={}, trocador={'api_key': '  '}))
        self.assertEqual(len(Exchange(make_config(providers=['flashift', 'Flashift', 'TROCADOR'])).clients), 2)
        with mock.patch.dict(os.environ, {'TROCADOR_API_KEY': 'env'}):
            self.assertEqual(len(Exchange(make_config(trocador={})).clients), 2)

    def test_log_write_failure_is_harmless(self):
        self.use(default_routes())
        self.assertIsNotNone(self.exchange(exchange_log='/nonexistent/dir/log.txt').create(D(1000), ME))


# ---------------------------------------------------------------- full runs

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
        self.assertTrue(rows[1].endswith(tx['id']))

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
                self.assertEqual((self.http.count('new_trade'), self.http.count('createTransaction')), (0, 0))
                self.assertEqual(node.broadcasts, [])

    def test_others_in_pool_are_fine(self):
        self.use(default_routes())
        self.assertEqual(self.run_drain(FakeNode(pool=[{'senderPublicKey': '02' + 'ab' * 32}])), 0)

    def test_no_order_no_send(self):
        self.use(default_routes(new_trade=Resp(500, {}), getEstimatedAmount=Resp(200, {'message': 'Pair is not valid'})))
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
                self.assertEqual((self.http.count('new_trade'), self.http.count('createTransaction')), (0, 0))
                self.assertEqual(node.broadcasts, [])
        amounts = [c[2]['params']['amount'] for c in self.http.calls if c[1] == 'getEstimatedAmount']
        self.assertEqual(amounts, ['750'], 'node down quotes test_amt')

    def test_test_mode_still_checks_a_given_passphrase(self):
        self.use(default_routes())
        with self.assertRaises(drain.Abort):
            self.run_drain(FakeNode(), test=True, passphrase='wrong words')

    def test_test_mode_quotes_real_amount(self):
        self.use(default_routes())
        self.run_drain(FakeNode(balance=500 * 10**8 + 1_005_000), test=True)
        self.assertEqual(self.http.calls[0][2]['params']['amount'], '500')


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
