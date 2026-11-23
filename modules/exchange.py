"""Swap providers for drain: Flashift and Trocador, called directly with local API keys.

Each client turns an amount into a Quote and a Quote into an Order. Exchange asks every
configured client for a quote, tries them best payout first, and only hands back an order
whose deposit address passed the safety checks in Exchange.check_order.

Amounts are Decimal whole coins (e.g. Decimal('300.1234')).
"""
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation

import base58
import requests

ATOMIC = 100_000_000
D0, DINF = Decimal(0), Decimal('Infinity')
ARK_PUBKEY_HASH = 23  # ARK mainnet address version byte ('A...')
EVM_ADDRESS = re.compile(r'^0x[0-9a-fA-F]{40}$')
EVM_NETWORKS = {'pol', 'polygon', 'matic', 'eth', 'erc20', 'ethereum', 'bsc', 'bep20', 'base',
                'arbitrum', 'arb', 'optimism', 'op', 'avaxc', 'cchain'}
log = logging.getLogger(__name__)


def is_ark_address(address):
    """Base58check-valid ARK mainnet address (checksum and version byte)."""
    if not isinstance(address, str) or len(address) != 34:
        return False
    try:
        return base58.b58decode_check(address)[0] == ARK_PUBKEY_HASH
    except ValueError:
        return False


def as_bool(value):
    return str(value).strip().lower() in ('true', '1', 'yes', 'y')


def to_decimal(value):
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() else None


def fmt(amount):
    """Plain decimal string without exponent or trailing zeros: 300.1000 -> '300.1'."""
    s = format(Decimal(amount), 'f')
    return s.rstrip('0').rstrip('.') if '.' in s else s


def _json(r):
    try:
        return r.json()
    except ValueError:
        return {'error': f'non-JSON response ({r.status_code}): {r.text[:200]!r}'}


def get_with_retry(url, attempts=3, **kwargs):
    """GET for read-only calls: retries connection errors, 429 and 5xx with backoff."""
    for i in range(attempts):
        try:
            r = requests.get(url, **kwargs)
            if r.status_code != 429 and r.status_code < 500:
                return r
            err = f'HTTP {r.status_code}'
        except requests.RequestException as e:
            err = str(e)
            r = None
        if i < attempts - 1:
            time.sleep(2 * (i + 1))
    if r is not None:
        return r
    raise RuntimeError(f'{url}: {err}')


@dataclass
class Quote:
    service: str          # Flashift / Trocador
    provider: str         # the exchange behind it, e.g. Changelly
    rate_type: str        # floating / fixed
    amount_to: Decimal
    ref: dict = field(default_factory=dict)  # whatever the service needs to create the order


@dataclass
class Order:
    service: str
    provider: str
    order_id: str
    deposit_address: str
    deposit_memo: str
    payout_address: str
    amount_to: Decimal
    amount_from: object = None   # as reported by the service, if it does
    symbol_to: str = ''
    network_to: str = ''


class FlashiftClient:
    """https://docs.flashift.app - quotes from many exchanges, provider picked from its tags."""
    name = 'Flashift'
    API = 'https://interfacev2.flashift.app/api/dev/v2'
    TAG_PRIORITY = ('Recommended', 'AI Best Rate', 'Best Rate')

    def __init__(self, settings, pair):
        self.key = (os.environ.get('FLASHIFT_API_KEY') or settings.get('api_key') or '').strip()
        self.rate = (settings.get('rate') or 'floating').strip().lower()
        self.pair = {'symbol_from': pair['from'], 'network_from': settings.get('network_from') or pair['from'],
                     'symbol_to': pair['to'], 'network_to': settings.get('network_to') or 'pol'}
        self.poll_delay = 3

    @property
    def enabled(self):
        return bool(self.key)

    def _headers(self):
        key = self.key if self.key.startswith('Bearer ') else 'Bearer ' + self.key
        return {'Authorization': key, 'Accept': 'application/json'}

    @classmethod
    def pick(cls, quotes, amount, rate):
        def limit(value, default):
            # missing -> no limit; present but unreadable -> reject the quote
            return default if value in (None, '') else to_decimal(value)

        def ok(q):
            out, lo, hi = to_decimal(q.get('amount')), limit(q.get('min_amount'), D0), limit(q.get('max_amount'), DINF)
            return (out is not None and out > 0 and q.get('provider_name') and lo is not None and hi is not None
                    and lo <= amount <= hi and (rate == 'any' or q.get('exchange_type') == rate))
        usable = [q for q in quotes if isinstance(q, dict) and ok(q)]
        if not usable:
            return None
        best = lambda qs: max(qs, key=lambda q: to_decimal(q['amount']))
        for tag in cls.TAG_PRIORITY:
            tagged = [q for q in usable if tag.lower() in (str(t).lower() for t in q.get('tags') or [])]
            if tagged:
                return best(tagged)
        return best(usable)

    def quote(self, amount):
        r = get_with_retry(f'{self.API}/getEstimatedAmount', headers=self._headers(),
                           params=dict(self.pair, amount=fmt(amount)), timeout=90)
        body = _json(r)
        if r.status_code != 200 or not isinstance(body, dict) or body.get('message') != 'OK':
            msg = body.get('message') or body.get('error') if isinstance(body, dict) else body
            raise RuntimeError(f'quote failed ({r.status_code}): {msg}')
        quotes = body.get('data') or []
        best = self.pick(quotes, amount, self.rate)
        if best is None:
            offered = [(q.get('provider_name'), q.get('exchange_type'), q.get('min_amount'), q.get('max_amount'))
                       for q in quotes if isinstance(q, dict)]
            raise RuntimeError(f'no {self.rate} quote covers {fmt(amount)} (offered: {offered})')
        return Quote(self.name, best['provider_name'], best['exchange_type'], to_decimal(best['amount']),
                     {'tags': best.get('tags')})

    def create(self, quote, amount, address_to, refund_address):
        # Flashift has no refund-address field; refunds are handled with the provider's support.
        # Not retried: a retry after a timeout could create a second order.
        body = dict(self.pair, provider_name=quote.provider, to_address=address_to, to_extra_id='',
                    amount=fmt(amount), fixed=(quote.rate_type == 'fixed'))
        r = requests.post(f'{self.API}/createTransaction', headers=self._headers(), json=body, timeout=90)
        created = _json(r)
        order_id = created.get('exchange_id') if isinstance(created, dict) else None
        if r.status_code != 200 or created.get('status') != 'OK' or not order_id:
            raise RuntimeError(f'createTransaction failed ({r.status_code}): {created}')
        # createTransaction only returns an id; the deposit address comes from getTransactionInfo
        info = {}
        for _ in range(5):
            time.sleep(self.poll_delay)
            try:
                r = get_with_retry(f'{self.API}/getTransactionInfo', headers=self._headers(),
                                   params={'exchange_id': order_id}, timeout=60)
                info = _json(r)
            except RuntimeError as e:
                info = {'error': str(e)}
            if r.status_code == 200 and isinstance(info, dict) and info.get('address_from'):
                break
        if not isinstance(info, dict) or not info.get('address_from'):
            raise RuntimeError(f'order {order_id} created but no deposit address returned: {info}')
        return Order(self.name, quote.provider, order_id, info.get('address_from') or '',
                     info.get('extra_from') or '', info.get('address_to') or '',
                     to_decimal(info.get('amount_to')) or quote.amount_to, info.get('amount_from'),
                     info.get('symbol_to') or '', info.get('network_to') or '')


class TrocadorClient:
    """https://trocador.app/docs - aggregator; new_rate returns a rate id plus quotes per exchange."""
    name = 'Trocador'
    API = 'https://api.trocador.app'

    def __init__(self, settings, pair):
        self.key = (os.environ.get('TROCADOR_API_KEY') or settings.get('api_key') or '').strip()
        self.rate = (settings.get('rate') or 'floating').strip().lower()
        self.min_kyc = (settings.get('min_kycrating') or '').strip().upper()
        self.pair = {'ticker_from': pair['from'], 'network_from': settings.get('network_from') or 'Mainnet',
                     'ticker_to': pair['to'], 'network_to': settings.get('network_to') or 'MATIC'}

    @property
    def enabled(self):
        return bool(self.key)

    def _request(self, method, params, retry):
        kwargs = dict(headers={'API-Key': self.key}, params=params, timeout=120)
        r = get_with_retry(f'{self.API}/{method}', **kwargs) if retry else requests.get(f'{self.API}/{method}', **kwargs)
        body = _json(r)
        if r.status_code != 200 or not isinstance(body, dict) or body.get('error'):
            raise RuntimeError(f'{method} failed ({r.status_code}): {body}')
        return body

    @staticmethod
    def quote_list(rate):
        quotes = rate.get('quotes') or []
        if isinstance(quotes, dict):
            quotes = quotes.get('quotes') or []
        return [q for q in quotes if isinstance(q, dict) and q.get('provider')
                and (to_decimal(q.get('amount_to')) or 0) > 0]

    @classmethod
    def pick(cls, rate, wanted):
        """Best quote for the wanted rate type; Trocador already sorts best first."""
        candidates = []
        if rate.get('provider') and (to_decimal(rate.get('amount_to')) or 0) > 0:
            candidates.append({'provider': rate['provider'], 'amount_to': rate['amount_to'], 'fixed': rate.get('fixed')})
        for q in cls.quote_list(rate):
            # quotes inherit the request's rate type unless they say otherwise
            candidates.append(dict(q, fixed=q.get('fixed', rate.get('fixed'))))
        if wanted != 'any':
            candidates = [c for c in candidates if as_bool(c.get('fixed')) == (wanted == 'fixed')]
        if not candidates:
            return None
        return max(candidates, key=lambda c: to_decimal(c['amount_to']))

    def quote(self, amount):
        params = dict(self.pair, amount_from=fmt(amount))
        if self.min_kyc:
            params['min_kycrating'] = self.min_kyc
        rate = self._request('new_rate', params, retry=True)
        best = self.pick(rate, self.rate)
        if best is None:
            raise RuntimeError(f'no {self.rate} quote (best was {rate.get("provider")} fixed={rate.get("fixed")})')
        rate_type = 'fixed' if as_bool(best.get('fixed')) else 'floating'
        return Quote(self.name, best['provider'], rate_type, to_decimal(best['amount_to']),
                     {'id': rate.get('trade_id'), 'kyc': best.get('kycrating')})

    def create(self, quote, amount, address_to, refund_address):
        params = dict(self.pair, id=quote.ref.get('id'), amount_from=fmt(amount), address=address_to,
                      refund=refund_address, provider=quote.provider,
                      fixed=str(quote.rate_type == 'fixed'))
        if self.min_kyc:
            params['min_kycrating'] = self.min_kyc
        trade = self._request('new_trade', params, retry=False)
        return Order(self.name, trade.get('provider') or quote.provider, str(trade.get('trade_id')),
                     trade.get('address_provider') or '', trade.get('address_provider_memo') or '',
                     trade.get('address_user') or '', to_decimal(trade.get('amount_to')) or quote.amount_to,
                     trade.get('amount_from'), trade.get('ticker_to') or '', trade.get('network_to') or '')


CLIENTS = {'flashift': FlashiftClient, 'trocador': TrocadorClient}


class Exchange:
    def __init__(self, config):
        self.config = config
        pair = {'from': config['convert_from'].lower(), 'to': config['convert_to'].lower()}
        self.min_rate = to_decimal(config.get('min_rate') or 0) or Decimal(0)
        self.clients = []
        seen = set()
        for name in config['providers']:
            cls = CLIENTS.get(name.lower())
            if cls is None:
                raise ValueError(f'Unknown provider {name!r}; use Flashift and/or Trocador')
            if cls in seen:
                continue
            seen.add(cls)
            client = cls(config.get(name.lower(), {}), pair)
            if client.enabled:
                self.clients.append(client)
            else:
                log.warning('%s skipped: no API key configured', cls.name)
        if not self.clients:
            raise ValueError('No provider has an API key; set one in config.ini or the environment')

    def quotes(self, amount):
        """Best quote from each configured service, best payout first. Errors are logged, not raised."""
        found = []
        for client in self.clients:
            try:
                q = client.quote(amount)
            except Exception as e:
                log.warning('%s quote failed: %s', client.name, e)
                continue
            rate = q.amount_to / amount
            log.info('%s quote: %s %s %s %s -> %s %s (%.6f per %s)', client.name, q.provider, q.rate_type,
                     fmt(amount), self.config['convert_from'], fmt(q.amount_to), self.config['convert_to'],
                     rate, self.config['convert_from'])
            if rate < self.min_rate:
                log.warning('%s quote ignored: rate %.6f below min_rate %s', client.name, rate, fmt(self.min_rate))
                continue
            found.append((q, client))
        return sorted(found, key=lambda qc: qc[0].amount_to, reverse=True)

    def check_order(self, order, amount, quote):
        """drain sends the whole amount to the deposit address, so be strict about what we accept."""
        if not is_ark_address(order.deposit_address):
            raise RuntimeError(f'deposit address is not a valid ARK address: {order.deposit_address!r}')
        if order.deposit_address == self.config['convert_address']:
            raise RuntimeError('deposit address is our own address')
        if order.deposit_memo and str(order.deposit_memo).strip() not in ('0', 'None', 'null'):
            raise RuntimeError(f'deposit needs a memo ({order.deposit_memo!r}), which drain does not send')
        if order.payout_address.strip().lower() != self.config['address_to'].strip().lower():
            raise RuntimeError(f'order pays out to {order.payout_address!r}, not address_to')
        if order.amount_from not in (None, ''):
            reported = to_decimal(order.amount_from)
            if reported is None or abs(reported - amount) > Decimal('0.0001'):
                raise RuntimeError(f'order is for {order.amount_from} {self.config["convert_from"]}, not {fmt(amount)}')
        if order.symbol_to and order.symbol_to.lower() != self.config['convert_to'].lower():
            raise RuntimeError(f'order pays out {order.symbol_to}, not {self.config["convert_to"]}')
        if order.amount_to is not None and order.amount_to < quote.amount_to * Decimal('0.97'):
            raise RuntimeError(f'order pays ~{fmt(order.amount_to)}, more than 3% below the quote of {fmt(quote.amount_to)}')

    def create(self, amount, refund_address):
        """Create an order for `amount` (Decimal whole coins). Returns the checked Order, or None."""
        for quote, client in self.quotes(amount):
            try:
                order = client.create(quote, amount, self.config['address_to'], refund_address)
                self.check_order(order, amount, quote)
            except Exception as e:
                log.error('%s via %s failed: %s', client.name, quote.provider, e)
                continue
            log.info('Order %s (%s via %s): send %s %s to %s for ~%s %s', order.order_id, order.service,
                     order.provider, fmt(amount), self.config['convert_from'], order.deposit_address,
                     fmt(order.amount_to), self.config['convert_to'])
            self.record(order, amount, 'created')
            return order
        log.error('No provider produced a usable order')
        return None

    def record(self, order, amount, status, txid=''):
        try:
            with open(self.config['exchange_log'], 'a') as f:
                f.write(','.join([datetime.now().isoformat(timespec='seconds'), status, order.service,
                                  order.provider, order.order_id, fmt(amount), order.deposit_address,
                                  fmt(order.amount_to), txid]) + '\n')
        except OSError as e:
            log.warning('Could not write %s: %s', self.config['exchange_log'], e)
