#! /usr/bin/env python
from __future__ import absolute_import, print_function

import binascii
import os
import random
import sys

from twisted.internet import reactor
from twisted.logger import Logger

from joinmarket.core import jmbtc
import joinmarket.core as jm
from joinmarket.tumbler import build_objects as build_tumbler
from joinmarket.yield_generator_basic import build_objects as build_yld

log = Logger()
log.debug('wtf')


def generate_wallets(num_wallets, base):
    log.debug('generate wallets: {num_wallets}', num_wallets=num_wallets)
    seeds = [binascii.hexlify(os.urandom(10)) for _ in range(num_wallets)]
    wallets = {}
    for i in range(num_wallets):
        wallets[i] = {'seed': seeds[i],
                      'wallet': jm.Wallet(seeds[i], max_mix_depth=5)}

    # adding coins somewhat randomly, spread over all 5 depths

    for i in range(num_wallets):
        w = wallets[i]['wallet']
        for j in range(5):
            for k in range(4):
                amt = base + random.random()
                amt = float("%.6f" % amt)
                # log.debug('grabbing amount: {}'.format(amt))
                try:
                    jm.bc_interface.grab_coins(w.get_receive_addr(j), amt)
                except:
                    log.debug('grab_coins reject', j=j, amt=amt)

    return wallets


def generate_yield_wallets(num_yield):
    log.debug('generate_yield_wallets')
    wallets = generate_wallets(num_yield, 2.0)

    argv = [['btc_generator_basic.py', str(wallets[i]['seed'])]
            for i in range(num_yield)]

    log.debug('yield run info')
    for a in argv:
        print(str(a))

    return argv


def generate_tumbler_wallets(num_tumbler):
    log.debug('generate_tumbler_wallets')
    wallets = generate_wallets(num_tumbler, 0.001)

    # todo: decide on proper testing size
    amt = int(1e5)  # in satoshis

    # send to any old address
    dest_address = [jmbtc.privkey_to_address(os.urandom(32))
                    for _ in range(num_tumbler)]

    # default mixdepth source is zero, so will take coins from m 0.
    # see tumbler.py --h for details

    argv = [['tumbler.py', '-N', '2', '0', '-a', '0', '-M', '5', '-w', '3',
             '-l', '0.2', '-q', '5', '-s', str(amt), str(wallets[i]['seed']),
             dest_address[i]] for i in range(num_tumbler)]

    log.debug('tumbler run info')
    for a in argv:
        print(str(a))

    return argv


def buildYields(yield_argv):

    ylds = []
    for argv in yield_argv:
        log.debug('launchYield: {argv}', argv=argv)
        ylds.append(build_yld(argv))

    return ylds


def buildTumbler(tumblr_argv):

    tmblrs = []
    for argv in tumblr_argv:
        log.debug('launchTumbler: {argv}', argv=argv)
        tmblrs.append(build_tumbler(argv))

    return tmblrs


def launch(insts):
    try:
        for n, y in enumerate(insts):
            log.debug('launching for nick: {nick}', nick=y.nickname)

            # stagger startup by a second
            reactor.callLater(n, y.build_irc)
    except:
        log.failure('launch failed')


def run():
    num_yields = 6
    num_tumblers = 1
    w = 'tumbler'
    if len(sys.argv) > 1:
        w = sys.argv[1]
    log.debug('choice: {w}', w=w)

    jm.bc_interface.rpc('generate', [101])

    if w == 'both':
        log.debug('running both')
        ylds = buildYields(generate_yield_wallets(num_yields))
        tumblers = buildTumbler(generate_tumbler_wallets(num_tumblers))
        jm.bc_interface.rpc('generate', [10])
        reactor.callWhenRunning(launch, ylds + tumblers)
    elif w == 'yields':
        log.debug('running yields')
        ylds = buildYields(generate_yield_wallets(num_yields))
        jm.bc_interface.rpc('generate', [10])
        reactor.callWhenRunning(launch, ylds)
    elif w == 'generate':
        aw = generate_yield_wallets(num_yields)
        at = generate_tumbler_wallets(num_tumblers)
        jm.bc_interface.rpc('generate', [10])
        for a in aw + at:
            print(str(a))
        return
    else:
        log.debug('running tumbler')
        tumblers = buildTumbler(num_tumblers)
        jm.bc_interface.rpc('generate', [10])
        reactor.callWhenRunning(launch, tumblers)

    reactor.run()


if __name__ == '__main__':
    sys.exit(run())
