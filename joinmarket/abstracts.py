from __future__ import absolute_import, print_function

import abc

from twisted.internet import defer
from twisted.logger import Logger
from configparser import NoSectionError

import bitcoin as btc
from joinmarket.support import select_gradual, select_greedy, select_greediest
from .configure import config

log = Logger()

class TransactionWatcher(object):

    def __init__(self, cjpeer):
        self.cjpeer = cjpeer
        ns = self.__module__ + '@' + cjpeer.nickname
        self.log = Logger(namespace=ns)

        self.d_confirm = None
        self._tx = None
        self._cj_addr = None

    @property
    def txd(self):
        return self._tx

    @txd.setter
    def txd(self, value):
        self._tx = value

    @property
    def cj_addr(self):
        return self._cj_addr

    @cj_addr.setter
    def cj_addr(self, value):
        self._cj_addr = value

    def confirm(self):
        self.d_confirm = defer.Deferred()
        return self.d_confirm

    def send_confirm(self, txd, txid, txdata):
        self.d_confirm.callback((txd, txid, txdata))

    def unconfirmfun(self, txd, txid):
        pass


class BlockchainInterface(object):
    __metaclass__ = abc.ABCMeta

    def __init__(self):
        pass

    def sync_wallet(self, wallet):
        self.sync_addresses(wallet)
        self.sync_unspent(wallet)

    @abc.abstractmethod
    def sync_addresses(self, wallet):
        """Finds which addresses have been used and sets
        wallet.index appropriately"""
        pass

    @abc.abstractmethod
    def sync_unspent(self, wallet):
        """Finds the unspent transaction outputs belonging to this wallet,
        sets wallet.unspent """
        pass

    @abc.abstractmethod
    def add_tx_notify(self, transaction_watcher):
        """Invokes unconfirmfun and confirmfun when tx is seen on the network"""
        pass

    @abc.abstractmethod
    def pushtx(self, txhex):
        """pushes tx to the network, returns txhash, or None if failed"""
        pass

    @abc.abstractmethod
    def query_utxo_set(self, txouts):
        """
        takes a utxo or a list of utxos
        returns None if they are spend or unconfirmed
        otherwise returns value in satoshis, address and output script
        """
        # address and output script contain the same information btw


class AbstractWallet(object):
    """
    Abstract wallet for use with JoinMarket
    Mostly written with Wallet in mind, the default JoinMarket HD wallet
    """

    def __init__(self):
        self.max_mix_depth = 0
        self._nickname = 'unk'
        self.log = Logger(self.nickname)
        self.utxo_selector = btc.select  # default fallback: upstream
        try:
            if config.get("POLICY", "merge_algorithm") == "gradual":
                self.utxo_selector = select_gradual
            elif config.get("POLICY", "merge_algorithm") == "greedy":
                self.utxo_selector = select_greedy
            elif config.get("POLICY", "merge_algorithm") == "greediest":
                self.utxo_selector = select_greediest
            elif config.get("POLICY", "merge_algorithm") != "default":
                raise Exception("Unknown merge algorithm")
        except NoSectionError:
            pass

    @property
    def nickname(self):
        return self._nickname

    @nickname.setter
    def nickname(self, value):
        self._nickname = value
        ns = self.__module__ + '@' + self._nickname
        self.log = Logger(namespace=ns)

    def get_key_from_addr(self, addr):
        raise NotImplementedError()

    def get_utxos_by_mixdepth(self):
        raise NotImplementedError

    def get_change_addr(self, mixing_depth):
        raise NotImplementedError

    def update_cache_index(self):
        return

    def remove_old_utxos(self, tx):
        return

    def add_new_utxos(self, tx, txid):
        return

    def select_utxos(self, mixdepth, amount):
        utxo_list = self.get_utxos_by_mixdepth()[mixdepth]
        unspent = [{'utxo': utxo,
                    'value': addrval['value']}
                   for utxo, addrval in utxo_list.iteritems()]
        inputs = self.utxo_selector(unspent, amount)
        log.debug('for mixdepth={} amount={} selected:'.format(
                mixdepth, amount))
        # log.debug('select_utxos: {}'.format(inputs))
        return dict([(i['utxo'], {'value': i['value'],
                                  'address': utxo_list[i['utxo']]['address']})
                     for i in inputs])

    def get_balance_by_mixdepth(self):
        mix_balance = {}
        for m in range(self.max_mix_depth):
            mix_balance[m] = 0
        for mixdepth, utxos in self.get_utxos_by_mixdepth().iteritems():
            mix_balance[mixdepth] = sum([addrval['value']
                                         for addrval in utxos.values()])
        return mix_balance

class CoinJoinerPeer(object):

    def __init__(self, block_instance):
        ns = self.__module__ + '@' + block_instance.nickname
        self.log = Logger(namespace=ns)
        self.block_instance = block_instance
        self.nickname = block_instance.nickname  # convenience - unsafe?

        # not the cleanest but it automates what would be an extra step
        self.block_instance.set_coinjoinerpeer(self)

    def __getattr__(self, name):
        if name == 'msgchan':
            return self.block_instance.irc_market
        if name[:3] == 'on_':
            log.debug('{} event not implemented'.format(name))
            return self.do_nothing
        else:
            raise AttributeError

    def do_nothing(self, *args, **kwargs):
        pass

__all__ = ('AbstractWallet',)
