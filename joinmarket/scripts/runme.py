#! /usr/bin/env python
from __future__ import absolute_import, print_function

from twisted.internet import reactor
from twisted.logger import Logger

"""Some helper functions for testing"""

import os

# from .commontest import make_wallets

from joinmarket.core.test.commontest import make_wallets

from joinmarket.yield_generator_basic import build_objects as btc_gen_build
from joinmarket.sendpayment import build_objects as sendpay_build

from joinmarket.core import jmbtc

log = Logger()
"""
Just some random thoughts to motivate possible tests;
almost none of this has really been done:

Expectations
1. Any bot should run indefinitely irrespective of the input
messages it receives, except bots which perform a finite action

2. A bot must never spend an unacceptably high transaction fee.

3. A bot must explicitly reject interactions with another bot not
respecting the JoinMarket protocol for its version.

4. Bots must never send bitcoin data in the clear over the wire.
"""


def main():
    # create 2 new random wallets.
    # put 10 coins into the first receive address
    # to allow that bot to start.
    wallets = make_wallets(
            2, wallet_structures=[[1, 0, 0, 0, 0], [1, 0, 0, 0, 0]],
            mean_amt=10)

    n = m = 2
    # for yield generator with wallet1
    argv = ['yield-gen-bas-test.py', str(wallets[0]['seed'])]
    btc_inst, _, _ = btc_gen_build(argv)

    btc_inst.build_irc()  # right away

    # run a single sendpayment call with wallet2
    amt = n * 100000000  # in satoshis
    dest_address = jmbtc.privkey_to_address(os.urandom(32))

    argv = ['sendpayment.py', '--yes', '-N', '1', wallets[1]['seed'],
            str(amt), dest_address]
    send_inst, _, _ = sendpay_build(argv)

    reactor.callLater(10, send_inst.build_irc)
    log.debug('all built')


reactor.callWhenRunning(main)

reactor.run()
log.debug('done')
