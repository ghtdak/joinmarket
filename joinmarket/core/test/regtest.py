#! /usr/bin/env python
from __future__ import absolute_import

from twisted.internet import reactor
from twisted.logger import Logger

"""Some helper functions for testing"""

import os
import subprocess
import time
import unittest

from .commontest import local_command, make_wallets

from joinmarket.yield_generator_basic import build_objects as yield_gen_build
from joinmarket.sendpayment import build_objects as sendpay_build

import bitcoin as btc

import joinmarket as jm

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


class Join2PTests(unittest.TestCase):
    """
    This test case intends to simulate
    a single join with a single counterparty. In that sense,
    it's not realistic, because nobody (should) do joins with only 1 maker,
    but this test has the virtue of being the simplest possible thing
    that JoinMarket can do.
    """

    def setUp(self):
        #create 2 new random wallets.
        #put 10 coins into the first receive address
        #to allow that bot to start.
        self.wallets = make_wallets(2,
            wallet_structures=[[1, 0, 0, 0, 0],
                               [1, 0, 0, 0, 0]],
            mean_amt=10)

    def run_simple_send(self, n, m):

        # for yield generator with wallet1
        argv = ['yield-gen-bas-test.py', str(self.wallets[0]['seed'])]
        btc_inst, _, _ = yield_gen_build(argv)

        btc_inst.build_irc()

        #run a single sendpayment call with wallet2
        amt = n * 100000000  #in satoshis
        dest_address = btc.privkey_to_address(os.urandom(32),
                                              jm.get_p2pk_vbyte())

        argv = ['sendpayment.py', '--yes', '-N', '1', self.wallets[1]['seed'],
                str(amt), dest_address]
        send_inst, _, _ = sendpay_build(argv)

        reactor.callLater(10, send_inst.build_irc)
        reactor.run()

        received = jm.bc_interface.get_received_by_addr(
            [dest_address], None)['data'][0]['balance']
        if received != amt * m:
            log.debug('received was: ' + str(received) + ' but amount was: ' +
                      str(amt))
            return False
        return True

    def test_simple_send(self):
        self.failUnless(self.run_simple_send(2, 2))

@unittest.skip("skipping")
class JoinNPTests(unittest.TestCase):

    def setUp(self):
        self.n = 2
        #create n+1 new random wallets.
        #put 10 coins into the first receive address
        #to allow that bot to start.
        wallet_structures = [[1, 0, 0, 0, 0]] * 3
        self.wallets = make_wallets(3,
                                    wallet_structures=wallet_structures,
                                    mean_amt=10)
        #the sender is wallet (n+1), i.e. index wallets[n]

    def test_n_partySend(self):
        self.failUnless(self.run_nparty_join())

    def run_nparty_join(self):
        yigen_procs = []
        for i in range(self.n):
            ygp = local_command(
                ['python', 'yield-gen-bas-test.py',
                 str(self.wallets[i]['seed'])],
                bg=True)
            time.sleep(2)  #give it a chance
            yigen_procs.append(ygp)

        #A significant delay is needed to wait for the yield generators to sync
        time.sleep(60)

        #run a single sendpayment call
        amt = 100000000  #in satoshis
        dest_address = btc.privkey_to_address(os.urandom(32), jm.get_p2pk_vbyte())
        try:
            sp_proc = local_command(['python', 'sendpayment.py', '--yes', '-N',
                                     str(self.n), self.wallets[self.n][
                                         'seed'], str(amt), dest_address])
        except subprocess.CalledProcessError, e:
            for ygp in yigen_procs:
                ygp.kill()
            print e.returncode
            print e.message
            raise

        if any(yigen_procs):
            for ygp in yigen_procs:
                ygp.kill()

        received = jm.bc_interface.get_received_by_addr(
            [dest_address], None)['data'][0]['balance']
        if received != amt:
            return False
        return True


def main():
    # os.chdir(data_dir)
    unittest.main()


if __name__ == '__main__':
    main()
