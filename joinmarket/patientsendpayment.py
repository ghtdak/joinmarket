from __future__ import absolute_import, print_function

import sys
from datetime import timedelta
from optparse import OptionParser

from twisted.internet import reactor
from twisted.logger import Logger

import joinmarket as jm

log = Logger()


class PatientSendPayment(jm.Maker, jm.Taker):
    def __init__(self, msgchan, wallet, destaddr, amount, makercount, txfee,
                 cjfee, waittime, mixdepth):
        self.destaddr = destaddr
        self.amount = amount
        self.makercount = makercount
        self.txfee = txfee
        self.cjfee = cjfee
        self.waittime = waittime
        self.mixdepth = mixdepth
        jm.Maker.__init__(self, msgchan, wallet)
        jm.Taker.__init__(self, msgchan)

        self.daemon = True
        self.finished = False

    def finishcallback(self, coinjointx):
        # todo: likely broken with different design
        self.tmaker.msgchan.shutdown()

    def start(self):
        # TODO this thread doesnt wake up for what could be hours
        # need a loop that periodically checks self.finished
        # TODO another issue is, what if the bot has run out of utxos and
        # needs to wait for some tx to confirm before it can trade
        # presumably it needs to wait here until the tx confirms
        if self.finished:
            return
        print('giving up waiting')
        # cancel the remaining order
        self.tmaker.modify_orders([0], [])
        orders, total_cj_fee = self.choose_orders(
                self.tmaker.db, self.tmaker.amount, self.tmaker.makercount,
                self.weighted_order_choose)
        print('chosen orders to fill ' + str(orders) + ' totalcjfee=' + str(
                total_cj_fee))
        total_amount = self.tmaker.amount + total_cj_fee + self.tmaker.txfee
        print('total amount spent = ' + str(total_amount))

        utxos = self.tmaker.wallet.select_utxos(self.tmaker.mixdepth,
                                                total_amount)
        self.tmaker.start_cj(
            self.tmaker.wallet, self.tmaker.amount, orders, utxos,
            self.tmaker.destaddr,
            self.tmaker.wallet.get_change_addr(self.tmaker.mixdepth),
            self.tmaker.txfee, self.finishcallback)


    def get_crypto_box_from_nick(self, nick):
        if self.cjtx:
            return jm.Taker.get_crypto_box_from_nick(self, nick)
        else:
            return jm.Maker.get_crypto_box_from_nick(self, nick)

    def on_welcome(self):
        jm.Maker.on_welcome(self)
        jm.Taker.on_welcome(self)
        reactor.callLater(self.tmaker.waittime, self.start)

    def create_my_orders(self):
        # choose an absolute fee order to discourage people from
        # mixing smaller amounts
        order = {'oid': 0,
                 'ordertype': 'absorder',
                 'minsize': 0,
                 'maxsize': self.amount,
                 'txfee': self.txfee,
                 'cjfee': self.cjfee}
        return [order]

    def oid_to_order(self, cjorder, oid, amount):
        # TODO race condition (kinda)
        # if an order arrives and before it finishes another order arrives
        # its possible this bot will end up paying to the destaddr more than it
        # intended
        utxos = self.wallet.select_utxos(self.mixdepth, amount)
        return utxos, self.destaddr, self.wallet.get_change_addr(self.mixdepth)

    def on_tx_unconfirmed(self, cjorder, balance, removed_utxos):
        self.amount -= cjorder.cj_amount
        if self.amount == 0:
            self.takerthread.finished = True
            print('finished sending, exiting..')
            self.msgchan.shutdown()
            return [], []
        available_balance = self.wallet.get_balance_by_mixdepth()[self.mixdepth]
        if available_balance >= self.amount:
            order = {'oid': 0,
                     'ordertype': 'absorder',
                     'minsize': 0,
                     'maxsize': self.amount,
                     'txfee': self.txfee,
                     'cjfee': self.cjfee}
            return [], [order]
        else:
            log.debug('not enough money left, have to wait until tx confirms')
            return [0], []

    def on_tx_confirmed(self, cjorder, confirmations, txid):
        if len(self.orderlist) == 0:
            order = {'oid': 0,
                     'ordertype': 'absorder',
                     'minsize': 0,
                     'maxsize': self.amount,
                     'txfee': self.txfee,
                     'cjfee': self.cjfee}
            return [], [order]
        else:
            return [], []


def build_objects(argv=None):
    if argv is None:
        argv = sys.argv

    parser = OptionParser(
        usage=('usage: %prog [options] [wallet file / fromaccount] [amount] ['
               'destaddr]'),
        description=('Sends a payment from your wallet to an given address '
                     'using coinjoin. First acts as a maker, announcing an '
                     'order and waiting for someone to fill it. After a set '
                     'period of time, gives up waiting and acts as a taker '
                     'and coinjoins any remaining coins'))
    parser.add_option(
            '-f',
            '--txfee',
            action='store',
            type='int',
            dest='txfee',
            default=10000,
            help='miner fee contribution, in satoshis, default=10000')
    parser.add_option(
            '-N',
            '--makercount',
            action='store',
            type='int',
            dest='makercount',
            help=
            'how many makers to coinjoin with when taking liquidity, default=2',
            default=2)
    parser.add_option(
            '-w',
            '--wait-time',
            action='store',
            type='float',
            dest='waittime',
            help=('wait time in hours as a maker before becoming a taker, '
                  'default=8'),
            default=8)
    parser.add_option(
            '-c',
            '--cjfee',
            action='store',
            type='int',
            dest='cjfee',
            help=('coinjoin fee asked for when being a maker, in satoshis per '
                  'order filled, default=50000'),
            default=50000)
    parser.add_option(
            '-m',
            '--mixdepth',
            action='store',
            type='int',
            dest='mixdepth',
            help='mixing depth to spend from, default=0',
            default=0)
    parser.add_option(
            '--rpcwallet',
            action='store_true',
            dest='userpcwallet',
            default=False,
            help=('Use the Bitcoin Core wallet through json rpc, instead of '
                  'the internal joinmarket wallet. Requires '
                  'blockchain_source=json-rpc'))
    (options, args) = parser.parse_args(argv[1:])

    if len(args) < 3:
        parser.error('Needs a wallet, amount and destination address')
        sys.exit(0)
    wallet_name = args[0]
    amount = int(args[1])
    destaddr = args[2]

    addr_valid, errormsg = jm.validate_address(destaddr)
    if not addr_valid:
        print('ERROR: Address invalid. ' + errormsg)
        return

    waittime = timedelta(hours=options.waittime).total_seconds()
    print('txfee=%d cjfee=%d waittime=%s makercount=%d' % (
        options.txfee, options.cjfee, str(timedelta(hours=options.waittime)),
        options.makercount))

    # todo: this section doesn't make a lot of sense
    if not options.userpcwallet:
        wallet = jm.Wallet(wallet_name, options.mixdepth + 1)
    else:
        print('not implemented yet')
        sys.exit(0)
    # wallet = BitcoinCoreWallet(fromaccount=wallet_name)
    wallet.sync_wallet()

    available_balance = wallet.get_balance_by_mixdepth()[options.mixdepth]
    if available_balance < amount:
        print('not enough money at mixdepth=%d, exiting' % options.mixdepth)
        return

    nickname = jm.random_nick()

    block_instance = jm.BlockInstance(nickname)

    log.debug('Running patient sender of a payment')

    psp = PatientSendPayment(
            block_instance, wallet, destaddr, amount, options.makercount,
            options.txfee, options.cjfee, waittime, options.mixdepth)

    return block_instance, psp
