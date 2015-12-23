#! /usr/bin/env python
from __future__ import absolute_import, print_function

import base64
import pprint
import random
import sqlite3
import sys
from decimal import InvalidOperation, Decimal
from math import exp

from twisted.internet import defer, reactor
from twisted.logger import Logger

import bitcoin as btc
from .abstracts import CoinJoinerPeer, TransactionWatcher
from .blockchaininterface import bc_interface
from .configure import get_p2pk_vbyte, maker_timeout_sec
from .enc_wrapper import init_keypair, as_init_encryption, init_pubkey
from .support import calc_cj_fee

log = Logger()


class CoinJoinTX(TransactionWatcher):
    # soon the taker argument will be removed and just be replaced by wallet
    # or some other interface
    def __init__(self,
                 taker,
                 cj_amount,
                 orders,
                 input_utxos,
                 cj_addr,
                 my_change_addr,
                 total_txfee,
                 auth_addr=None):
        super(CoinJoinTX, self).__init__(taker)

        # properties from superclass
        self.txd = None
        self.cj_addr = cj_addr

        self.d_phase1 = None
        self.taker = taker
        self.block_instance = self.taker.block_instance

        """
        if my_change is None then there wont be a change address thats
        used if you want to entirely coinjoin one utxo with no change left
        over orders is the orders you want to fill {'counterpartynick': oid,
        'cp2': oid2}
        """
        self.log.debug(
                'starting cj to {cj_addr} with change at {my_change_addr}',
                cj_addr=cj_addr, my_change_addr=my_change_addr)

        # todo: this was the only action by defunct start_cj.  rearchitect!!
        self.taker.cjtx = self

        self.db = self.taker.db
        self.wallet = self.taker.wallet

        self.cj_amount = cj_amount
        self.active_orders = dict(orders)
        self.input_utxos = input_utxos
        self.total_txfee = total_txfee
        self.my_change_addr = my_change_addr
        self.auth_addr = auth_addr

        self.all_responded = False
        self.watchdog = CoinJoinTX.Watchdog(self)
        self.watchdog.start()
        self.utxo_tx = None

        # state variables
        # todo: is self.txid used anywhere?
        self.txid = None
        self.cjfee_total = 0
        self.maker_txfee_contributions = 0
        self.nonrespondants = list(self.active_orders.keys())

        # None means they belong to me
        self.utxos = {None: self.input_utxos.keys()}
        self.outputs = []
        # create DH keypair on the fly for this Tx object
        self.kp = init_keypair()
        self.crypto_boxes = {}
        self.msgchan.fill_orders(
                self.active_orders, self.cj_amount, self.kp.hex_pk())

    def __getattr__(self, name):
        if name == 'msgchan':
            return self.block_instance.irc_market
        else:
            raise AttributeError

    def phase1(self):
        """
        the creator waits for the results
        :return: deferred
        """
        self.d_phase1 = defer.Deferred()
        return self.d_phase1

    def start_encryption(self, nick, maker_pk):
        if nick not in self.active_orders.keys():
            self.log.debug("Counterparty not part of this transaction. "
                           "Ignoring")
            return
        self.crypto_boxes[nick] = [maker_pk,
                                   as_init_encryption(self.kp,
                                                      init_pubkey(maker_pk))]
        # send authorisation request
        if self.auth_addr:
            my_btc_addr = self.auth_addr
        else:
            my_btc_addr = self.input_utxos.itervalues().next()['address']
        my_btc_priv = self.wallet.get_key_from_addr(my_btc_addr)
        my_btc_pub = btc.privtopub(my_btc_priv)
        my_btc_sig = btc.ecdsa_sign(self.kp.hex_pk(), my_btc_priv)
        self.msgchan.send_auth(nick, my_btc_pub, my_btc_sig)

    def auth_counterparty(self, nick, btc_sig, cj_pub):
        """Validate the counterpartys claim to own the btc
        address/pubkey that will be used for coinjoining
        with an ecdsa verification."""
        # crypto_boxes[nick][0] = maker_pubkey
        if not btc.ecdsa_verify(self.crypto_boxes[nick][0], btc_sig, cj_pub):
            self.log.debug('signature didnt match pubkey and message')
            return False
        return True

    def recv_txio(self, nick, utxo_list, cj_pub, change_addr):
        if nick not in self.nonrespondants:
            self.log.debug(
                    'recv_txio => nick={nick} not in nonrespondants',
                    nick=nick)
            return
        self.utxos[nick] = utxo_list
        order = self.db.execute('SELECT ordertype, txfee, cjfee FROM '
                                'orderbook WHERE oid=? AND counterparty=?',
                                (self.active_orders[nick], nick)).fetchone()

        bci = bc_interface
        utxo_data = bci.query_utxo_set(self.utxos[nick])
        if None in utxo_data:
            self.log.debug('ERROR outputs unconfirmed or already spent. ')
            # when internal reviewing of makers is created, add it here to
            # immediately quit
            return

        # ignore this message, eventually the timeout thread will recover
        total_input = sum([d['value'] for d in utxo_data])

        real_cjfee = calc_cj_fee(order['ordertype'], order['cjfee'],
                                 self.cj_amount)

        self.outputs.append({'address': change_addr,
                             'value': total_input - self.cj_amount - order[
                                 'txfee'] + real_cjfee})

        self.log.debug('fee breakdown for {nick} totalin={totalin} '
                       'cjamount={cjamount} txfee={txfee} '
                       'realcjfee={realcjfee}',
                       nick=nick, totalin=total_input, cjamount=self.cj_amount,
                       txfee=order['txfee'], realcjfee=real_cjfee)

        cj_addr = btc.pubtoaddr(cj_pub, get_p2pk_vbyte())
        self.outputs.append({'address': cj_addr, 'value': self.cj_amount})
        self.cjfee_total += real_cjfee
        self.maker_txfee_contributions += order['txfee']
        self.nonrespondants.remove(nick)
        if len(self.nonrespondants) > 0:
            self.log.debug('nonrespondants = ' + str(self.nonrespondants))
            return

        self.watchdog.cancel()

        self.log.debug('got all parts, enough to build a tx')
        self.nonrespondants = list(self.active_orders.keys())

        my_total_in = sum(
                [va['value'] for u, va in self.input_utxos.iteritems()])

        my_txfee = max(self.total_txfee - self.maker_txfee_contributions, 0)
        my_change_value = (
            my_total_in - self.cj_amount - self.cjfee_total - my_txfee)

        fmt = ('fee breakdown totalin={totalin} my_txfee={my_txfee} '
               'makers_txfee={makers_txfee} cjfee_total={cjfee_total} => '
               'changevalue={changevalue}')
        self.log.debug(
                totalin=my_total_in, my_txfee=my_txfee,
                makers_txfee=self.maker_txfee_contributions,
                cjfee_total=self.cjfee_total,
                changevalue=my_change_value)

        if self.my_change_addr is None:
            if my_change_value != 0 and abs(my_change_value) != 1:
                # seems you wont always get exactly zero because of integer
                # rounding so 1 satoshi extra or fewer being spent as miner
                # fees is acceptable
                self.log.debug('WARNING CHANGE NOT BEING USED\n'
                               'CHANGEVALUE = {cv}', cv=my_change_value)
        else:
            self.outputs.append({'address': self.my_change_addr,
                                 'value': my_change_value})
        self.utxo_tx = [dict([('output', u)])
                        for u in sum(self.utxos.values(), [])]
        self.outputs.append({'address': self.coinjoin_address(),
                             'value': self.cj_amount})
        random.shuffle(self.utxo_tx)
        random.shuffle(self.outputs)
        tx = btc.mktx(self.utxo_tx, self.outputs)
        self.msgchan.send_tx(self.active_orders.keys(), tx)

        self.txd = btc.deserialize(tx)
        for index, ins in enumerate(self.txd['ins']):
            utxo = ins['outpoint']['hash'] + ':' + str(ins['outpoint']['index'])
            if utxo not in self.input_utxos.keys():
                continue
            # placeholders required
            ins['script'] = 'deadbeef'

    def add_signature(self, nick, sigb64):
        if nick not in self.nonrespondants:
            self.log.debug(('add_signature => nick={} '
                       'not in nonrespondants {}').format(
                    nick, self.nonrespondants))
            return
        sig = base64.b64decode(sigb64).encode('hex')
        inserted_sig = False
        txhex = btc.serialize(self.txd)

        # batch retrieval of utxo data
        utxo = {}
        ctr = 0
        for index, ins in enumerate(self.txd['ins']):
            utxo_for_checking = ins['outpoint']['hash'] + ':' + str(ins[
                'outpoint']['index'])
            if (ins['script'] != '' or
                    utxo_for_checking in self.input_utxos.keys()):
                continue
            utxo[ctr] = [index, utxo_for_checking]
            ctr += 1
        utxo_data = bc_interface.query_utxo_set([x[1] for x in utxo.values()])

        # insert signatures
        for i, u in utxo.iteritems():
            if utxo_data[i] is None:
                continue
            sig_good = btc.verify_tx_input(txhex, u[0], utxo_data[i]['script'],
                                           *btc.deserialize_script(sig))
            if sig_good:
                self.log.debug('found good sig at index=%d' % (u[0]))
                self.txd['ins'][u[0]]['script'] = sig
                inserted_sig = True
                # check if maker has sent everything possible
                self.utxos[nick].remove(u[1])
                if len(self.utxos[nick]) == 0:
                    self.log.debug('nick = {nick} sent all sigs, removing from '
                                   'nonrespondant list', nick=nick)
                    self.nonrespondants.remove(nick)
                break
        if not inserted_sig:
            self.log.debug('signature did not match anything in the tx')
            # TODO what if the signature doesnt match anything
            # nothing really to do except drop it, carry on and wonder why the
            # other guy sent a failed signature

        tx_signed = True
        for ins in self.txd['ins']:
            if ins['script'] == '':
                tx_signed = False
        if not tx_signed:
            return

        self.watchdog.cancel()

        self.log.debug('all makers have sent their signatures')
        for index, ins in enumerate(self.txd['ins']):
            # remove placeholders
            if ins['script'] == 'deadbeef':
                ins['script'] = ''

        bc_interface.add_tx_notify(self)
        self.d_phase1.callback(self)

    def coinjoin_address(self):
        if self.cj_addr:
            return self.cj_addr
        else:
            return donation_address(self)

    def sign_tx(self, tx, i, priv):
        if self.cj_addr:
            return btc.sign(tx, i, priv)
        else:
            return sign_donation_tx(tx, i, priv)

    def self_sign(self):
        # now sign it ourselves
        tx = btc.serialize(self.txd)
        for index, ins in enumerate(self.txd['ins']):
            utxo = ins['outpoint']['hash'] + ':' + str(ins['outpoint']['index'])
            if utxo not in self.input_utxos.keys():
                continue
            addr = self.input_utxos[utxo]['address']
            tx = self.sign_tx(tx, index, self.wallet.get_key_from_addr(addr))
        self.txd = btc.deserialize(tx)

    def push(self, txd):
        tx = btc.serialize(txd)
        new_txid=btc.txhash(tx)
        self.log.debug('push txid: {txid}, {sz}',
                       txid=new_txid, sz=len(tx))
        # TODO send to a random maker or push myself
        # TODO need to check whether the other party sent it
        # self.msgchan.push_tx(self.active_orders.keys()[0], txhex)
        self.txid = bc_interface.pushtx(tx)
        if self.txid is None:
            self.log.warn('pushtx failed: {txid}', txid=new_txid)

    def self_sign_and_push(self):
        self.self_sign()
        self.push(self.txd)

    def unconfirmfun(self, txd, txid):
        self.log.debug('unconfirmfun and I don\'t care')

    def watchdog_timeout(self):
        self.log.debug('nonresponding makers: {nonrespondents}',
                       nonrespondents=self.nonrespondants)

        # was finishcallback
        self.d_phase1.callback(self)

    class Watchdog(object):

        def __init__(self, cjtx):
            self.cjtx = cjtx
            self.watchdog = None

        def times_up(self):
            self.watchdog = None
            log.debug('CJTX Timeout: Makers didnt respond')
            self.cjtx.watchdog_timeout()

        def cancel(self):
            self.cjtx.all_responded = True
            if self.watchdog:
                self.watchdog.cancel()
                self.watchdog = None

        def start(self):
            if self.watchdog:
                self.watchdog.cancel()

            self.cjtx.all_responded = False

            self.watchdog = reactor.callLater(maker_timeout_sec, self.times_up)


class OrderbookWatch(CoinJoinerPeer):

    def __init__(self, block_instance):
        super(OrderbookWatch, self).__init__(block_instance)

        con = sqlite3.connect(":memory:", check_same_thread=False)
        con.row_factory = sqlite3.Row
        self.db = con.cursor()
        self.db.execute("CREATE TABLE orderbook(counterparty TEXT, "
                        "oid INTEGER, ordertype TEXT, minsize INTEGER, "
                        "maxsize INTEGER, txfee INTEGER, cjfee TEXT);")

    def on_order_seen(self, counterparty, oid, ordertype, minsize, maxsize,
                      txfee, cjfee):
        try:
            if int(oid) < 0 or int(oid) > sys.maxint:
                self.log.debug(
                        "Got invalid order ID: {oid} from {counterparty}",
                        oid=oid, counterparty=counterparty)
                return
            # delete orders eagerly, so in case a buggy maker sends an
            # invalid offer, we won't accidentally !fill based on the ghost
            # of its previous message.
            self.db.execute(
                ("DELETE FROM orderbook WHERE counterparty=? "
                 "AND oid=?;"), (counterparty, oid))
            # now validate the remaining fields
            if int(minsize) < 0 or int(minsize) > 21 * 10**14:
                self.log.debug("Got invalid minsize: {} from {}".format(
                        minsize, counterparty))
                return
            if int(maxsize) < 0 or int(maxsize) > 21 * 10**14:
                self.log.debug("Got invalid maxsize: " + maxsize + " from " +
                               counterparty)
                return
            if int(txfee) < 0:
                self.log.debug("Got invalid txfee: {txfee} from {counterparty}",
                               txfee=txfee, counterparty=counterparty)
                return
            if int(minsize) > int(maxsize):
                self.log.debug("Got minsize bigger than maxsize: {minsize} - "
                               "{maxsize} from {counterparty}",
                               minsize=minsize, maxsize=maxsize,
                               counterparty=counterparty)
                return
            self.db.execute(
                'INSERT INTO orderbook VALUES(?, ?, ?, ?, ?, ?, ?);',
                (counterparty, oid, ordertype, minsize, maxsize, txfee,
                 str(Decimal(cjfee))))  # any parseable Decimal is a valid cjfee
        except InvalidOperation:
            self.log.debug("Got invalid cjfee: {cjfee} from {counterparty}",
                           cjfee=cjfee, counterparty=counterparty)
        except:
            self.log.debug("Error parsing order {oid} from {counterparty}",
                           oid=oid, counterparty=counterparty)

    def on_order_cancel(self, counterparty, oid):
        self.db.execute(
            ("DELETE FROM orderbook WHERE "
             "counterparty=? AND oid=?;"), (counterparty, oid))

    def on_welcome(self):
        self.msgchan.request_orderbook()

    def on_nick_leave(self, nick):
        self.db.execute('DELETE FROM orderbook WHERE counterparty=?;', (nick,))

    def on_disconnect(self, reason):
        self.db.execute('DELETE FROM orderbook;')


# assume this only has one open cj tx at a time
class Taker(OrderbookWatch):

    def __init__(self, block_instance):
        OrderbookWatch.__init__(self, block_instance)
        self.cjtx = None
        self.maker_pks = {}
        self.sibling = None
        # part of the great function rewriting

        self._chooseOrdersFunc = self.cheapest_order_choose

    @property
    def chooseOrdersFunc(self):
        return self._chooseOrdersFunc

    @chooseOrdersFunc.setter
    def chooseOrdersFunc(self, value):
        self.log.debug('pasta: chooseOrdersFunc->{name}', name=value.__name__)
        self._chooseOrdersFunc = value

    # -----------------------------------------
    # callbacks
    # -----------------------------------------

    def choose_orders_recover(
            self, cj_amount, makercount, nonrespondants=None,
            active_nicks=None):
        raise NotImplementedError()

    # ----------------------------------------

    def get_crypto_box_from_nick(self, nick):
        if nick in self.cjtx.crypto_boxes:
            # libsodium encryption object
            return self.cjtx.crypto_boxes[nick][1]
        else:
            self.log.debug('something wrong, no crypto object, nick={nick}, '
                           'message will be dropped', nick=nick)
            return None

    def on_pubkey(self, nick, maker_pubkey):
        self.cjtx.start_encryption(nick, maker_pubkey)

    def on_ioauth(self, nick, utxo_list, cj_pub, change_addr, btc_sig):
        if not self.cjtx.auth_counterparty(nick, btc_sig, cj_pub):
            fmt = ('Authenticated encryption with counterparty: {}'
                   ' not established. TODO: send rejection message').format
            self.log.debug(fmt(nick))
            return
        self.cjtx.recv_txio(nick, utxo_list, cj_pub, change_addr)

    def on_sig(self, nick, sig):
        self.cjtx.add_signature(nick, sig)

    def choose_orders(self, db, cj_amount, n, ignored_makers=None):
        if ignored_makers is None:
            ignored_makers = []
        sqlorders = db.execute('SELECT * FROM orderbook;').fetchall()

        orders = []
        for o in sqlorders:
            if (o['minsize'] <= cj_amount <= o['maxsize'] and
                        o['counterparty'] not in ignored_makers):
                orders.append(
                        (o['counterparty'], o['oid'], calc_cj_fee(
                                o['ordertype'], o['cjfee'], cj_amount),
                         o['txfee']))

        # function that returns the fee for a given order
        def feekey(o):
            return o[2] - o[3]

        counterparties = set([o[0] for o in orders])
        if n > len(counterparties):
            self.log.debug(
                    'ERROR not enough liquidity in the orderbook {n} suitable-'
                    'counterparties=%d amount=%d totalorders=%d',
                    n=n, numcp=len(counterparties), cj_amount=cj_amount,
                    numord=len(orders))

            # TODO handle not enough liquidity better, maybe an Exception
            return None, 0
        """
        restrict to one order per counterparty, choose the one with the lowest
        cjfee this is done in advance of the order selection algo, so applies to
        all of them. however, if orders are picked manually, allow duplicates.
        """
        if self.chooseOrdersFunc != self.pick_order:
            orders = sorted(
                dict((v[0], v) for v in sorted(
                        orders, key=feekey, reverse=True)).values(), key=feekey)
        else:
            # sort from smallest to biggest cj fee
            orders = sorted(orders, key=feekey)

        for n, o in enumerate(orders):
            self.log.debug('candidate orders[{n}]: {o}', n=n, o=o)

        total_cj_fee = 0
        chosen_orders = []
        for i in range(n):
            chosen_order = self.chooseOrdersFunc(orders, n, feekey)
            # remove all orders from that same counterparty
            orders = [o for o in orders if o[0] != chosen_order[0]]
            chosen_orders.append(chosen_order)
            total_cj_fee += chosen_order[2]

        for n, o in enumerate(chosen_orders):
            self.log.debug('selected orders[{n}]: {o}', n=n, o=o)
        chosen_orders = [o[:2] for o in chosen_orders]
        return dict(chosen_orders), total_cj_fee

    @staticmethod
    def rand_weighted_choice(n, p_arr):
        """
        Choose a value in 0..n-1
        with the choice weighted by the probabilities
        in the list p_arr. Note that there will be some
        floating point rounding errors, but see the note
        at the top of this section.
        """
        if abs(sum(p_arr) - 1.0) > 1e-4:
            raise ValueError("Sum of probabilities must be 1")
        if len(p_arr) != n:
            raise ValueError("Need: " + str(n) + " probabilities.")
        cum_pr = [sum(p_arr[:i + 1]) for i in xrange(len(p_arr))]
        r = random.random()
        return sorted(cum_pr + [r]).index(r)

    def weighted_order_choose(self, orders, n, feekey):
        """
        Algorithm for choosing the weighting function
        it is an exponential
        P(f) = exp(-(f - fmin) / phi)
        P(f) - probability of order being chosen
        f - order fee
        fmin - minimum fee in the order book
        phi - scaling parameter, 63% of the distribution is within

        define number M, related to the number of counterparties in this coinjoin
        phi has a value such that it contains up to the Mth order
        unless M < orderbook size, then phi goes up to the last order
        """
        minfee = feekey(orders[0])
        M = int(3 * n)
        if len(orders) > M:
            phi = feekey(orders[M]) - minfee
        else:
            phi = feekey(orders[-1]) - minfee
        fee = [feekey(o) for o in orders]
        if phi > 0:
            weight = [exp(-(1.0 * f - minfee) / phi) for f in fee]
        else:
            weight = [1.0] * len(fee)
        weight = [x / sum(weight) for x in weight]
        self.log.debug('phi=' + str(phi) + ' weights = ' + str(weight))
        chosen_order_index = self.rand_weighted_choice(len(orders), weight)
        return orders[chosen_order_index]


    def cheapest_order_choose(self, orders, n, feekey):
        """
        Return the cheapest order from the orders.
        """
        return sorted(orders, key=feekey)[0]


    def pick_order(self, orders, n, feekey):
        i = -1
        self.log.info("Considered orders:")
        for o in orders:
            i += 1
            self.log.info("    %2d. %20s, CJ fee: %6d, tx fee: %6d" %
                     (i, o[0], o[2], o[3]))
        pickedOrderIndex = -1
        if i == 0:
            self.log.info("Only one possible pick, picking it.")
            return orders[0]
        while pickedOrderIndex == -1:
            try:
                pickedOrderIndex = int(raw_input('Pick an order between 0 and ' +
                                                 str(i) + ': '))
            except ValueError:
                pickedOrderIndex = -1
                continue

            if 0 <= pickedOrderIndex < len(orders):
                return orders[pickedOrderIndex]
            pickedOrderIndex = -1


    def choose_sweep_orders(self, db,
                            total_input_value,
                            total_txfee,
                            n,
                            ignored_makers=None):
        """
        choose an order given that we want to be left with no change
        i.e. sweep an entire group of utxos

        solve for cjamount when mychange = 0
        for an order with many makers, a mixture of absorder and relorder
        mychange = totalin - cjamount - total_txfee - sum(absfee) - sum(relfee*cjamount)
        => 0 = totalin - mytxfee - sum(absfee) - cjamount*(1 + sum(relfee))
        => cjamount = (totalin - mytxfee - sum(absfee)) / (1 + sum(relfee))
        """

        if ignored_makers is None:
            ignored_makers = []

        def calc_zero_change_cj_amount(ordercombo):
            sumabsfee = 0
            sumrelfee = Decimal('0')
            sumtxfee_contribution = 0
            for order in ordercombo:
                sumtxfee_contribution += order[0]['txfee']
                if order[0]['ordertype'] == 'absorder':
                    sumabsfee += int(order[0]['cjfee'])
                elif order[0]['ordertype'] == 'relorder':
                    sumrelfee += Decimal(order[0]['cjfee'])
                else:
                    raise RuntimeError('unknown order type: {}'.format(
                            order[0]['ordertype']))

            my_txfee = max(total_txfee - sumtxfee_contribution, 0)
            cjamount = (total_input_value - my_txfee - sumabsfee) / (1 + sumrelfee)
            cjamount = int(cjamount.quantize(Decimal(1)))
            return cjamount, int(sumabsfee + sumrelfee * cjamount)

        self.log.debug('choosing sweep orders for total_input_value = {tiv}',
                       tiv=total_input_value)

        sqlorders = db.execute('SELECT * FROM orderbook WHERE minsize <= ?;',
                               (total_input_value,)).fetchall()
        orderkeys = ['counterparty', 'oid', 'ordertype', 'minsize', 'maxsize',
                     'txfee', 'cjfee']
        orderlist = [dict([(k, o[k]) for k in orderkeys])
                     for o in sqlorders
                     if o['counterparty'] not in ignored_makers]

        # uncomment this and comment previous two lines for faster runtime but
        # less readable output
        # orderlist = sqlorders

        print(pprint.pformat(orderlist))

        # choose N amount of orders
        available_orders = [(o, calc_cj_fee(o['ordertype'], o['cjfee'],
                                            total_input_value), o['txfee'])
                            for o in orderlist]

        def feekey(o):
            return o[1] - o[2]

        # sort from smallest to biggest cj fee
        available_orders = sorted(available_orders, key=feekey)
        chosen_orders = []

        # todo: static analysis bug
        cj_amount = 0
        while len(chosen_orders) < n:
            if len(available_orders) < n - len(chosen_orders):
                self.log.debug('ERROR not enough liquidity in the orderbook')
                # TODO handle not enough liquidity better, maybe an Exception
                return None, 0
            for i in range(n - len(chosen_orders)):
                chosen_order = self.chooseOrdersFunc(
                        available_orders, n, feekey)
                # remove all orders from that same counterparty
                available_orders = [o for o in available_orders
                                    if o[0]['counterparty'] !=
                                    chosen_order[0]['counterparty']]
                chosen_orders.append(chosen_order)
            # calc cj_amount and check its in range
            cj_amount, total_fee = calc_zero_change_cj_amount(chosen_orders)
            for c in list(chosen_orders):
                minsize = c[0]['minsize']
                maxsize = c[0]['maxsize']
                if cj_amount > maxsize or cj_amount < minsize:
                    chosen_orders.remove(c)

            print(pprint.pformat(chosen_order))

        result = dict([(o[0]['counterparty'], o[0]['oid'])
                       for o in chosen_orders])
        self.log.debug('cj amount = {cj_amount}', cj_amount=cj_amount)
        return result, cj_amount


def sign_donation_tx(tx, i, priv):
    k = sign_k
    hashcode = btc.SIGHASH_ALL
    i = int(i)
    if len(priv) <= 33:
        priv = btc.safe_hexlify(priv)
    pub = btc.privkey_to_pubkey(priv)
    address = btc.pubkey_to_address(pub)
    signing_tx = btc.signature_form(tx, i, btc.mk_pubkey_script(address),
                                    hashcode)

    msghash = btc.bin_txhash(signing_tx, hashcode)
    z = btc.hash_to_int(msghash)
    # k = deterministic_generate_k(msghash, priv)
    r, y = btc.fast_multiply(btc.G, k)
    s = btc.inv(k, btc.N) * (z + r * btc.decode_privkey(priv)) % btc.N
    rawsig = 27 + (y % 2), r, s

    sig = btc.der_encode_sig(*rawsig) + btc.encode(hashcode, 16, 2)
    # sig = ecdsa_tx_sign(signing_tx, priv, hashcode)
    txobj = btc.deserialize(tx)
    txobj["ins"][i]["script"] = btc.serialize_script([sig, pub])
    return btc.serialize(txobj)


# this stuff copied and slightly modified from pybitcointools
def donation_address(cjtx):
    reusable_donation_pubkey = ('02be838257fbfddabaea03afbb9f16e852'
                                '9dfe2de921260a5c46036d97b5eacf2a')

    donation_utxo_data = cjtx.input_utxos.iteritems().next()
    global donation_utxo
    donation_utxo = donation_utxo_data[0]
    privkey = cjtx.wallet.get_key_from_addr(donation_utxo_data[1]['address'])
    # tx without our inputs and outputs
    tx = btc.mktx(cjtx.utxo_tx, cjtx.outputs)
    # address = privtoaddr(privkey)
    # signing_tx = signature_form(tx, 0, mk_pubkey_script(address), SIGHASH_ALL)
    msghash = btc.bin_txhash(tx, btc.SIGHASH_ALL)
    # generate unpredictable k
    global sign_k
    sign_k = btc.deterministic_generate_k(msghash, privkey)
    c = btc.sha256(btc.multiply(reusable_donation_pubkey, sign_k))
    sender_pubkey = btc.add_pubkeys(reusable_donation_pubkey,
                                    btc.multiply(btc.G, c))
    sender_address = btc.pubtoaddr(sender_pubkey, get_p2pk_vbyte())
    log.debug('sending coins to ' + sender_address)
    return sender_address

__all__ = ('Taker', 'OrderbookWatch', 'CoinJoinTX')
