from __future__ import absolute_import, print_function

from twisted.logger import Logger
from twisted.internet import reactor

import joinmarket as jm
from joinmarket.btc_generator_basic import build_objects

log = Logger()


# noinspection PyBroadException
def run():
    maker, wallet = None, None
    try:
        log.debug('Reactor Run')
        maker, wallet = build_objects()
        reactor.run()
    except:
        log.debug('CRASHING, DUMPING EVERYTHING')
        jm.debug_dump_object(wallet, ['addr_cache', 'keys', 'seed'])
        jm.debug_dump_object(maker)
        import traceback
        log.debug(traceback.format_exc())
