#! /usr/bin/env python
from __future__ import absolute_import, print_function

import sys
from optparse import OptionParser

from twisted.logger import Logger

from twisted.internet import reactor

import bitcoin as btc
import joinmarket as jm
from joinmarket.sendpayment import check_high_fee


log = Logger()


# thread which does the buy-side algorithm
# chooses which coinjoins to initiate and when
class PaymentThread(object):

    def __init__(self, taker):
        self.daemon = True
        self.taker = taker
        self.ignored_makers = []

    def create_tx(self):
        crow = self.taker.db.execute(
            'SELECT COUNT(DISTINCT counterparty) FROM orderbook;').fetchone()

        counterparty_count = crow['COUNT(DISTINCT counterparty)']
        counterparty_count -= len(self.ignored_makers)
        if counterparty_count < self.taker.options.makercount:
            log.info('not enough counterparties to fill order, ending')
            self.taker.msgchan.shutdown()
            return

        utxos = self.taker.utxo_data
        change_addr = None
        choose_orders_recover = None
        if self.taker.cjamount == 0:
            total_value = sum([va['value'] for va in utxos.values()])
            orders, cjamount = jm.choose_sweep_orders(
                self.taker.db, total_value, self.taker.options.txfee,
                self.taker.options.makercount, self.taker.chooseOrdersFunc,
                self.ignored_makers)
            if not self.taker.options.answeryes:
                total_cj_fee = total_value - cjamount - self.taker.options.txfee
                log.debug('total cj fee = ' + str(total_cj_fee))
                total_fee_pc = 1.0 * total_cj_fee / cjamount
                log.debug('total coinjoin fee = ' + str(float('%.3g' % (
                    100.0 * total_fee_pc))) + '%')
                check_high_fee(total_fee_pc)
                if raw_input('send with these orders? (y/n):')[0] != 'y':
                    # noinspection PyTypeChecker
                    self.finishcallback(None)
                    return
        else:
            orders, total_cj_fee = self.sendpayment_choose_orders(
                self.taker.cjamount, self.taker.options.makercount)
            if not orders:
                log.debug(
                    'ERROR not enough liquidity in the orderbook, exiting')
                return
            total_amount = self.taker.cjamount + total_cj_fee + \
                           self.taker.options.txfee
            log.info('total amount spent = ' + str(total_amount))
            cjamount = self.taker.cjamount
            change_addr = self.taker.changeaddr
            choose_orders_recover = self.sendpayment_choose_orders

        auth_addr = self.taker.utxo_data[self.taker.auth_utxo]['address']
        self.taker.start_cj(self.taker.wallet, cjamount, orders, utxos,
                            self.taker.destaddr, change_addr,
                            self.taker.options.txfee, self.finishcallback,
                            choose_orders_recover, auth_addr)

    def finishcallback(self, coinjointx):
        if coinjointx.all_responded:
            # now sign it ourselves
            tx = btc.serialize(coinjointx.tx)
            for index, ins in enumerate(coinjointx.tx['ins']):
                utxo = ins['outpoint']['hash'] + ':' + str(ins['outpoint'][
                    'index'])
                if utxo != self.taker.auth_utxo:
                    continue
                addr = coinjointx.input_utxos[utxo]['address']
                tx = btc.sign(tx, index,
                              coinjointx.wallet.get_key_from_addr(addr))
            log.info('unsigned tx = \n\n' + tx + '\n')
            log.debug('created unsigned tx, ending')
            self.taker.msgchan.shutdown()
            return
        self.ignored_makers += coinjointx.nonrespondants
        log.debug('recreating the tx, ignored_makers=' + str(
            self.ignored_makers))
        self.create_tx()

    def sendpayment_choose_orders(self,
                                  cj_amount,
                                  makercount,
                                  nonrespondants=None,
                                  active_nicks=None):
        if active_nicks is None:
            active_nicks = []
        if nonrespondants is None:
            nonrespondants = []
        self.ignored_makers += nonrespondants
        orders, total_cj_fee = jm.choose_orders(
            self.taker.db, cj_amount, makercount, self.taker.chooseOrdersFunc,
            self.ignored_makers + active_nicks)
        if not orders:
            return None, 0
        log.info('chosen orders to fill ' + str(orders) + ' totalcjfee=' + str(
            total_cj_fee))
        if not self.taker.options.answeryes:
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
                self.taker.msgchan.shutdown()
                return None, -1
        return orders, total_cj_fee

    def start(self):
        log.info('PaymentWatchdog waiting for stuff to arrive')
        # time.sleep(self.taker.options.waittime)
        reactor.callLater(self.taker.options.waittime, self.create_tx)


class CreateUnsignedTx(jm.Taker):

    def __init__(self, msgchan, wallet, auth_utxo, cjamount, destaddr,
                 changeaddr, utxo_data, options, chooseOrdersFunc):
        jm.Taker.__init__(self, msgchan)
        self.wallet = wallet
        self.auth_utxo = auth_utxo
        self.cjamount = cjamount
        self.destaddr = destaddr
        self.changeaddr = changeaddr
        self.utxo_data = utxo_data
        self.options = options
        self.chooseOrdersFunc = chooseOrdersFunc

    def on_welcome(self):
        jm.Taker.on_welcome(self)
        PaymentThread(self).start()


def build_objects(argv=None):
    if argv is None:
        argv = sys.argv

    parser = OptionParser(
        usage='usage: %prog [options] [auth utxo] [cjamount] [cjaddr] ['
        'changeaddr] [utxos..]',
        description=('Creates an unsigned coinjoin transaction. Outputs '
                     'a partially signed transaction hex string. The user '
                     'must sign their inputs independently and broadcast '
                     'them. The JoinMarket protocol requires the taker to '
                     'have a single p2pk UTXO input to use to '
                     'authenticate the  encrypted messages. For this '
                     'reason you must pass auth utxo and the '
                     'corresponding private key'))

    # for cjamount=0 do a sweep, and ignore change address
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
            help=
            'manually pick which orders to take. doesn\'t work while sweeping.')
    parser.add_option(
            '--yes',
            action='store_true',
            dest='answeryes',
            default=False,
            help='answer yes to everything')

    # TODO implement parser.add_option('-n', '--no-network',
    # action='store_true', dest='nonetwork', default=False, help='dont query
    # the blockchain interface, instead user must supply value of UTXOs on '
    # + ' command line in the format txid:output/value-in-satoshi')

    (options, args) = parser.parse_args(argv[1:])

    if len(args) < 3:
        parser.error('Needs a wallet, amount and destination address')
        sys.exit(0)
    auth_utxo = args[0]
    cjamount = int(args[1])
    destaddr = args[2]
    changeaddr = args[3]
    cold_utxos = args[4:]

    addr_valid1, errormsg1 = jm.validate_address(destaddr)
    errormsg2 = None
    # if amount = 0 dont bother checking changeaddr so user can write any junk
    if cjamount != 0:
        addr_valid2, errormsg2 = jm.validate_address(changeaddr)
    else:
        addr_valid2 = True
    if not addr_valid1 or not addr_valid2:
        if not addr_valid1:
            log.info('ERROR: Address invalid. ' + errormsg1)
        else:
            log.info('ERROR: Address invalid. ' + errormsg2)
        return

    all_utxos = [auth_utxo] + cold_utxos
    query_result = jm.bc_interface.query_utxo_set(all_utxos)
    if None in query_result:
        log.info(query_result)
    utxo_data = {}
    for utxo, data in zip(all_utxos, query_result):
        utxo_data[utxo] = {'address': data['address'], 'value': data['value']}
    auth_privkey = raw_input('input private key for ' + utxo_data[auth_utxo][
        'address'] + ' :')
    if utxo_data[auth_utxo]['address'] != btc.privtoaddr(
            auth_privkey, jm.get_p2pk_vbyte()):
        log.info('ERROR: privkey does not match auth utxo')
        return

    if options.pickorders and cjamount != 0:  # cant use for sweeping
        chooseOrdersFunc = jm.pick_order
    elif options.choosecheapest:
        chooseOrdersFunc = jm.cheapest_order_choose
    else:  # choose randomly (weighted)
        chooseOrdersFunc = jm.weighted_order_choose

    nickname = jm.random_nick()
    log.debug('starting sendpayment')

    class UnsignedTXWallet(jm.AbstractWallet):

        def get_key_from_addr(self, addr):
            log.debug('getting privkey of ' + addr)
            if btc.privtoaddr(auth_privkey, jm.get_p2pk_vbyte()) != addr:
                raise RuntimeError('privkey doesnt match given address')
            return auth_privkey

    wallet = UnsignedTXWallet()
    block_instance = jm.BlockInstance(nickname)
    taker = CreateUnsignedTx(block_instance, wallet, auth_utxo, cjamount, destaddr,
                             changeaddr, utxo_data, options, chooseOrdersFunc)

    return block_instance, taker, wallet
