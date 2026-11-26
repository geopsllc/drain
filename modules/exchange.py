"""Swaps for drain through the ARK Exchange Worker (https://xc.geops.net by default).

The Worker holds the Flashift and Trocador API keys, so drain needs none. It only quotes and creates
orders; it never sees a passphrase or moves funds. drain still checks every order itself before sending
anything to its deposit address (Exchange.check_order), signs the ARK transfer locally, and links to the
exchange's own order page for status.

Amounts are Decimal whole coins (e.g. Decimal('300.1234')).
"""
import logging
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation

import base58
import requests

ATOMIC = 100_000_000
ARK_PUBKEY_HASH = 23  # ARK mainnet address version byte ('A...')
EVM_ADDRESS = re.compile(r'^0x[0-9a-fA-F]{40}$')
DEFAULT_WORKER = 'https://xc.geops.net'
# Payout networks and coins the Worker offers for ARK (all EVM chains, so address_to is a 0x address).
# Neither aggregator has ARK -> USDT on Base.
NETWORKS = {
    'polygon': ('usdc', 'usdt'),
    'base': ('usdc',),
    'ethereum': ('usdc', 'usdt'),
    'arbitrum': ('usdc', 'usdt'),
    'optimism': ('usdc', 'usdt'),
    'bsc': ('usdc', 'usdt'),
}
SERVICES = ('Flashift', 'Trocador')
MAX_ORDER_ATTEMPTS = 3   # each failed order costs Worker and aggregator rate limit
MAX_WAIT = 90            # longest Retry-After drain waits out for a quote
USER_AGENT = 'drain/2.0'
log = logging.getLogger(__name__)


class WorkerError(RuntimeError):
    def __init__(self, message, status=None, retry_after=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def is_ark_address(address):
    """Base58check-valid ARK mainnet address (checksum and version byte)."""
    if not isinstance(address, str) or len(address) != 34:
        return False
    try:
        return base58.b58decode_check(address)[0] == ARK_PUBKEY_HASH
    except ValueError:
        return False


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


@dataclass
class Quote:
    service: str          # Flashift / Trocador
    provider: str         # the exchange behind it, e.g. Changelly
    rate_type: str        # floating / fixed
    amount_to: Decimal
    network_to: str = ''  # the service's code for the payout network, e.g. pol / MATIC
    ref: dict = field(default_factory=dict)  # handed back to the Worker to create the order
    kyc: str = ''         # Trocador's KYC grade (A-D), when given

    def as_request(self):
        return {'service': self.service, 'provider': self.provider, 'rateType': self.rate_type, 'ref': self.ref}


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
    provider_order_id: str = ''  # the order's id at the exchange itself

    def tracking(self):
        """(label, link) where the order's status can be followed: the exchange's own order page when
        known, otherwise the aggregator's."""
        url = EXCHANGE_TRACKING.get(norm(self.provider))
        if url and self.provider_order_id:
            return [(self.provider, url.format(id=quote_url(self.provider_order_id)))]
        if self.service == 'Trocador':
            return [('Trocador', f'https://trocador.app/checkout/{quote_url(self.order_id)}')]
        if self.service == 'Flashift':  # its app has no per-order URL
            return [('Flashift', f'https://exchange.flashift.app (History > Track Your Swap > {self.order_id})')]
        return []

    def tracking_text(self):
        return '; '.join(f'{label}: {link}' for label, link in self.tracking())


# Order pages on the exchanges themselves; each checked with a real (unfunded) order.
EXCHANGE_TRACKING = {
    'changelly': 'https://changelly.com/track/{id}',
    'changenow': 'https://changenow.io/exchange/txs/{id}',
    'simpleswap': 'https://simpleswap.io/exchange?id={id}',
    'stealthex': 'https://stealthex.io/exchange/?id={id}',
}


def norm(name):
    return re.sub(r'[^a-z0-9]', '', str(name or '').lower())


def quote_url(value):
    return urllib.parse.quote(str(value), safe='')


class WorkerClient:
    """POST JSON to the Worker. Quotes are retried once (after its Retry-After, if short); orders never are,
    since a retry after a timeout could create a second order."""

    def __init__(self, url):
        self.url = url.rstrip('/')

    def post(self, path, body, retry):
        for attempt in (1, 2):
            try:
                r = requests.post(f'{self.url}/{path}', json=body, timeout=(15, 180),
                                  headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'})
            except requests.RequestException as e:
                err = WorkerError(f'{path}: cannot reach the exchange worker: {e}')
            else:
                try:
                    data = r.json()
                except ValueError:
                    data = None
                if r.status_code == 200 and isinstance(data, dict):
                    return data
                message = data.get('error') if isinstance(data, dict) else None
                retry_after = to_decimal(data.get('retryAfter')) if isinstance(data, dict) else None
                if retry_after is None:
                    retry_after = to_decimal(r.headers.get('Retry-After'))
                err = WorkerError(f'{path}: {message or f"HTTP {r.status_code}: {r.text[:200]!r}"}',
                                  r.status_code, retry_after)
                if r.status_code < 500 and r.status_code != 429:
                    raise err  # a bad request won't get better
            if not retry or attempt == 2:
                raise err
            wait = 3 if err.status != 429 else err.retry_after
            if wait is None or wait > MAX_WAIT:
                raise err
            log.warning('%s; retrying in %ss', err, fmt(wait))
            time.sleep(float(wait))


class Exchange:
    def __init__(self, config):
        self.config = config
        self.worker = WorkerClient(config['worker'])
        self.coin = config['convert_to']
        self.network = config['network']
        self.min_rate = to_decimal(config.get('min_rate') or 0) or Decimal(0)
        wanted = {norm(p) for p in config.get('providers') or SERVICES}
        unknown = wanted - {norm(s) for s in SERVICES}
        if unknown:
            raise ValueError(f'Unknown provider(s) {", ".join(sorted(unknown))}; use Flashift and/or Trocador')
        self.services = [s for s in SERVICES if norm(s) in wanted]

    def quotes(self, amount):
        """Usable quotes from the Worker in its order (each service's pick by payout, then the rest).
        Problems are logged; an empty list means nothing usable."""
        try:
            data = self.worker.post('quote', {'amount': fmt(amount), 'network': self.network, 'coin': self.coin},
                                    retry=True)
        except WorkerError as e:
            log.warning('Quote failed: %s', e)
            return []
        for err in data.get('errors') or []:
            if isinstance(err, dict) and err.get('service') in self.services:
                log.warning('%s quote failed: %s', err.get('service'), err.get('message'))
        found = []
        for q in data.get('quotes') or []:
            quote = self.parse_quote(q)
            if quote is None or quote.service not in self.services:
                continue
            rate = quote.amount_to / amount
            log.info('%s quote: %s %s %s ark -> %s %s (%.6f per ark)%s', quote.service, quote.provider,
                     quote.rate_type, fmt(amount), fmt(quote.amount_to), self.coin, rate,
                     f', KYC {quote.kyc}' if quote.kyc else '')
            if rate < self.min_rate:
                log.warning('%s via %s ignored: rate %.6f below min_rate %s', quote.service, quote.provider,
                            rate, fmt(self.min_rate))
                continue
            found.append(quote)
        return found

    @staticmethod
    def parse_quote(q):
        if not isinstance(q, dict) or q.get('service') not in SERVICES or not q.get('provider'):
            return None
        amount_to = to_decimal(q.get('amountTo'))
        if amount_to is None or amount_to <= 0 or q.get('rateType') not in ('fixed', 'floating'):
            return None
        ref = q.get('ref') if isinstance(q.get('ref'), dict) else {}
        return Quote(q['service'], str(q['provider']), q['rateType'], amount_to, str(q.get('networkTo') or ''),
                     ref, str(q.get('kyc') or ''))

    @staticmethod
    def parse_order(o):
        if not isinstance(o, dict):
            raise RuntimeError(f'unexpected order response: {o!r}')
        s = lambda k: '' if o.get(k) is None else str(o.get(k))
        return Order(s('service'), s('provider'), s('orderId'), s('depositAddress'), s('depositMemo'),
                     s('payoutAddress'), to_decimal(o.get('amountTo')), o.get('amountFrom'), s('symbolTo'),
                     s('networkTo'), s('providerOrderId'))

    def check_order(self, order, amount, quote):
        """drain sends the whole amount to the deposit address, so be strict about what we accept."""
        if not order.order_id or order.order_id == 'None':
            raise RuntimeError('order has no id')
        if order.service and order.service != quote.service:
            raise RuntimeError(f'order came from {order.service}, not {quote.service}')
        if not is_ark_address(order.deposit_address):
            raise RuntimeError(f'deposit address is not a valid ARK address: {order.deposit_address!r}')
        if order.deposit_address == self.config['convert_address']:
            raise RuntimeError('deposit address is our own address')
        if order.deposit_memo and str(order.deposit_memo).strip() not in ('0', 'None', 'null'):
            raise RuntimeError(f'deposit needs a memo ({order.deposit_memo!r}), which drain does not send')
        if str(order.payout_address).strip().lower() != self.config['address_to'].strip().lower():
            raise RuntimeError(f'order pays out to {order.payout_address!r}, not address_to')
        if order.amount_from not in (None, ''):
            reported = to_decimal(order.amount_from)
            if reported is None or abs(reported - amount) > Decimal('0.0001'):
                raise RuntimeError(f'order is for {order.amount_from} ark, not {fmt(amount)}')
        if str(order.symbol_to).lower() != self.coin:
            raise RuntimeError(f'order pays out {order.symbol_to or "an unknown coin"}, not {self.coin}')
        if not quote.network_to or str(order.network_to).lower() != quote.network_to.lower():
            raise RuntimeError(f'order pays out on {order.network_to or "an unknown network"}, '
                               f'not {quote.network_to or self.network}')
        if order.amount_to is None or order.amount_to < quote.amount_to * Decimal('0.97'):
            raise RuntimeError(f'order pays ~{order.amount_to}, more than 3% below the quote of {fmt(quote.amount_to)}')

    def create(self, amount, refund_address, quotes=None):
        """Create an order for `amount` (Decimal whole coins). Returns the checked Order, or None."""
        quotes = self.quotes(amount) if quotes is None else quotes
        for quote in quotes[:MAX_ORDER_ATTEMPTS]:
            body = {'amount': fmt(amount), 'network': self.network, 'coin': self.coin,
                    'quote': quote.as_request(), 'payoutAddress': self.config['address_to'],
                    'refundAddress': refund_address}
            try:
                order = self.parse_order(self.worker.post('order', body, retry=False).get('order'))
                self.check_order(order, amount, quote)
            except WorkerError as e:
                log.error('%s via %s failed: %s', quote.service, quote.provider, e)
                if e.status == 429:
                    break  # the next attempt would be refused too
                continue
            except Exception as e:
                log.error('%s via %s failed: %s', quote.service, quote.provider, e)
                continue
            log.info('Order %s (%s via %s): send %s ark to %s for ~%s %s', order.order_id, order.service,
                     order.provider, fmt(amount), order.deposit_address, fmt(order.amount_to), self.coin)
            self.record(order, amount, 'created')
            return order
        log.error('No usable order could be created')
        return None

    def record(self, order, amount, status, txid=''):
        try:
            with open(self.config['exchange_log'], 'a') as f:
                f.write(','.join([datetime.now().isoformat(timespec='seconds'), status, order.service,
                                  order.provider, order.order_id, fmt(amount), order.deposit_address,
                                  fmt(order.amount_to), txid, order.tracking_text().replace(',', ' ')]) + '\n')
        except OSError as e:
            log.warning('Could not write %s: %s', self.config['exchange_log'], e)
