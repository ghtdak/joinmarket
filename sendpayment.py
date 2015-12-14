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


# callback sibling which does the buy-side algorithm
# chooses which coinjoins to initiate and when
class PaymentThread(jm.TakerSibling):

    def __init__(self, taker):
        super(PaymentThread, self).__init__(taker)
        self.daemon = True
        self.ignored_makers = []

    def start(self):
        # reactor.callLater(self.taker.waittime, self.create_tx)
        reactor.callLater(15, self.create_tx)

    def create_tx(self):
        log.debug('sendpayment: create_tx called')
        crow = self.taker.db.execute('SELECT COUNT(DISTINCT counterparty) FROM '
                                     'orderbook;').fetchone()
        # log.debug('counterparty counting: {}'.format(crow))
        counterparty_count = crow['COUNT(DISTINCT counterparty)']
        counterparty_count -= len(self.ignored_makers)
        if counterparty_count < self.taker.makercount:
            log.info('{:d} of {:d} not enough counterparties to fill order, '
                     'ending'.format(counterparty_count, self.taker.makercount))
            # self.taker.msgchan.shutdown(0)
            return

        change_addr = None
        if self.taker.amount == 0:
            utxos = self.taker.wallet.get_utxos_by_mixdepth()[
                self.taker.mixdepth]
            total_value = sum([va['value'] for va in utxos.values()])

            orders, cjamount = jm.choose_sweep_orders(
                self.taker.db, total_value, self.taker.txfee,
                self.taker.makercount, self.taker.chooseOrdersFunc,
                self.ignored_makers)

            if not self.taker.answeryes:
                total_cj_fee = total_value - cjamount - self.taker.txfee
                log.debug('total cj fee = ' + str(total_cj_fee))
                total_fee_pc = 1.0 * total_cj_fee / cjamount
                log.debug('total coinjoin fee = ' + str(float('%.3g' % (
                    100.0 * total_fee_pc))) + '%')
                check_high_fee(total_fee_pc)
                if raw_input('send with these orders? (y/n):')[0] != 'y':
                    self.taker.msgchan.shutdown(0)
                    return
        else:
            orders, total_cj_fee = self.sendpayment_choose_orders(
                self.taker.amount, self.taker.makercount)
            if not orders:
                log.debug('ERROR not enough liquidity in the orderbook, '
                          'exiting')
                return
            total_amount = self.taker.amount + total_cj_fee + self.taker.txfee
            log.info('total amount spent = ' + str(total_amount))
            utxos = self.taker.wallet.select_utxos(self.taker.mixdepth,
                                                   total_amount)
            cjamount = self.taker.amount
            change_addr = self.taker.wallet.get_change_addr(self.taker.mixdepth)

        # self.taker.start_cj(self.taker.wallet, cjamount, orders, utxos,
        #                     self.taker.destaddr, change_addr, self.taker.txfee,
        #                     self.finishcallback, choose_orders_recover)
        # instead, do...

        jm.CoinJoinTX(self, cjamount, orders, utxos, self.taker.destaddr,
                      change_addr, self.taker.txfee)

    def finishcallback(self, coinjointx):
        # log.debug('sendpayment->finishcallback: {}...'.format(
        #         str(coinjointx)[:20]))
        if coinjointx.all_responded:
            coinjointx.self_sign_and_push()
            log.debug('created fully signed tx, ending')
            # self.taker.msgchan.shutdown(0)
            return
        self.ignored_makers += coinjointx.nonrespondants
        log.debug('recreating the tx, ignored_makers='.format(
            self.ignored_makers))
        reactor.callLater(2.0, self.create_tx)

    def sendpayment_choose_orders(self,
                                  cj_amount,
                                  makercount,
                                  nonrespondants=None,
                                  active_nicks=None):

        if nonrespondants is None:
            nonrespondants = []
        if active_nicks is None:
            active_nicks = []

        self.ignored_makers += nonrespondants

        orders, total_cj_fee = jm.choose_orders(
            self.taker.db, cj_amount, makercount, self.taker.chooseOrdersFunc,
            self.ignored_makers + active_nicks)

        if not orders:
            return None, 0

        # log.info('chosen orders to fill {} totalcjfee={}'.format(orders,
        #                                                          total_cj_fee))

        if not self.taker.answeryes:
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
                self.taker.msgchan.shutdown(0)
                return None, -1

        return orders, total_cj_fee


class SendPayment(jm.Taker):

    def __init__(self, binst, msgchan, wallet, destaddr, amount, makercount,
                 txfee, waittime, mixdepth, answeryes, chooseOrdersFunc):
        super(SendPayment, self).__init__(binst, msgchan)
        self.wallet = wallet
        self.destaddr = destaddr
        self.amount = amount
        self.makercount = makercount
        self.txfee = txfee
        self.waittime = waittime
        self.mixdepth = mixdepth
        self.answeryes = answeryes
        self.chooseOrdersFunc = chooseOrdersFunc
        self.taker_sibling = PaymentThread(self)

    def on_welcome(self):
        jm.Taker.on_welcome(self)
        log.debug('on_welcome: {}'.format(__name__))
        self.taker_sibling.start()


def build_objects(argv=sys.argv):
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

    (options, args) = parser.parse_args(argv)

    if len(args) < 3:
        parser.error('Needs a wallet, amount and destination address')
        sys.exit(0)
    wallet_name = args[0]
    amount = int(args[1])
    destaddr = args[2]

    # load_program_config()

    block_inst = jm.BlockInstance()

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

    block_inst.nickname = nick = jm.random_nick()
    jm.nick_logging(nick)

    log.debug('starting sendpayment')

    if not options.userpcwallet:
        wallet = jm.Wallet(block_inst, wallet_name, options.mixdepth + 1)
    else:
        wallet = jm.BitcoinCoreWallet(block_inst, fromaccount=wallet_name)
    block_inst.get_bci().sync_wallet(wallet)

    irc = jm.build_irc_communicator(block_inst.nickname)

    taker = SendPayment(block_inst, irc, wallet, destaddr, amount,
                        options.makercount, options.txfee, options.waittime,
                        options.mixdepth, options.answeryes, chooseOrdersFunc)
    return taker, wallet

def run_reactor(taker, wallet):
    try:
        log.debug('starting irc')
        reactor.run()
    except:
        log.debug('CRASHING, DUMPING EVERYTHING')
        jm.debug_dump_object(wallet, ['addr_cache', 'keys', 'wallet_name',
                                      'seed'])
        jm.debug_dump_object(taker)
        import traceback
        log.debug(traceback.format_exc())


if __name__ == "__main__":
    taker, wallet = build_objects()
    run_reactor(taker, wallet)
    log.info('sendpayment: done')
    sys.exit(0)
