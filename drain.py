from solar_client import SolarClient
from configparser import ConfigParser
from pathlib import Path
from modules.exchange import Exchange
from solar_crypto.transactions.builder.transfer import Transfer
from solar_crypto.configuration.network import set_custom_network
from datetime import datetime


def get_config():
    home = str(Path.home())
    config_path = home+'/drain/config.ini'
    config = ConfigParser()
    config.read(config_path)

    config_dict = {'atomic' : int(config.get('static', 'atomic')),
                   'network' : config.get('static', 'network'),
                   'passphrase' : config.get('static', 'passphrase'),
                   'secondphrase' : config.get('static', 'secondphrase'),
                   'convert_from' : config.get('static', 'convert_from'),
                   'convert_address' : config.get('static', 'convert_address'),
                   'convert_to' : config.get('static', 'convert_to'),
                   'address_to' : config.get('static', 'address_to'),
                   'network_to' : config.get('static', 'network_to'),
                   'provider' : config.get('static', 'provider')}

    return config_dict


def build_transfer_tx(config, exchange, fee, amt, n):
    transaction = Transfer()
    transaction.set_fee(fee)
    transaction.set_nonce(n)
    net_exchange = amt-fee
    
    # exchange processing
    pay_in = exchange.exchange_select(config['convert_address'], net_exchange, config['provider'])
    if pay_in == config['convert_address']:
        print('Failed Exchange - Quit Processing')
        quit()
    else:
        print('Succcessful Exchange')

    transaction.add_transfer(net_exchange, pay_in)

    transaction.sign(config['passphrase'])
    sp = config['secondphrase']
    if sp == 'None':
        sp = None
    if sp is not None:
        transaction.second_sign(sp)
    
    transaction_dict = transaction.to_dict()
    return transaction_dict


def get_client(ip="localhost"):
    solar_epoch = ["2022","03","28","18","00","00"]
    t = [int(i) for i in solar_epoch]
    epoch = datetime(t[0], t[1], t[2], t[3], t[4], t[5])
    version = 63
    wif = 252
    set_custom_network(epoch, version, wif)
    return SolarClient('http://{0}:{1}/api'.format(ip, 6003))
  
  
  def get_fee(client, numtx=1):
    node_configs = client.node.configuration()['data']['pool']['dynamicFees']
    dynamic_offset = node_configs['addonBytes']['transfer']
    fee_multiplier = node_configs['minFeePool']

    # get size of transaction
    multi_tx = 125
    second_sig = 64
    per_tx_fee = 29
    tx_size = multi_tx + second_sig + (numtx * per_tx_fee)

    # calculate transaction fee
    transaction_fee = int((dynamic_offset + (round(tx_size/2) + 1)) * fee_multiplier)
    return transaction_fee


if __name__ == '__main__':    
    # get client / config / fees
    config = get_config()
    client = get_client()
    fee = get_fee(client)
    exchange = Exchange(config)

    # get wallet balance
    wallet = client.wallets.get(config['convert_address'])['data']
    nonce = int(wallet['nonce'])
    balance = int(wallet['balance'])
    
    # build transfer
    tx = build_transfer_tx(config, exchange, fee, balance, nonce)
    print(tx)

    # broadcast transaction
    transaction = client.transactions.create(tx)
    print(transaction)