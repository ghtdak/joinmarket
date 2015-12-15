#! /usr/bin/env python
from __future__ import absolute_import, print_function

import sys
from optparse import OptionParser

from twisted.logger import Logger

from twisted.internet import reactor

import joinmarket as jm

log = Logger()


def check_high_fee(total_fee_pc):
    WARNING_THRESHOLD = 0.02  # 2%
    if total_fee_pc > WARNING_THRESHOLD:
        log.info('\n'.join(['=' * 60] * 3))
        log.info('WARNING   ' * 6)
        log.info('\n'.join(['=' * 60] * 1))
        log.info('OFFERED COINJOIN FEE IS UNUSUALLY HIGH. DOUBLE/TRIPLE CHECK.')
        log.info('\n'.join(['=' * 60] * 1))
        log.info('WARNING   ' * 6)
        log.info('\n'.join(['=' * 60] * 3))


class SendPayment(jm.Taker):

    def __init__(self, block_instance, wallet, destaddr, amount, makercount,
                 txfee, waittime, mixdepth, answeryes, chooseOrdersFunc):
        super(SendPayment, self).__init__(block_instance)
        self.wallet = wallet
        self.destaddr = destaddr
        self.amount = amount
        self.makercount = makercount
        self.txfee = txfee
        self.waittime = waittime
        self.mixdepth = mixdepth
        self.answeryes = answeryes
        self.chooseOrdersFunc = chooseOrdersFunc
        self.daemon = True
        self.ignored_makers = []

    # -------------------------------------------------------------------
    # takerSibling section - was a separate object in a previous life
    # includes callbacks
    # -------------------------------------------------------------------

    def create_tx(self):
        log.debug('sendpayment: create_tx called')
        crow = self.db.execute('SELECT COUNT(DISTINCT counterparty) FROM '
                                     'orderbook;').fetchone()
        # log.debug('counterparty counting: {}'.format(crow))
        counterparty_count = crow['COUNT(DISTINCT counterparty)']
        counterparty_count -= len(self.ignored_makers)
        if counterparty_count < self.makercount:
            log.info('{:d} of {:d} not enough counterparties to fill order, '
                     'ending'.format(counterparty_count, self.makercount))
            # self.msgchan.shutdown(0)
            return

        change_addr = None
        if self.amount == 0:
            utxos = self.wallet.get_utxos_by_mixdepth()[
                self.mixdepth]
            total_value = sum([va['value'] for va in utxos.values()])

            orders, cjamount = jm.choose_sweep_orders(
                self.db, total_value, self.txfee,
                self.makercount, self.chooseOrdersFunc,
                self.ignored_makers)

            if not self.answeryes:
                total_cj_fee = total_value - cjamount - self.txfee
                log.debug('total cj fee = ' + str(total_cj_fee))
                total_fee_pc = 1.0 * total_cj_fee / cjamount
                log.debug('total coinjoin fee = ' + str(float('%.3g' % (
                    100.0 * total_fee_pc))) + '%')
                check_high_fee(total_fee_pc)
                if raw_input('send with these orders? (y/n):')[0] != 'y':
                    self.msgchan.shutdown(0)
                    return
        else:
            orders, total_cj_fee = self.sendpayment_choose_orders(
                self.amount, self.makercount)
            if not orders:
                log.debug('ERROR not enough liquidity in the orderbook, '
                          'exiting')
                return
            total_amount = self.amount + total_cj_fee + self.txfee
            log.info('total amount spent = ' + str(total_amount))
            utxos = self.wallet.select_utxos(self.mixdepth,
                                                   total_amount)
            cjamount = self.amount
            change_addr = self.wallet.get_change_addr(self.mixdepth)

        # self.start_cj(self.wallet, cjamount, orders, utxos,
        #                     self.destaddr, change_addr, self.txfee,
        #                     self.finishcallback, choose_orders_recover)
        # instead, do...

        jm.CoinJoinTX(self, cjamount, orders, utxos, self.destaddr,
                      change_addr, self.txfee)

    def finishcallback(self, coinjointx):
        # log.debug('sendpayment->finishcallback: {}...'.format(
        #         str(coinjointx)[:20]))
        if coinjointx.all_responded:
            coinjointx.self_sign_and_push()
            log.debug('created fully signed tx, ending')
            # self.msgchan.shutdown(0)
            return
        self.ignored_makers += coinjointx.nonrespondants
        log.debug('recreating the tx, ignored_makers='.format(
            self.ignored_makers))
        reactor.callLater(2.0, self.create_tx)

    def sendpayment_choose_orders(self, cj_amount, makercount,
                                  nonrespondants=None, active_nicks=None):

        if nonrespondants is None:
            nonrespondants = []
        if active_nicks is None:
            active_nicks = []

        self.ignored_makers += nonrespondants

        orders, total_cj_fee = jm.choose_orders(
            self.db, cj_amount, makercount, self.chooseOrdersFunc,
            self.ignored_makers + active_nicks)

        if not orders:
            return None, 0

        # log.info('chosen orders to fill {} totalcjfee={}'.format(orders,
        #                                                          total_cj_fee))

        if not self.answeryes:
            if len(self.ignored_makers) > 0:
                noun = 'total'
            else:
                noun = 'additional'

            total_fee_pc = 1.0 * total_cj_fee / cj_amount
            log.debug(noun + ' coinjoin fee = ' + str(float('%.3g' % (
                100.0 * total_fee_pc))) + '%')
            check_high_fee(total_fee_pc)

            if raw_input('send with these orders? (y/n):')[0] != 'y':
                log.debug('ending')
                self.msgchan.shutdown(0)
                return None, -1

        return orders, total_cj_fee

    # for callback
    choose_orders_recover = sendpayment_choose_orders

    # --------------------------------------------------------
    # taker stuff
    # --------------------------------------------------------

    def on_welcome(self):
        log.debug('on_welcome')
        super(SendPayment, self).on_welcome()
        # todo: self.waittime seemed too short. ???
        # reactor.callLater(self.waittime, self.create_tx)
        reactor.callLater(self.waittime, self.create_tx)


def build_objects(argv=None):
    if argv is None:
        argv = sys.argv

    parser = OptionParser(
            usage=('usage: %prog [options] [wallet file / fromaccount] '
                   '[amount] [destaddr]'),
            description=('Sends a single payment from a given mixing depth of '
                         'your wallet to an given address using coinjoin and '
                         'then switches off. Also sends from bitcoinqt. '
                         'Setting amount to zero will do a sweep, where the '
                         'entire mix depth is emptied'))
    parser.add_option(
            '-f',
            '--txfee',
            action='store',
            type='int',
            dest='txfee',
            default=10000,
            help='total miner fee in satoshis, default=10000')
    parser.add_option(
            '-w',
            '--wait-time',
            action='store',
            type='float',
            dest='waittime',
            help='wait time in seconds to allow orders to arrive, default=5',
            default=5)
    parser.add_option(
            '-N',
            '--makercount',
            action='store',
            type='int',
            dest='makercount',
            help='how many makers to coinjoin with, default=2',
            default=2)
    parser.add_option(
            '-C',
            '--choose-cheapest',
            action='store_true',
            dest='choosecheapest',
            default=False,
            help='override weightened offers picking and choose cheapest')
    parser.add_option(
            '-P',
            '--pick-orders',
            action='store_true',
            dest='pickorders',
            default=False,
            help=('manually pick which orders to take. doesn\'t work '
                  'while sweeping.'))
    parser.add_option(
            '-m',
            '--mixdepth',
            action='store',
            type='int',
            dest='mixdepth',
            help='mixing depth to spend from, default=0',
            default=0)
    parser.add_option(
            '--yes',
            action='store_true',
            dest='answeryes',
            default=False,
            help='answer yes to everything')
    parser.add_option(
            '--rpcwallet',
            action='store_true',
            dest='userpcwallet',
            default=False,
            help=('Use the Bitcoin Core wallet through json rpc, instead '
                  'of the internal joinmarket wallet. Requires '
                  'blockchain_source=json-rpc'))

    (options, args) = parser.parse_args(argv[1:])

    if len(args) < 3:
        parser.error('Needs a wallet, amount and destination address')
        sys.exit(0)
    wallet_name = args[0]
    amount = int(args[1])
    destaddr = args[2]

    # load_program_config()

    nickname = jm.random_nick()

    block_instance = jm.BlockInstance(nickname)

    addr_valid, errormsg = jm.validate_address(destaddr)
    if not addr_valid:
        log.info('ERROR: Address invalid. ' + errormsg)
        return

    if options.pickorders and amount != 0:  # cant use for sweeping
        chooseOrdersFunc = jm.pick_order
    elif options.choosecheapest:
        chooseOrdersFunc = jm.cheapest_order_choose
    else:  # choose randomly (weighted)
        chooseOrdersFunc = jm.weighted_order_choose

    log.debug('starting sendpayment')

    if not options.userpcwallet:
        wallet = jm.Wallet(wallet_name, options.mixdepth + 1)
    else:
        wallet = jm.BitcoinCoreWallet(fromaccount=wallet_name)
    jm.bc_interface.sync_wallet(wallet)

    taker = SendPayment(block_instance, wallet, destaddr, amount,
                        options.makercount, options.txfee, options.waittime,
                        options.mixdepth, options.answeryes, chooseOrdersFunc)
    return block_instance, taker, wallet
