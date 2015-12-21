from __future__ import absolute_import, print_function

from twisted.logger import Logger
from twisted.internet import reactor

import joinmarket.core as jm
from joinmarket.sendpayment import build_objects

log = Logger()


# noinspection PyBroadException
def run():
    taker, wallet = None, None
    try:
        log.debug('Reactor Run')
        block_instance, taker, wallet = build_objects()
        reactor.run()
    except:
        log.debug('CRASHING, DUMPING EVERYTHING')
        jm.debug_dump_object(wallet, ['addr_cache', 'keys', 'seed'])
        jm.debug_dump_object(taker)
        import traceback
        log.debug(traceback.format_exc())
