from __future__ import absolute_import, print_function

import logging

from .support import get_log, calc_cj_fee, debug_dump_object, \
    choose_sweep_orders, choose_orders, \
    pick_order, cheapest_order_choose, weighted_order_choose, \
    rand_norm_array, rand_pow_array, rand_exp_array, system_shutdown
from .enc_wrapper import decode_decrypt, encrypt_encode, get_pubkey
from .txirc import build_irc_communicator, random_nick
from .maker import Maker
from .message_channel import MessageChannel
from .old_mnemonic import mn_decode, mn_encode
from .slowaes import decryptData, encryptData
from .taker import Taker, OrderbookWatch, TakerSibling, CoinJoinTX
from .wallet import AbstractWallet, BitcoinCoreInterface, Wallet, \
    BitcoinCoreWallet
from .configure import BlockInstance, config, get_network, maker_timeout_sec,\
    get_config_irc_channel, get_p2pk_vbyte, validate_address, DUST_THRESHOLD

from .blockchaininterface import BlockrInterface
# Set default logging handler to avoid "No handler found" warnings.

try:
    from logging import NullHandler
except ImportError:
    class NullHandler(logging.Handler):
        def emit(self, record):
            pass

logging.getLogger(__name__).addHandler(NullHandler())

