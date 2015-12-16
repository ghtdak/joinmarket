#! /usr/bin/env python
from __future__ import absolute_import, print_function

import binascii
import os
import random

import sys

import signal
from twisted.internet import reactor
from twisted.logger import Logger

from joinmarket.yield_generator_basic import build_objects as build_yld

log = Logger()
log.debug('wtf')

import bitcoin as btc
import joinmarket as jm


def main(argv=None):
    if argv is None:
        argv = sys.argv


    def printTumblr(args):
        for a in args:
            print(str(a))


        # start a tumbler
        amt = int(1e8)  # in satoshis

        # send to any old address
        dest_address = btc.privkey_to_address(os.urandom(32),
                                              jm.get_p2pk_vbyte())
        # default mixdepth source is zero, so will take coins from m 0.
        # see tumbler.py --h for details

        av = ['tumbler.py', '-N', '2', '0', '-a', '0', '-M', '5', '-w',
                '3', '-l', '0.2', '-s', str(amt), str(wallets[6]['seed']),
                dest_address]

        print(str(av))

        # _block_inst, _, _ = build_tumbler(argv)
        # _block_inst.build_irc()

    # create 7 new random wallets.
    # put about 10 coins in each, spread over random mixdepths
    # in units of 0.5

    seeds = jm.chunks(binascii.hexlify(os.urandom(15 * 7)), 7)
    wallets = {}
    for i in range(7):
        wallets[i] = {'seed': seeds[i],
                      'wallet': jm.Wallet(seeds[i], max_mix_depth=5)}

    # adding coins somewhat randomly, spread over all 5 depths
    for i in range(7):
        w = wallets[i]['wallet']
        for j in range(5):
            for k in range(10):
                base = 0.001 if i == 6 else 1.0
                # average is 0.5 for tumbler, else 1.5
                amt = base + random.random()
                amt = float("%.6f" % (amt))
                log.debug('grabbing amount: {}'.format(amt))
                try:
                    jm.bc_interface.grab_coins(w.get_receive_addr(j), amt)
                except:
                    log.debug('grab_coins reject', j=j, amt=amt)

    argvv = [['btc_generator_basic.py', str(wallets[i]['seed'])]
             for i in range(6)]

    printTumblr(argvv)

    for argv in argvv:
        log.debug('launching yielder', argv=argv)
        def do_irc((block_inst, _, __)):
            block_inst.build_irc()
            log.debug('irc done for:', nick=block_inst.nickname)
        build_yld(argv).addCallback(do_irc)

    log.debug('done building')

def shutdown_handler(*args, **kwargs):
    log.debug('keyboard interrupt')
    reactor.stop()

def install_handler():
    signal.signal(signal.SIGINT, shutdown_handler)


def run():
    reactor.callLater(0.1, install_handler)
    reactor.callWhenRunning(main)
    reactor.run()

if __name__ == '__main__':
    sys.exit(run())
