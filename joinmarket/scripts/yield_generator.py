from __future__ import absolute_import, print_function

from twisted.internet import reactor
from twisted.logger import Logger

from joinmarket.yield_generator_basic import build_objects

log = Logger()


def main():
    try:
        block_inst = build_objects()
        block_inst.build_irc()
    except:
        log.failure('badness')

    # d = deferredFrob(knob)
    # d.addErrback(lambda f: log.failure, "While frobbing {knob}", f, knob=knob)


def run():
    reactor.callWhenRunning(main)
    reactor.run()

if __name__ == '__main__':
    run()
