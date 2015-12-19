#! /usr/bin/env python
from __future__ import absolute_import, print_function

import sys
from twisted.internet import reactor
from twisted.logger import Logger

from joinmarket.tumbler import build_objects as build_tumbler

log = Logger()
log.debug('wtf')


def buildTumbler():
    tumblr_argv = ['tumbler.py', '-N', '2', '0', '-a', '0', '-M', '5',
                   '-w', '3', '-l', '0.2', '-s', '100000000', '59bf49a',
                   'mhyGR4qBKDWoCdFZuzoSyVeCrphtPXtbgD']

    log.debug('launchTumbler: {argv}', argv=tumblr_argv)
    return build_tumbler(tumblr_argv)


def main(tumbler):
    try:
        log.debug('reactor running')
        tumbler.build_irc()
    except:
        log.failure('badness')


def run():
    tumbler = buildTumbler()
    reactor.callWhenRunning(main, tumbler)
    reactor.run()


if __name__ == '__main__':
    sys.exit(run())
