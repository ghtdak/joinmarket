#! /usr/bin/env python
from __future__ import absolute_import, print_function

import base64
import pprint
import random
import sqlite3
import sys
from decimal import InvalidOperation, Decimal

import bitcoin as btc
from joinmarket.configure import get_p2pk_vbyte, maker_timeout_sec
from joinmarket.enc_wrapper import init_keypair, as_init_encryption, init_pubkey
from joinmarket.support import get_log, calc_cj_fee
from twisted.internet import reactor

log = get_log()


class CoinJoinTX(object):
    # soon the taker argument will be removed and just be replaced by wallet
    # or some other interface
    def __init__(self,
                 taker_sibling,
                 cj_amount,
                 orders,
                 input_utxos,
                 my_cj_addr,
                 my_change_addr,
                 total_txfee,
                 auth_addr=None):
        """
        if my_change is None then there wont be a change address
        thats used if you want to entirely coinjoin one utxo with no change left over
        orders is the orders you want to fill {'counterpartynick': oid, 'cp2': oid2}
        """
        log.debug('starting cj to {} with change '
                  'at {}'.format(my_change_addr, my_change_addr))

        self.taker_sibling = taker_sibling
        self.taker = self.taker_sibling.taker

        # this was the only action by start_cj its kinda different
        self.taker.cjtx = self

        # todo: lazy lazy.  remove this
        self.msgchan = self.taker.msgchan
        self.db = self.taker.db
        self.wallet = self.taker.wallet

        self.cj_amount = cj_amount
        self.active_orders = dict(orders)
        self.input_utxos = input_utxos
        self.total_txfee = total_txfee
        self.my_cj_addr = my_cj_addr
        self.my_change_addr = my_change_addr
        self.auth_addr = auth_addr

        self.all_responded = False
        self.watchdog = CoinJoinTX.Watchdog(self)
        self.watchdog.start()

        # state variables
        self.txid = None
        self.cjfee_total = 0
        self.maker_txfee_contributions = 0
        self.nonrespondants = list(self.active_orders.keys())

        self.latest_tx = None
        # None means they belong to me
        self.utxos = {None: self.input_utxos.keys()}
        self.outputs = []
        # create DH keypair on the fly for this Tx object
        self.kp = init_keypair()
        self.crypto_boxes = {}
        self.msgchan.fill_orders(self.active_orders, self.cj_amount,
                                 self.kp.hex_pk())

    def start_encryption(self, nick, maker_pk):
        if nick not in self.active_orders.keys():
            log.debug("Counterparty not part of this transaction. Ignoring")
            return
        self.crypto_boxes[nick] = [maker_pk, as_init_encryption(
                self.kp, init_pubkey(maker_pk))]
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
            log.debug('signature didnt match pubkey and message')
            return False
        return True

    def recv_txio(self, nick, utxo_list, cj_pub, change_addr):
        if nick not in self.nonrespondants:
            log.debug(('recv_txio => nick={} not in '
                       'nonrespondants {}').format(nick, self.nonrespondants))
            return
        self.utxos[nick] = utxo_list
        order = self.db.execute('SELECT ordertype, txfee, cjfee FROM '
                                'orderbook WHERE oid=? AND counterparty=?',
                                (self.active_orders[nick], nick)).fetchone()

        # todo: this is getting a bit verbose
        bci = self.taker.block_instance.get_bci()
        utxo_data = bci.query_utxo_set(self.utxos[nick])
        if None in utxo_data:
            log.debug(('ERROR outputs unconfirmed or already spent. '
                       'utxo_data={}').format(pprint.pformat(utxo_data)))
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

        fmt = ('fee breakdown for {} totalin={:d} '
               'cjamount={:d} txfee={:d} realcjfee={:d}').format
        log.debug(fmt(nick, total_input, self.cj_amount,
                      order['txfee'], real_cjfee))

        cj_addr = btc.pubtoaddr(cj_pub, get_p2pk_vbyte())
        self.outputs.append({'address': cj_addr, 'value': self.cj_amount})
        self.cjfee_total += real_cjfee
        self.maker_txfee_contributions += order['txfee']
        self.nonrespondants.remove(nick)
        if len(self.nonrespondants) > 0:
            log.debug('nonrespondants = ' + str(self.nonrespondants))
            return

        self.watchdog.success()

        log.debug('got all parts, enough to build a tx')
        self.nonrespondants = list(self.active_orders.keys())

        my_total_in = sum([va['value'] for u, va in
                           self.input_utxos.iteritems()])
        my_txfee = max(self.total_txfee - self.maker_txfee_contributions, 0)
        my_change_value = (
            my_total_in - self.cj_amount - self.cjfee_total - my_txfee)

        fmt = ('fee breakdown for me totalin={:d} my_txfee={:d} '
               'makers_txfee={:d} cjfee_total={:d} => '
               'changevalue={:d}').format
        log.debug(fmt(my_total_in, my_txfee, self.maker_txfee_contributions,
                      self.cjfee_total, my_change_value))

        if self.my_change_addr is None:
            if my_change_value != 0 and abs(my_change_value) != 1:
                # seems you wont always get exactly zero because of integer
                # rounding so 1 satoshi extra or fewer being spent as miner
                # fees is acceptable
                log.debug(('WARNING CHANGE NOT BEING '
                           'USED\nCHANGEVALUE = {}').format(my_change_value))
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
        log.debug('obtained tx\n' + pprint.pformat(btc.deserialize(tx)))
        self.msgchan.send_tx(self.active_orders.keys(), tx)

        self.latest_tx = btc.deserialize(tx)
        for index, ins in enumerate(self.latest_tx['ins']):
            utxo = ins['outpoint']['hash'] + ':' + str(
                    ins['outpoint']['index'])
            if utxo not in self.input_utxos.keys():
                continue
            # placeholders required
            ins['script'] = 'deadbeef'

    def add_signature(self, nick, sigb64):
        if nick not in self.nonrespondants:
            log.debug(('add_signature => nick={} '
                       'not in nonrespondants {}').format(
                    nick, self.nonrespondants))
            return
        sig = base64.b64decode(sigb64).encode('hex')
        inserted_sig = False
        txhex = btc.serialize(self.latest_tx)

        # batch retrieval of utxo data
        utxo = {}
        ctr = 0
        for index, ins in enumerate(self.latest_tx['ins']):
            utxo_for_checking = ins['outpoint']['hash'] + ':' + str(
                    ins['outpoint']['index'])
            if (ins['script'] != '' or
                        utxo_for_checking in self.input_utxos.keys()):
                continue
            utxo[ctr] = [index, utxo_for_checking]
            ctr += 1
        utxo_data = self.taker.block_instance.bc_interface.query_utxo_set(
                [x[1] for x in utxo.values()])

        # insert signatures
        for i, u in utxo.iteritems():
            if utxo_data[i] is None:
                continue
            sig_good = btc.verify_tx_input(
                    txhex, u[0], utxo_data[i]['script'],
                    *btc.deserialize_script(sig))
            if sig_good:
                log.debug('found good sig at index=%d' % (u[0]))
                self.latest_tx['ins'][u[0]]['script'] = sig
                inserted_sig = True
                # check if maker has sent everything possible
                self.utxos[nick].remove(u[1])
                if len(self.utxos[nick]) == 0:
                    log.debug(('nick = {} sent all sigs, removing from '
                               'nonrespondant list').format(nick))
                    self.nonrespondants.remove(nick)
                break
        if not inserted_sig:
            log.debug('signature did not match anything in the tx')
            # TODO what if the signature doesnt match anything
            # nothing really to do except drop it, carry on and wonder why the
            # other guy sent a failed signature

        tx_signed = True
        for ins in self.latest_tx['ins']:
            if ins['script'] == '':
                tx_signed = False
        if not tx_signed:
            return

        self.watchdog.success()

        log.debug('all makers have sent their signatures')
        for index, ins in enumerate(self.latest_tx['ins']):
            # remove placeholders
            if ins['script'] == 'deadbeef':
                ins['script'] = ''
            self.taker_sibling.finishcallback(self)

    def coinjoin_address(self):
        if self.my_cj_addr:
            return self.my_cj_addr
        else:
            return donation_address(self)

    def sign_tx(self, tx, i, priv):
        if self.my_cj_addr:
            return btc.sign(tx, i, priv)
        else:
            return sign_donation_tx(tx, i, priv)

    def self_sign(self):
        # now sign it ourselves
        tx = btc.serialize(self.latest_tx)
        for index, ins in enumerate(self.latest_tx['ins']):
            utxo = ins['outpoint']['hash'] + ':' + str(
                    ins['outpoint']['index'])
            if utxo not in self.input_utxos.keys():
                continue
            addr = self.input_utxos[utxo]['address']
            tx = self.sign_tx(tx, index, self.wallet.get_key_from_addr(addr))
        self.latest_tx = btc.deserialize(tx)

    def push(self, txd):
        tx = btc.serialize(txd)
        log.debug('\n' + tx)
        log.debug('txid = ' + btc.txhash(tx))
        # TODO send to a random maker or push myself
        # TODO need to check whether the other party sent it
        # self.msgchan.push_tx(self.active_orders.keys()[0], txhex)
        self.txid = self.taker.block_instance.get_bci().pushtx(tx)
        if self.txid is None:
            log.debug('unable to pushtx')

    def self_sign_and_push(self):
        self.self_sign()
        self.push(self.latest_tx)

    def recover_from_nonrespondants(self):
        log.debug('nonresponding makers = ' + str(self.nonrespondants))

        # if there is no choose_orders_recover then end and call finishcallback
        # so the caller can handle it in their own way, notable for sweeping
        # where simply replacing the makers wont work

        # this is new

        if not self.taker_sibling.does_recover():
            self.taker_sibling.finishcallback(self)
            return

        if self.latest_tx is None:
            # nonresponding to !fill, recover by finding another maker
            log.debug('nonresponse to !fill')

            for nr in self.nonrespondants:
                del self.active_orders[nr]

            new_orders, new_makers_fee = \
                self.taker_sibling.choose_orders_recover(
                        self.cj_amount, len(self.nonrespondants),
                        self.nonrespondants,
                        self.active_orders.keys())

            for nick, order in new_orders.iteritems():
                self.active_orders[nick] = order

            self.nonrespondants = list(new_orders.keys())

            log.debug(('new active_orders = {} \nnew nonrespondants = '
                       '{}').format(
                    pprint.pformat(self.active_orders),
                    pprint.pformat(self.nonrespondants)))

            self.msgchan.fill_orders(
                    new_orders, self.cj_amount, self.kp.hex_pk())
        else:
            log.debug('nonresponse to !sig')
            # nonresponding to !sig, have to restart tx from the beginning
            self.taker_sibling.finishcallback(self)
                # finishcallback will check if self.all_responded is True and will know it came from here

    class Watchdog(object):

        def __init__(self, cjtx):
            self.cjtx = cjtx
            # todo: all_responded communication strategy - rearchitect!
            self.watchdog = None

        def times_up(self):
            self.watchdog = None
            log.debug('CJTX Timeout: Makers didnt respond')
            self.cjtx.recover_from_nonrespondants()

        def success(self):
            self.cjtx.all_responded = True
            if self.watchdog:
                self.watchdog.cancel()
                self.watchdog = None

        def start(self):
            if self.watchdog:
                self.watchdog.cancel()

            self.cjtx.all_responded = False

            self.watchdog = reactor.callLater(
                    maker_timeout_sec, self.times_up)

class CoinJoinerPeer(object):
    def __init__(self, block_instance, msgchan):
        self.block_instance = block_instance
        self.msgchan = msgchan
        self.msgchan.set_coinjoiner_peer(self)

    def get_crypto_box_from_nick(self, nick):
        raise Exception()

    def on_set_topic(self, *args, **kwargs):
        pass

    def on_welcome(self, *args, **kwargs):
        pass

    def on_connect(self, *args, **kwargs):
        pass

    def on_disconnect(self, *args, **kwargs):
        pass

    def on_nick_leave(self, *args, **kwargs):
        pass

    def on_nick_change(self, *args, **kwargs):
        pass

    def on_order_seen(self, *args, **kwargs):
        pass

    def on_order_cancel(self, *args, **kwargs):
        pass

    def on_error(self, *args, **kwargs):
        pass

    def on_pubkey(self, *args, **kwargs):
        pass

    def on_ioauth(self, *args, **kwargs):
        pass

    def on_sig(self, *args, **kwargs):
        pass

    def on_orderbook_requested(self, *args, **kwargs):
        pass

    def on_order_fill(self, *args, **kwargs):
        pass

    def on_seen_auth(self, *args, **kwargs):
        pass

    def on_seen_tx(self, *args, **kwargs):
        pass

    def on_push_tx(self, *args, **kwargs):
        pass


class OrderbookWatch(CoinJoinerPeer):
    def __init__(self, block_instance, msgchan):
        super(OrderbookWatch, self).__init__(block_instance, msgchan)

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
                log.debug(
                    "Got invalid order ID: " + oid + " from " + counterparty)
                return
            # delete orders eagerly, so in case a buggy maker sends an
            # invalid offer, we won't accidentally !fill based on the ghost
            # of its previous message.
            self.db.execute(("DELETE FROM orderbook WHERE counterparty=? "
                             "AND oid=?;"), (counterparty, oid))
            # now validate the remaining fields
            if int(minsize) < 0 or int(minsize) > 21 * 10 ** 14:
                log.debug("Got invalid minsize: {} from {}".format(
                        minsize, counterparty))
                return
            if int(maxsize) < 0 or int(maxsize) > 21 * 10 ** 14:
                log.debug("Got invalid maxsize: " + maxsize + " from " +
                          counterparty)
                return
            if int(txfee) < 0:
                log.debug("Got invalid txfee: {} from {}".format(
                        txfee, counterparty))
                return
            if int(minsize) > int(maxsize):

                fmt = ("Got minsize bigger than maxsize: {} - {} "
                       "from {}").format
                log.debug(fmt(minsize, maxsize, counterparty))
                return
            self.db.execute(
                    'INSERT INTO orderbook VALUES(?, ?, ?, ?, ?, ?, ?);',
                    (counterparty, oid, ordertype, minsize, maxsize, txfee,
                     str(Decimal(
                         cjfee))))  # any parseable Decimal is a valid cjfee
        except InvalidOperation:
            log.debug("Got invalid cjfee: " + cjfee + " from " + counterparty)
        except:
            log.debug("Error parsing order " + oid + " from " + counterparty)

    def on_order_cancel(self, counterparty, oid):
        self.db.execute(("DELETE FROM orderbook WHERE "
                         "counterparty=? AND oid=?;"), (counterparty, oid))

    def on_welcome(self):
        self.msgchan.request_orderbook()

    def on_nick_leave(self, nick):
        self.db.execute('DELETE FROM orderbook WHERE counterparty=?;', (nick,))

    def on_disconnect(self, reason):
        self.db.execute('DELETE FROM orderbook;')


# assume this only has one open cj tx at a time
class Taker(OrderbookWatch):
    def __init__(self, block_instance, msgchan):
        OrderbookWatch.__init__(self, block_instance, msgchan)
        msgchan.cjpeer = self
        self.cjtx = None
        self.maker_pks = {}
        self.sibling = None
        # TODO have a list of maker's nick we're coinjoining with, so
        # that some other guy doesnt send you confusing stuff

    def get_crypto_box_from_nick(self, nick):
        if nick in self.cjtx.crypto_boxes:
            return self.cjtx.crypto_boxes[nick][
                1]  # libsodium encryption object
        else:
            log.debug('something wrong, no crypto object, nick=' + nick +
                      ', message will be dropped')
            return None

    def on_pubkey(self, nick, maker_pubkey):
        self.cjtx.start_encryption(nick, maker_pubkey)

    def on_ioauth(self, nick, utxo_list, cj_pub, change_addr, btc_sig):
        if not self.cjtx.auth_counterparty(nick, btc_sig, cj_pub):
            fmt = ('Authenticated encryption with counterparty: {}'
                    ' not established. TODO: send rejection message').format
            log.debug(fmt(nick))
            return
        self.cjtx.recv_txio(nick, utxo_list, cj_pub, change_addr)

    def on_sig(self, nick, sig):
        self.cjtx.add_signature(nick, sig)


def sign_donation_tx(tx, i, priv):
    k = sign_k
    hashcode = btc.SIGHASH_ALL
    i = int(i)
    if len(priv) <= 33:
        priv = btc.safe_hexlify(priv)
    pub = btc.privkey_to_pubkey(priv)
    address = btc.pubkey_to_address(pub)
    signing_tx = btc.signature_form(
            tx, i, btc.mk_pubkey_script(address), hashcode)

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
    sender_pubkey = btc.add_pubkeys(
            reusable_donation_pubkey, btc.multiply(
                    btc.G, c))
    sender_address = btc.pubtoaddr(sender_pubkey,
                                   get_p2pk_vbyte())
    log.debug('sending coins to ' + sender_address)
    return sender_address

class TakerSibling(object):
    def __init__(self, taker):
        self.taker = taker

    def choose_orders_recover(
            self, cj_amount, makercount, nonrespondants=None,
            active_nicks=None):
        pass

    def finishcallback(self, coinjointx):
        pass

    def does_recover(self):
        """
        not enitirely sure.  the coinjointx code has an option...
        :return:
        """
        return True
