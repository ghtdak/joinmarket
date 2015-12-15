#! /usr/bin/env python
from __future__ import absolute_import, print_function

import binascii
import os
import random

from twisted.internet import reactor
from twisted.logger import Logger

from joinmarket.yield_generator_basic import build_objects as build_yld

log = Logger()
log.debug('wtf')

import bitcoin as btc
import joinmarket as jm


def main():

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
                jm.bc_interface.grab_coins(w.get_receive_addr(j), amt)

    argvv = [['btc_generator_basic.py', str(wallets[i]['seed'])]
             for i in range(6)]

    printTumblr(argvv)

    for argv in argvv:
        log.debug('launching yielder: {}'.format(str(argv)))
        block_inst, _, _ = build_yld(argv)
        block_inst.build_irc()


    log.debug('done building')

reactor.callWhenRunning(main)
reactor.run()
