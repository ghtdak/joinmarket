from __future__ import absolute_import, print_function

import abc
import pprint

from twisted.internet import defer
from twisted.logger import Logger

from joinmarket.core import system_shutdown

log = Logger()


class TransactionWatcher(object):

    def __init__(self, cjpeer):
        self.cjpeer = cjpeer
        self.block_instance = self.cjpeer.block_instance
        self.msgchan = self.block_instance.irc_market

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
        self.log.error('UNIMPLEMENTED ERROR - not gonna rais it tho')


class BlockchainInterface(object):
    __metaclass__ = abc.ABCMeta

    def __init__(self):
        self.log = Logger(namespace=self.__module__)

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
        ns = self.__module__ + '@' + self._nickname
        self.log = Logger(ns)

    # moved from blockchaininterface
    def sync_wallet(self):
        self.sync_addresses()
        self.sync_unspent()

    # moved from blockchaininterface
    def sync_addresses(self):
        """Finds which addresses have been used and sets
        wallet.index appropriately"""
        pass

    def sync_unspent(self):
        """Finds the unspent transaction outputs belonging to this wallet,
        sets wallet.unspent """
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
        raise NotImplementedError

    def remove_old_utxos(self, tx):
        raise NotImplementedError

    def add_new_utxos(self, tx, txid):
        raise NotImplementedError

    def select_utxos(self, mixdepth, amount):
        raise NotImplementedError()

    def get_balance_by_mixdepth(self):
        raise NotImplementedError()


class CoinJoinerPeer(object):

    def __init__(self, block_instance):
        ns = self.__module__ + '@' + block_instance.nickname
        self.log = Logger(namespace=ns)
        self.block_instance = block_instance
        self.msgchan = self.block_instance.irc_market
        self.nickname = self.block_instance.nickname  # convenience - unsafe?

        # not the cleanest but it automates what would be an extra step
        self.block_instance.set_coinjoinerpeer(self)

    def __getattr__(self, name):
        """
        This is probably a little risky.  If an on_<whatever> gets called
        on an object which doesn't have it, do nothing -
        :param name:
        :return:
        """
        if name[:3] == 'on_':
            # log.debug('{} event not implemented'.format(name))
            return self.do_nothing
        else:
            raise AttributeError

    def do_nothing(self, *args, **kwargs):
        pass

    def on_stalled(self):
        self.log.debug('stalled, shutting down')
        system_shutdown(-1, 'stalled')

__all__ = ('AbstractWallet',)
