#! /usr/bin/env python

from common import *
import taker as takermodule
import bitcoin as btc

from optparse import OptionParser
import threading


def choose_sweep_order(db, my_total_input, my_tx_fee):
    '''
	choose an order given that we want to be left with no change
	i.e. sweep an entire group of utxos

	solve for cjamount when mychange = 0
	ABS FEE
	mychange = totalin - cjamount - mytxfee - absfee = 0
	=> cjamount = totalin - mytxfee - absfee
	REL FEE
	mychange = totalin - cjamount - mytxfee - relfee*cjamount
	=> 0 = totalin - mytxfee - cjamount*(1 + relfee)
	=> cjamount = (totalin - mytxfee) / (1 + relfee)
	'''

    def calc_zero_change_cj_amount(ordertype, cjfee):
        cj_amount = None
        if ordertype == 'absorder':
            cj_amount = my_total_input - my_tx_fee - cjfee
        elif ordertype == 'relorder':
            cj_amount = (my_total_input - my_tx_fee) / (Decimal(cjfee) + 1)
            cj_amount = int(cj_amount.quantize(Decimal(1)))
        else:
            raise RuntimeError('unknown order type: ' + str(ordertype))
        return cj_amount

    sqlorders = db.execute('SELECT * FROM orderbook;').fetchall()
    orders = [(o['counterparty'], o['oid'],
               calc_zero_change_cj_amount(o['ordertype'], o['cjfee']),
               o['minsize'], o['maxsize']) for o in sqlorders]
    #filter cj_amounts that are not in range
    orders = [o[:3] for o in orders if o[2] >= o[3] and o[2] <= o[4]]
    orders = sorted(orders, key=lambda k: k[2])
    print 'sweep orders = ' + str(orders)
    return orders[-1]  #choose one with the highest cj_amount, most left over after paying everything else


#thread which does the buy-side algorithm
# chooses which coinjoins to initiate and when
class PaymentThread(threading.Thread):

    def __init__(self, taker):
        threading.Thread.__init__(self)
        self.daemon = True
        self.taker = taker

    def finishcallback(self):
        self.taker.shutdown()

    def run(self):
        print 'waiting for all orders to certainly arrive'
        time.sleep(self.taker.waittime)

        crow = self.taker.db.execute(
            'SELECT COUNT(DISTINCT counterparty) FROM orderbook;').fetchone()
        counterparty_count = crow['COUNT(DISTINCT counterparty)']
        if counterparty_count < self.taker.makercount:
            print 'not enough counterparties to fill order, ending'
            self.taker.shutdown()
            return

        orders, total_cj_fee = choose_order(self.taker.db, self.taker.amount,
                                            self.taker.makercount)
        print 'chosen orders to fill ' + str(orders) + ' totalcjfee=' + str(
            total_cj_fee)
        total_amount = self.taker.amount + total_cj_fee + self.taker.txfee
        print 'total amount spent = ' + str(total_amount)

        utxos = self.taker.wallet.select_utxos(self.taker.mixdepth,
                                               total_amount)
        self.taker.cjtx = takermodule.CoinJoinTX(
            self.taker, self.taker.amount, orders, utxos, self.taker.destaddr,
            self.taker.wallet.get_change_addr(self.taker.mixdepth),
            self.taker.txfee, self.finishcallback)
        '''
		counterparty, oid, cj_amount = choose_sweep_order(addrvalue['value'], my_tx_fee)
		cjtx = CoinJoinTX(self.taker, cj_amount, [counterparty], [int(oid)],
			[utxo], self.taker.wallet.get_receive_addr(mixing_depth=1), None,
			my_tx_fee, self.finished_cj_callback)
		'''


class SendPayment(takermodule.Taker):

    def __init__(self, wallet, keyfile, destaddr, amount, makercount, txfee,
                 waittime, mixdepth):
        takermodule.Taker.__init__(self, keyfile)
        self.wallet = wallet
        self.destaddr = destaddr
        self.amount = amount
        self.makercount = makercount
        self.txfee = txfee
        self.waittime = waittime
        self.mixdepth = mixdepth

    def on_welcome(self):
        takermodule.Taker.on_welcome(self)
        PaymentThread(self).start()


def main():
    parser = OptionParser(
        usage='usage: %prog [options] [seed] [amount] [destaddr]',
        description='Sends a single payment from the zero mixing depth of your '
        + ' wallet to an given address using coinjoin and then switches off.')
    parser.add_option('-f',
                      '--txfee',
                      action='store',
                      type='int',
                      dest='txfee',
                      default=10000,
                      help='miner fee contribution, in satoshis, default=10000')
    parser.add_option(
        '-w',
        '--wait-time',
        action='store',
        type='float',
        dest='waittime',
        help='wait time in seconds to allow orders to arrive, default=5',
        default=5)
    parser.add_option('-N',
                      '--makercount',
                      action='store',
                      type='int',
                      dest='makercount',
                      help='how many makers to coinjoin with, default=2',
                      default=2)
    parser.add_option('-m',
                      '--mixdepth',
                      action='store',
                      type='int',
                      dest='mixdepth',
                      help='mixing depth to spend from, default=0',
                      default=0)
    (options, args) = parser.parse_args()

    if len(args) < 3:
        parser.error('Needs a seed, amount and destination address')
        sys.exit(0)
    seed = args[0]
    amount = int(args[1])
    destaddr = args[2]

    from socket import gethostname
    nickname = 'payer-' + btc.sha256(gethostname())[:6]

    wallet = Wallet(seed)
    wallet.sync_wallet()
    keyfile = 'keyfile-' + str(seed) + '.txt'

    print 'starting irc'
    taker = SendPayment(wallet, keyfile, destaddr, amount, options.makercount,
                        options.txfee, options.waittime, options.mixdepth)
    try:
        taker.run(HOST, PORT, nickname, CHANNEL)
    finally:
        debug('CRASHING, DUMPING EVERYTHING')
        debug('wallet seed = ' + seed)
        debug_dump_object(wallet, ['addr_cache'])
        debug_dump_object(taker)


if __name__ == "__main__":
    main()
    print('done')
