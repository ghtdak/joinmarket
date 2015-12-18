#! /usr/bin/env python
from __future__ import absolute_import, print_function

from twisted.internet import reactor
from twisted.logger import Logger

from joinmarket.tumbler import build_objects as build_tumbler

log = Logger()
log.debug('wtf')

def main():

    argv = ['tumbler.py', '-N', '2', '0', '-a', '0', '-M', '5', '-w', '3', '-l', '0.2', '-s', '100000000', '4a1ea14', 'muvUXwYByQhMizhFRncLpuwyW4NpLYzPZq']

    log.debug('launching tumblrr: {}'.format(str(argv)))

    block_inst = build_tumbler(argv)
    block_inst.build_irc()
    log.debug('done building')


reactor.callWhenRunning(main)
reactor.run()
