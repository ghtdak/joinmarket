from __future__ import absolute_import, print_function

import traceback

from twisted.logger import Logger
from twisted.internet import reactor

from joinmarket.yield_generator_basic import build_objects

log = Logger()


# noinspection PyBroadException
def run():
    try:
        log.debug('Reactor Run')
        block_instance, _, _ = build_objects()
        block_instance.build_irc()
        reactor.run()
    except:
        log.debug('CRASHING, DUMPING EVERYTHING')
        # jm.debug_dump_object(wallet, ['addr_cache', 'keys', 'seed'])
        # jm.debug_dump_object(maker)
        log.debug(traceback.format_exc())
