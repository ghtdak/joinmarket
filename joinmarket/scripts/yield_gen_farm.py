#! /usr/bin/env python
from __future__ import absolute_import, print_function

import binascii
import os
import random
import sys

from twisted.internet import reactor
from twisted.logger import Logger

import bitcoin as btc
import joinmarket.core as jm
from joinmarket.tumbler import build_objects as build_tumbler
from joinmarket.yield_generator_basic import build_objects as build_yld

log = Logger()
log.debug('wtf')


def generate_yield_wallets(num_yield):

    seeds = jm.chunks(binascii.hexlify(os.urandom(15 * 7)), num_yield)
    wallets = {}
    for i in range(num_yield):
        wallets[i] = {'seed': seeds[i],
                      'wallet': jm.Wallet(seeds[i], max_mix_depth=5)}

    # adding coins somewhat randomly, spread over all 5 depths

    for i in range(num_yield):
        w = wallets[i]['wallet']
        for j in range(5):
            for k in range(10):
                base = 1.0
                # average is 0.5 for tumbler, else 1.5
                amt = base + random.random()
                amt = float("%.6f" % amt)
                log.debug('grabbing amount: {}'.format(amt))
                try:
                    jm.bc_interface.grab_coins(w.get_receive_addr(j), amt)
                except:
                    log.debug('grab_coins reject', j=j, amt=amt)

    argv = [['btc_generator_basic.py', str(wallets[i]['seed'])]
            for i in range(num_yield)]

    log.debug('yield run info')
    for a in argv:
        print(str(a))

    return argv


def generate_tumbler_wallets(num_tumbler):

    seeds = jm.chunks(binascii.hexlify(os.urandom(15 * 7)), num_tumbler)
    wallets = {}
    for i in range(num_tumbler):
        wallets[i] = {'seed': seeds[i],
                      'wallet': jm.Wallet(seeds[i], max_mix_depth=5)}

    for i in range(num_tumbler):
        w = wallets[i]['wallet']
        for j in range(5):
            for k in range(10):
                base = 0.001
                # average is 0.5 for tumbler, else 1.5
                amt = base + random.random()
                amt = float("%.6f" % amt)
                log.debug('grabbing amount: {}'.format(amt))
                try:
                    jm.bc_interface.grab_coins(w.get_receive_addr(j), amt)
                except:
                    log.debug('grab_coins reject', j=j, amt=amt)

    amt = int(1e8)  # in satoshis

    # send to any old address
    dest_address = [
        btc.privkey_to_address(os.urandom(32), jm.get_p2pk_vbyte())
        for _ in range(num_tumbler)]

    # default mixdepth source is zero, so will take coins from m 0.
    # see tumbler.py --h for details

    argv = [['tumbler.py', '-N', '2', '0', '-a', '0', '-M', '5', '-w', '3',
             '-l', '0.2', '-s', str(amt), str(wallets[i]['seed']),
             dest_address[i]] for i in range(num_tumbler)]

    log.debug('tumbler run info')
    for a in argv:
        print(str(a))

    return argv


def buildYields():
    _yield_argv = [['btc_generator_basic.py', '79d18ce'],
                   ['btc_generator_basic.py', '41d158b'],
                   ['btc_generator_basic.py', 'c5417cf'],
                   ['btc_generator_basic.py', '7ba4b63'],
                   ['btc_generator_basic.py', '732ad8c'],
                   ['btc_generator_basic.py', '8bf5fcd']]

    yield_argv = [['btc_generator_basic.py', '0de582fb1b'],
                  ['btc_generator_basic.py', 'be690fd5c7'],
                  ['btc_generator_basic.py', '4c7736402b'],
                  ['btc_generator_basic.py', '044d5279c9'],
                  ['btc_generator_basic.py', '4c6d819823'],
                  ['btc_generator_basic.py', '8889fa8950'],
                  ['btc_generator_basic.py', 'eec5c2c951'],
                  ['btc_generator_basic.py', '6f77ebec5c'],
                  ['btc_generator_basic.py', '22f784a732'],
                  ['btc_generator_basic.py', '160f60d2a4']]

    ylds = []
    for argv in yield_argv:
        log.debug('launchYield: {argv}', argv=argv)
        ylds.append(build_yld(argv))

    return ylds


def buildTumbler():
    _tumblr_argv = [['tumbler.py', '-N', '2', '0', '-a', '0', '-M', '5',
                    '-w', '3', '-l', '0.2', '-s', '100000000', '59bf49a',
                    'mhyGR4qBKDWoCdFZuzoSyVeCrphtPXtbgD']]

    tumblr_argv = [['tumbler.py', '-N', '2', '0', '-a', '0', '-M', '5', '-w',
                    '3', '-l', '0.2', '-s', '100000000', '72',
                    'mttJs8ghKdBum3umD5EP2k5acz3HNQjsey'],
                   ['tumbler.py', '-N', '2', '0', '-a', '0', '-M', '5', '-w',
                    '3', '-l', '0.2', '-s', '100000000', '22',
                    'mnmV8RHftcgXF1mkmrmLc9jv8RwLG1WWn3']]

    tumblr_argv.pop()  # one for now

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
    w = 'tumbler'
    if len(sys.argv) > 1:
        w = sys.argv[1]
    log.debug('choice: {w}', w=w)
    if w == 'both':
        log.debug('running both')
        ylds = buildYields()
        tumblers = buildTumbler()
        # pre-initialized so just go ahead and launch (staggered)
        reactor.callWhenRunning(launch, ylds + tumblers)
    elif w == 'yields':
        log.debug('running yields')
        ylds = buildYields()
        reactor.callWhenRunning(launch, ylds)
    elif w == 'generate':
        aw = generate_yield_wallets(10)
        at = generate_tumbler_wallets(2)
        for a in aw + at:
            print(str(a))
        return
    else:
        log.debug('running tumbler')
        tumblers = buildTumbler()
        reactor.callWhenRunning(launch, tumblers)

    reactor.run()


if __name__ == '__main__':
    sys.exit(run())
