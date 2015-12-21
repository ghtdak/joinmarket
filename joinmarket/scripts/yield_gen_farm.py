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
import joinmarket.core as jm

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
    yield_argv = [['btc_generator_basic.py', '79d18ce'],
                   ['btc_generator_basic.py', '41d158b'],
                   ['btc_generator_basic.py', 'c5417cf'],
                   ['btc_generator_basic.py', '7ba4b63'],
                   ['btc_generator_basic.py', '732ad8c'],
                   ['btc_generator_basic.py', '8bf5fcd']]

    _yield_argv = [['btc_generator_basic.py', 'd6fee66ca4'],
                  ['btc_generator_basic.py', '16f84012af'],
                  ['btc_generator_basic.py', '25aefce8b3'],
                  ['btc_generator_basic.py', '9e099a5c73'],
                  ['btc_generator_basic.py', 'c0dcc0be82'],
                  ['btc_generator_basic.py', '52a5d129d6'],
                  ['btc_generator_basic.py', 'fbadafd257'],
                  ['btc_generator_basic.py', 'fc0846fd41'],
                  ['btc_generator_basic.py', 'f814f17537'],
                  ['btc_generator_basic.py', '91061a6135']]

    ylds = []
    for argv in yield_argv:
        log.debug('launchYield: {argv}', argv=argv)
        ylds.append(build_yld(argv))

    return ylds


def buildTumbler():
    tumblr_argv = ['tumbler.py', '-N', '2', '0', '-a', '0', '-M', '5',
                    '-w', '3', '-l', '0.2', '-s', '100000000', '59bf49a',
                    'mhyGR4qBKDWoCdFZuzoSyVeCrphtPXtbgD']

    _tumblr_argv = [['tumbler.py', '-N', '2', '0', '-a', '0', '-M', '5', '-w',
                    '3', '-l', '0.2', '-s', '100000000', 'cf',
                    'mrGCJVbgkmv2TWBNYFjZjTqQYyNuY1REEP'],
                   ['tumbler.py', '-N', '2', '0', '-a', '0', '-M', '5', '-w',
                    '3', '-l', '0.2', '-s', '100000000', '8a',
                    'miVLMMtgNyzC9P8jBWvnRdJs5UrxHkihUn']]

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
        reactor.callWhenRunning(launch, ylds)
        reactor.callLater(30, launch, tumblers)
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
