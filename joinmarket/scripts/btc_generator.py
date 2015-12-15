from __future__ import absolute_import, print_function

import traceback

from twisted.logger import Logger
from twisted.internet import reactor

import joinmarket as jm
from joinmarket.btc_generator_basic import build_objects

log = Logger()


# noinspection PyBroadException
def run():
    block_instance, maker, wallet = None, None, None
    try:
        log.debug('Reactor Run')
        block_instance, maker, wallet = build_objects()
        reactor.run()
    except:
        log.debug('CRASHING, DUMPING EVERYTHING')
        # jm.debug_dump_object(wallet, ['addr_cache', 'keys', 'seed'])
        # jm.debug_dump_object(maker)
        log.debug(traceback.format_exc())
