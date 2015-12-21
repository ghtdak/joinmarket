#! /usr/bin/env python
from __future__ import absolute_import, print_function

import binascii
import os
import random
import sys

from twisted.internet import reactor
from twisted.logger import Logger

from joinmarket.yield_generator_basic import build_objects as build_yld
from joinmarket.tumbler import build_objects as build_tumbler

import bitcoin as btc
import joinmarket as jm

log = Logger()
log.debug('wtf')


def buildWallets(argv=None):
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

        av = ['tumbler.py', '-N', '2', '0', '-a', '0', '-M', '5', '-w', '3',
              '-l', '0.2', '-s', str(amt),
              str(wallets[6]['seed']), dest_address]

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
                amt = float("%.6f" % amt)
                log.debug('grabbing amount: {}'.format(amt))
                try:
                    jm.bc_interface.grab_coins(w.get_receive_addr(j), amt)
                except:
                    log.debug('grab_coins reject', j=j, amt=amt)

    argvv = [['btc_generator_basic.py', str(wallets[i]['seed'])]
             for i in range(6)]

    printTumblr(argvv)


def buildYields():
    yield_argv = [['btc_generator_basic.py', '79d18ce'],
                  ['btc_generator_basic.py', '41d158b'],
                  ['btc_generator_basic.py', 'c5417cf'],
                  ['btc_generator_basic.py', '7ba4b63'],
                  ['btc_generator_basic.py', '732ad8c'],
                  ['btc_generator_basic.py', '8bf5fcd']]

    ylds = []
    for argv in yield_argv:
        log.debug('launchYield: {argv}', argv=argv)
        ylds.append(build_yld(argv))

    return ylds


def buildTumbler():
    tumblr_argv = ['tumbler.py', '-N', '2', '0', '-a', '0', '-M', '5',
                   '-w', '3', '-l', '0.2', '-s', '100000000', '59bf49a',
                   'mhyGR4qBKDWoCdFZuzoSyVeCrphtPXtbgD']

    log.debug('launchTumbler: {argv}', argv=tumblr_argv)
    return [build_tumbler(tumblr_argv)]


def launch(insts):
    try:
        for n, y in enumerate(insts):
            log.debug('launching for nick: {nick}', nick=y.nickname)

            # stagger startup by a second
            reactor.callLater(n, y.build_irc)
    except:
        log.failure('launch failed')


def run():
    w = 'tumbler'
    if len(sys.argv) > 1:
        w = sys.argv[1]
    log.debug('choice: {w}', w=w)
    if w == 'both':
        log.debug('running both')
        ylds = buildYields()
        tumblers = buildTumbler()
        reactor.callWhenRunning(launch, ylds)
        reactor.callLater(30, launch, tumblers)
    elif w == 'yields':
        log.debug('running yields')
        ylds = buildYields()
        reactor.callWhenRunning(launch, ylds)
    else:
        log.debug('running tumbler')
        tumblers = buildTumbler()
        reactor.callWhenRunning(launch, tumblers)

    reactor.run()


if __name__ == '__main__':
    sys.exit(run())
