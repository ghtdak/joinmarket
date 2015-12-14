from __future__ import absolute_import, print_function

from twisted.logger import Logger
from twisted.internet import reactor

import joinmarket as jm
from joinmarket.tumbler import build_objects

log = Logger()


# noinspection PyBroadException
def main(wallet, tumbler):
    try:
        log.debug('connecting to irc')
        reactor.run()
    except:
        log.debug('CRASHING, DUMPING EVERYTHING')
        jm.debug_dump_object(wallet, ['addr_cache', 'keys', 'seed'])
        jm.debug_dump_object(tumbler)
        jm.debug_dump_object(tumbler.cjtx)
        import traceback
        log.debug(traceback.format_exc())


if __name__ == "__main__":
    wallet, tumbler = build_objects()
    main(wallet, tumbler)
    print('done')
