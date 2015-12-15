from __future__ import absolute_import, print_function

import datetime
import os
import sys
import time
import cPickle as pickle

from twisted.logger import Logger
from twisted.internet import reactor

import bitcoin
import joinmarket as jm
from joinmarket.jsonrpc import tb_stack_dd
from joinmarket.test.commontest import make_wallets
from .sendpayment import build_objects as sender_build

# from joinmarket.jsonrpc import tb_stack_set

txfee = 1000
cjfee = '0.002'  # 0.2% fee

# minimum size is such that you always net profit at least 20% of the miner fee
minsize = int(1.2 * txfee / float(cjfee))

mix_levels = 5

log = Logger()


class YieldGenerator(jm.Maker):
    statement_file = os.path.join('logs', 'yigen-statement.csv')

    def __init__(self, block_instance, wallet):
        super(YieldGenerator, self).__init__(block_instance, wallet)
        self.tx_unconfirm_timestamp = {}
        self.income_statement = None
        self.block_instance = block_instance

    def log_statement(self, data):
        if jm.get_network() == 'testnet':
            return

        data = [str(d) for d in data]
        self.income_statement = open(self.statement_file, 'a')
        self.income_statement.write(','.join(data) + '\n')
        self.income_statement.close()

    def on_welcome(self):
        log.debug('on_welcome')
        jm.Maker.on_welcome(self)
        if not os.path.isfile(self.statement_file):
            self.log_statement(
                ['timestamp', 'cj amount/satoshi', 'my input count',
                 'my input value/satoshi', 'cjfee/satoshi', 'earned/satoshi',
                 'confirm time/min', 'notes'])

        timestamp = datetime.datetime.now().strftime("%Y/%m/%d %H:%M:%S")
        self.log_statement([timestamp, '', '', '', '', '', '', 'Connected'])

    def create_my_orders(self):
        mix_balance = self.wallet.get_balance_by_mixdepth()
        if len([b for m, b in mix_balance.iteritems() if b > 0]) == 0:
            log.debug('No coins left!!!')
            return []

        # print mix_balance
        max_mix = max(mix_balance, key=mix_balance.get)
        order = {'oid': 0,
                 'ordertype': 'relorder',
                 'minsize': minsize,
                 'maxsize': (mix_balance[max_mix] - jm.DUST_THRESHOLD),
                 'txfee': txfee,
                 'cjfee': cjfee}
        return [order]

    def oid_to_order(self, cjorder, oid, amount):
        mix_balance = self.wallet.get_balance_by_mixdepth()
        max_mix = max(mix_balance, key=mix_balance.get)

        # algo attempts to make the largest-balance mixing depth get an even
        # larger balance
        log.debug('finding suitable mixdepth')
        mixdepth = (max_mix - 1) % self.wallet.max_mix_depth
        while True:
            if mixdepth in mix_balance and mix_balance[mixdepth] >= amount:
                break
            mixdepth = (mixdepth - 1) % self.wallet.max_mix_depth
        # mixdepth is the chosen depth we'll be spending from
        cj_addr = self.wallet.get_receive_addr((mixdepth + 1) %
                                               self.wallet.max_mix_depth)
        change_addr = self.wallet.get_change_addr(mixdepth)

        utxos = self.wallet.select_utxos(mixdepth, amount)
        my_total_in = sum([va['value'] for va in utxos.values()])
        real_cjfee = jm.calc_cj_fee(cjorder.ordertype, cjorder.cjfee, amount)
        change_value = my_total_in - amount - cjorder.txfee + real_cjfee
        if change_value <= jm.DUST_THRESHOLD:
            log.debug(('change value={} below dust threshold, '
                       'finding new utxos').format(change_value))
            try:
                utxos = self.wallet.select_utxos(
                    mixdepth, amount + self.block_instance.DUST_THRESHOLD)
            except Exception:
                log.debug('dont have the required UTXOs to make a '
                          'output above the dust threshold, quitting')
                return None, None, None

        return utxos, cj_addr, change_addr

    def on_tx_unconfirmed(self, cjorder, txid, removed_utxos):
        self.tx_unconfirm_timestamp[cjorder.cj_addr] = int(time.time())
        # if the balance of the highest-balance mixing depth change then
        # reannounce it
        oldorder = self.orderlist[0] if len(self.orderlist) > 0 else None
        neworders = self.create_my_orders()
        if len(neworders) == 0:
            return [0], []  # cancel old order
        # oldorder may not exist when this is called from on_tx_confirmed
        if oldorder:
            if oldorder['maxsize'] == neworders[0]['maxsize']:
                return [], []  # change nothing
        # announce new order, replacing the old order
        return [], [neworders[0]]

    def on_tx_confirmed(self, cjorder, confirmations, txid):
        if cjorder.cj_addr in self.tx_unconfirm_timestamp:
            confirm_time = int(time.time()) - self.tx_unconfirm_timestamp[
                cjorder.cj_addr]
        else:
            confirm_time = 0
        timestamp = datetime.datetime.now().strftime("%Y/%m/%d %H:%M:%S")
        self.log_statement([timestamp, cjorder.cj_amount, len(
            cjorder.utxos), sum([av['value'] for av in cjorder.utxos.values(
            )]), cjorder.real_cjfee, cjorder.real_cjfee - cjorder.txfee, round(
                confirm_time / 60.0, 2), ''])
        return self.on_tx_unconfirmed(cjorder, txid, None)


class Monitor(object):

    def __init__(self, delay):
        self.callgraph = None
        reactor.callLater(delay, self.pickleStats)

    def pickleStats(self):
        with open('logs/callstats.pickle', 'wb') as f:
            pickle.dump(tb_stack_dd, f, -1)
        log.debug('callgraph pickle dumped')


monitor = Monitor(120)


def build_objects(argv=None):
    if argv is None:
        argv = sys.argv

    # def calltrace():
    #     for t in tb_stack_set:
    #         log.debug(str(t))
    #
    # reactor.callLater(120, calltrace)

    realname = 'btcint=' + jm.config.get("BLOCKCHAIN", "blockchain_source")
    nickname = jm.random_nick()
    password = 'nimDid[Quoc6'

    block_instance = jm.BlockInstance(nickname,
                                      realname=realname,
                                      password=password)

    # todo: for testing... remove me!!

    if isinstance(jm.bc_interface, jm.BlockrInterface):
        c = ('\nYou are running a yield generator by polling the blockr.io '
             'website. This is quite bad for privacy. That site is owned by '
             'coinbase.com Also your bot will run faster and more efficently, '
             'you can be immediately notified of new bitcoin network '
             'information so your money will be working for you as hard as '
             'possibleLearn how to setup JoinMarket with Bitcoin Core: '
             'https://github.com/chris-belcher/joinmarket/wiki/Running'
             '-JoinMarket-with-Bitcoin-Core-full-node')
        print(c)
        ret = raw_input('\nContinue? (y/n):')
        if ret[0] != 'y':
            return

    # --------------------------------------------------------
    # Testing Infrastructure

    #create 2 new random wallets.
    #put 10 coins into the first receive address
    #to allow that bot to start.
    def build_otherguys():
        wallets = make_wallets(2,
                               wallet_structures=[[1, 0, 0, 0, 0],
                                                  [1, 0, 0, 0, 0]],
                               mean_amt=10)

        #run a single sendpayment call with wallet2
        n = m = 2
        amt = n * 100000000  #in satoshis
        dest_address = bitcoin.privkey_to_address(
            os.urandom(32), jm.get_p2pk_vbyte())

        for _ in range(m):
            sender_args = ['--yes', '-N', '1', str(wallets[1]['seed']),
                           str(amt), dest_address]
            log.debug('constructing sender, args: {}'.format(
                    ' '.join(sender_args)))
            bi, t, w = sender_build(sender_args)

            # the necessary delay as per test/regtest.py
            reactor.callLater(30, bi.build_irc)

        return wallets

    # wallets = build_otherguys()
    # fakeseed = str(wallets[0]['seed'])

    # -----------------------------------------------------

    seed = argv[1]

    wallet = jm.Wallet(seed, max_mix_depth=mix_levels)

    jm.bc_interface.sync_wallet(wallet)

    log.debug('starting yield generator')

    maker = YieldGenerator(block_instance, wallet)

    return block_instance, maker, wallet

