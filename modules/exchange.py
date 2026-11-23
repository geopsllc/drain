"""Swap providers for drain: Flashift and Trocador, called directly with local API keys.

Each client turns an amount into a Quote and a Quote into an Order. Exchange asks every
configured client for a quote, tries them best payout first, and only hands back a deposit
address that passed the safety checks in Exchange._check_order.
"""
import logging
import math
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime

import requests

ATOMIC = 100_000_000
ARK_ADDRESS = re.compile(r'^A[1-9A-HJ-NP-Za-km-z]{33}$')
log = logging.getLogger(__name__)


def truncate(value, decimals):
    return math.floor(value * 10 ** decimals) / 10 ** decimals


def as_bool(value):
    return str(value).strip().lower() in ('true', '1', 'yes', 'y')


@dataclass
class Quote:
    service: str          # Flashift / Trocador
    provider: str         # the exchange behind it, e.g. Changelly
    rate_type: str        # floating / fixed
    amount_to: float
    ref: dict = field(default_factory=dict)  # whatever the service needs to create the order


@dataclass
class Order:
    service: str
    provider: str
    order_id: str
    deposit_address: str
    deposit_memo: str
    payout_address: str
    amount_to: float


class FlashiftClient:
    """https://docs.flashift.app - quotes from many exchanges, provider picked from its tags."""
    name = 'Flashift'
    API = 'https://interfacev2.flashift.app/api/dev/v2'
    TAG_PRIORITY = ('Recommended', 'AI Best Rate', 'Best Rate')

    def __init__(self, settings, pair):
        self.key = os.environ.get('FLASHIFT_API_KEY') or settings.get('api_key', '')
        self.rate = settings.get('rate', 'floating').lower()
        self.pair = {'symbol_from': pair['from'], 'network_from': settings.get('network_from', pair['from']),
                     'symbol_to': pair['to'], 'network_to': settings.get('network_to', 'pol')}

    @property
    def enabled(self):
        return bool(self.key)

    def _headers(self):
        key = self.key if self.key.startswith('Bearer ') else 'Bearer ' + self.key
        return {'Authorization': key, 'Accept': 'application/json'}

    @classmethod
    def pick(cls, quotes, amount, rate):
        def in_range(q):
            lo = float(q['min_amount']) if q.get('min_amount') else 0.0
            hi = float(q['max_amount']) if q.get('max_amount') else float('inf')
            return lo <= amount <= hi
        usable = [q for q in quotes if (rate == 'any' or q.get('exchange_type') == rate) and in_range(q)]
        if not usable:
            return None
        for tag in cls.TAG_PRIORITY:
            tagged = [q for q in usable if tag.lower() in (t.lower() for t in q.get('tags') or [])]
            if tagged:
                return max(tagged, key=lambda q: float(q['amount']))
        return max(usable, key=lambda q: float(q['amount']))

    def quote(self, amount):
        r = requests.get(f'{self.API}/getEstimatedAmount', headers=self._headers(),
                         params=dict(self.pair, amount=str(amount)), timeout=90)
        body = r.json()
        if r.status_code != 200 or body.get('message') != 'OK':
            raise RuntimeError(f"quote failed ({r.status_code}): {body.get('message', body)}")
        quotes = body.get('data') or []
        best = self.pick(quotes, amount, self.rate)
        if best is None:
            offered = [(q['provider_name'], q['exchange_type'], q.get('min_amount'), q.get('max_amount')) for q in quotes]
            raise RuntimeError(f'no {self.rate} quote covers {amount} (offered: {offered})')
        return Quote(self.name, best['provider_name'], best['exchange_type'], float(best['amount']),
                     {'tags': best.get('tags')})

    def create(self, quote, amount, address_to, refund_address):
        # Flashift has no refund-address field; refunds are handled with the provider's support.
        body = dict(self.pair, provider_name=quote.provider, to_address=address_to, to_extra_id='',
                    amount=str(amount), fixed=(quote.rate_type == 'fixed'))
        r = requests.post(f'{self.API}/createTransaction', headers=self._headers(), json=body, timeout=90)
        created = r.json()
        order_id = created.get('exchange_id')
        if r.status_code != 200 or created.get('status') != 'OK' or not order_id:
            raise RuntimeError(f'createTransaction failed ({r.status_code}): {created}')
        # createTransaction only returns an id; the deposit address comes from getTransactionInfo
        info = {}
        for _ in range(5):
            time.sleep(3)
            r = requests.get(f'{self.API}/getTransactionInfo', headers=self._headers(),
                             params={'exchange_id': order_id}, timeout=60)
            info = r.json()
            if r.status_code == 200 and info.get('address_from'):
                break
        return Order(self.name, quote.provider, order_id, info.get('address_from') or '',
                     info.get('extra_from') or '', info.get('address_to') or '',
                     float(info.get('amount_to') or quote.amount_to))


class TrocadorClient:
    """https://trocador.app/docs - aggregator; new_rate returns a rate id plus quotes per exchange."""
    name = 'Trocador'
    API = 'https://api.trocador.app'

    def __init__(self, settings, pair):
        self.key = os.environ.get('TROCADOR_API_KEY') or settings.get('api_key', '')
        self.rate = settings.get('rate', 'floating').lower()
        self.min_kyc = settings.get('min_kycrating', '').strip().upper()
        self.pair = {'ticker_from': pair['from'], 'network_from': settings.get('network_from', 'Mainnet'),
                     'ticker_to': pair['to'], 'network_to': settings.get('network_to', 'Polygon')}

    @property
    def enabled(self):
        return bool(self.key)

    def _get(self, method, params):
        r = requests.get(f'{self.API}/{method}', headers={'API-Key': self.key}, params=params, timeout=120)
        try:
            body = r.json()
        except ValueError:
            body = {'error': r.text[:300]}
        if r.status_code != 200 or (isinstance(body, dict) and body.get('error')):
            raise RuntimeError(f'{method} failed ({r.status_code}): {body}')
        return body

    @staticmethod
    def quote_list(rate):
        quotes = rate.get('quotes') or []
        if isinstance(quotes, dict):
            quotes = quotes.get('quotes') or []
        return [q for q in quotes if isinstance(q, dict) and q.get('provider') and q.get('amount_to')]

    @classmethod
    def pick(cls, rate, wanted):
        """Best quote for the wanted rate type; Trocador already sorts best first."""
        candidates = []
        if rate.get('provider') and rate.get('amount_to'):
            candidates.append({'provider': rate['provider'], 'amount_to': rate['amount_to'], 'fixed': rate.get('fixed')})
        for q in cls.quote_list(rate):
            # quotes inherit the request's rate type unless they say otherwise
            candidates.append(dict(q, fixed=q.get('fixed', rate.get('fixed'))))
        if wanted != 'any':
            candidates = [c for c in candidates if as_bool(c.get('fixed')) == (wanted == 'fixed')]
        if not candidates:
            return None
        return max(candidates, key=lambda c: float(c['amount_to']))

    def quote(self, amount):
        params = dict(self.pair, amount_from=str(amount))
        if self.min_kyc:
            params['min_kycrating'] = self.min_kyc
        rate = self._get('new_rate', params)
        best = self.pick(rate, self.rate)
        if best is None:
            raise RuntimeError(f'no {self.rate} quote (best was {rate.get("provider")} fixed={rate.get("fixed")})')
        rate_type = 'fixed' if as_bool(best.get('fixed')) else 'floating'
        return Quote(self.name, best['provider'], rate_type, float(best['amount_to']),
                     {'id': rate.get('trade_id'), 'kyc': best.get('kycrating')})

    def create(self, quote, amount, address_to, refund_address):
        params = dict(self.pair, id=quote.ref.get('id'), amount_from=str(amount), address=address_to,
                      refund=refund_address, provider=quote.provider,
                      fixed=str(quote.rate_type == 'fixed'))
        if self.min_kyc:
            params['min_kycrating'] = self.min_kyc
        trade = self._get('new_trade', params)
        return Order(self.name, trade.get('provider') or quote.provider, str(trade.get('trade_id')),
                     trade.get('address_provider') or '', trade.get('address_provider_memo') or '',
                     trade.get('address_user') or '', float(trade.get('amount_to') or quote.amount_to))


CLIENTS = {'flashift': FlashiftClient, 'trocador': TrocadorClient}


class Exchange:
    def __init__(self, config):
        self.config = config
        pair = {'from': config['convert_from'].lower(), 'to': config['convert_to'].lower()}
        self.clients = []
        for name in config['providers']:
            cls = CLIENTS.get(name.lower())
            if cls is None:
                raise ValueError(f'Unknown provider {name!r}; use Flashift and/or Trocador')
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
                log.info('%s quote: %s %s %s %s -> %.6f %s', client.name, q.provider, q.rate_type, amount,
                         self.config['convert_from'], q.amount_to, self.config['convert_to'])
                found.append((q, client))
            except Exception as e:
                log.warning('%s quote failed: %s', client.name, e)
        return sorted(found, key=lambda qc: qc[0].amount_to, reverse=True)

    def _check_order(self, order, refund_address):
        """drain sends the whole amount to the deposit address, so be strict about what we accept."""
        if not ARK_ADDRESS.match(order.deposit_address):
            raise RuntimeError(f'deposit address is not an ARK address: {order.deposit_address!r}')
        if order.deposit_address in (refund_address, self.config['convert_address']):
            raise RuntimeError('deposit address equals our own address')
        if order.deposit_memo and str(order.deposit_memo).strip() not in ('0', 'None', 'null'):
            raise RuntimeError(f'deposit needs a memo ({order.deposit_memo!r}), which drain does not send')
        if order.payout_address.lower() != self.config['address_to'].lower():
            raise RuntimeError(f'order pays out to {order.payout_address!r}, not address_to')

    def create(self, amount, refund_address):
        """Create an order for `amount` (whole coins). Returns the checked Order, or None."""
        for quote, client in self.quotes(amount):
            try:
                order = client.create(quote, amount, self.config['address_to'], refund_address)
                self._check_order(order, refund_address)
            except Exception as e:
                log.error('%s via %s failed: %s', client.name, quote.provider, e)
                continue
            log.info('Order %s (%s via %s): send %s %s to %s for ~%.6f %s', order.order_id, order.service,
                     order.provider, amount, self.config['convert_from'], order.deposit_address,
                     order.amount_to, self.config['convert_to'])
            with open(self.config['exchange_log'], 'a') as f:
                f.write(','.join([datetime.now().isoformat(), order.service, order.provider, order.order_id,
                                  str(amount), order.deposit_address, str(order.amount_to)]) + '\n')
            return order
        log.error('No provider produced a usable order')
        return None
