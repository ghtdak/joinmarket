import datetime, threading, binascii, sys, os, copy
data_dir = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(data_dir, 'lib'))

import taker as takermodule
import common
from common import *
from irc import IRCMessageChannel

from optparse import OptionParser
import numpy as np
from pprint import pprint

orderwaittime = 5


def lower_bounded_int(thelist, lowerbound):
    return [int(l) if int(l) >= lowerbound else lowerbound for l in thelist]


def generate_tumbler_tx(destaddrs, options):
    #sends the coins up through a few mixing depths
    #send to the destination addresses from different mixing depths

    #simple algo, move coins completely from one mixing depth to the next
    # until you get to the end, then send to destaddrs

    #txcounts for going completely from one mixdepth to the next
    # follows a normal distribution
    txcounts = np.random.normal(options.txcountparams[0],
                                options.txcountparams[1], options.mixdepthcount)
    txcounts = lower_bounded_int(txcounts, 1)
    tx_list = []
    for m, txcount in enumerate(txcounts):
        #assume that the sizes of outputs will follow a power law
        amount_ratios = 1.0 - np.random.power(options.amountpower, txcount)
        amount_ratios /= sum(amount_ratios)
        #transaction times are uncorrelated
        #time between events in a poisson process followed exp
        waits = np.random.exponential(options.timelambda, txcount)
        #number of makers to use follows a normal distribution
        makercounts = np.random.normal(options.makercountrange[0],
                                       options.makercountrange[1], txcount)
        makercounts = lower_bounded_int(makercounts, 2)
        for amount_ratio, wait, makercount in zip(amount_ratios, waits,
                                                  makercounts):
            tx = {
                'amount_ratio': amount_ratio,
                'wait': round(wait, 2),
                'srcmixdepth': m + options.mixdepthsrc,
                'makercount': makercount,
                'dest': 'internal'
            }
            tx_list.append(tx)

    total_dest_addr = len(destaddrs) + options.addrask
    external_dest_addrs = destaddrs + ['addrask'] * options.addrask
    if total_dest_addr >= options.mixdepthcount:
        print 'not enough mixing depths to pay to all destination addresses'
        return None
    for i, srcmix in enumerate(range(options.mixdepthcount - total_dest_addr,
                                     options.mixdepthcount)):
        for tx in reversed(tx_list):
            if tx['srcmixdepth'] == srcmix:
                tx['dest'] = external_dest_addrs[i]
                break
        if total_dest_addr - i != 1:
            continue
        tx_list_remove = []
        for tx in tx_list:
            if tx['srcmixdepth'] == srcmix:
                if tx['dest'] == 'internal':
                    tx_list_remove.append(tx)
                else:
                    tx['amount_ratio'] = 1.0
        [tx_list.remove(t) for t in tx_list_remove]
    return tx_list


#thread which does the buy-side algorithm
# chooses which coinjoins to initiate and when
class TumblerThread(threading.Thread):

    def __init__(self, taker):
        threading.Thread.__init__(self)
        self.daemon = True
        self.taker = taker

    def unconfirm_callback(self, txd, txid):
        pass

    def confirm_callback(self, txd, txid, confirmations):
        self.taker.wallet.add_new_utxos(txd, txid)
        self.lockcond.acquire()
        self.lockcond.notify()
        self.lockcond.release()

    def finishcallback(self, coinjointx):
        common.bc_interface.add_tx_notify(coinjointx.latest_tx,
                                          self.unconfirm_callback,
                                          self.confirm_callback)
        self.taker.wallet.remove_old_utxos(coinjointx.latest_tx)

    def send_tx(self, tx, balance, sweep, i, l):
        destaddr = None
        if tx['dest'] == 'internal':
            destaddr = self.taker.wallet.get_receive_addr(tx['srcmixdepth'] + 1)
        elif tx['dest'] == 'addrask':
            destaddr = raw_input('insert new address: ')
        else:
            destaddr = tx['dest']

        if sweep:
            print 'sweeping'
            all_utxos = self.taker.wallet.get_utxos_by_mixdepth(
            )[tx['srcmixdepth']]
            total_value = sum([addrval['value'] for addrval in all_utxos.values(
            )])
            orders, cjamount = choose_sweep_order(
                self.taker.db, total_value, self.taker.txfee, tx['makercount'])
            self.taker.start_cj(self.taker.wallet, cjamount, orders, all_utxos,
                                destaddr, None, self.taker.txfee,
                                self.finishcallback)
        else:
            amount = int(tx['amount_ratio'] * balance)
            changeaddr = self.taker.wallet.get_change_addr(tx['srcmixdepth'])
            print 'coinjoining ' + str(amount)
            while True:
                orders, total_cj_fee = choose_order(self.taker.db, amount,
                                                    tx['makercount'])
                cj_fee = 1.0 * total_cj_fee / tx['makercount'] / amount
                if cj_fee < self.taker.maxcjfee:
                    break
                print 'cj fee too high at ' + str(
                    cj_fee) + ', waiting 10 seconds'
                time.sleep(10)
            print 'chosen orders to fill ' + str(orders) + ' totalcjfee=' + str(
                total_cj_fee)
            total_amount = amount + total_cj_fee + self.taker.txfee
            print 'total amount spent = ' + str(total_amount)

            utxos = self.taker.wallet.select_utxos(tx['srcmixdepth'], amount)
            self.taker.start_cj(self.taker.wallet, amount, orders, utxos,
                                destaddr, changeaddr, self.taker.txfee,
                                self.finishcallback)

        print 'that was %d tx out of %d' % (i, l)
        self.lockcond.acquire()
        self.lockcond.wait()
        self.lockcond.release()
        debug('tx confirmed, waiting for ' + str(tx['wait']) + ' minutes')
        time.sleep(tx['wait'] * 60)
        print 'woken'

    def run(self):
        print 'waiting for all orders to certainly arrive'
        time.sleep(orderwaittime)

        sqlorders = self.taker.db.execute(
            'SELECT cjfee, ordertype FROM orderbook;').fetchall()
        orders = [o['cjfee'] for o in sqlorders if o['ordertype'] == 'relorder']
        orders = sorted(orders)
        relorder_fee = float(orders[0])
        print 'relorder fee = ' + str(relorder_fee)
        maker_count = sum([tx['makercount'] for tx in self.taker.tx_list])
        print('uses ' + str(maker_count) + ' makers, at ' + str(
            relorder_fee * 100) + '% per maker, estimated total cost ' +
              str(round((1 - (1 - relorder_fee) ** maker_count) * 100, 3)) + '%'
       )

        time.sleep(orderwaittime)
        print 'starting'
        self.lockcond = threading.Condition()

        self.balance_by_mixdepth = {}
        for i, tx in enumerate(self.taker.tx_list):
            if tx['srcmixdepth'] not in self.balance_by_mixdepth:
                self.balance_by_mixdepth[tx['srcmixdepth']
                            ] = self.taker.wallet.get_balance_by_mixdepth(
                            )[tx['srcmixdepth']]
            sweep = True
            for later_tx in self.taker.tx_list[i + 1:]:
                if later_tx['srcmixdepth'] == tx['srcmixdepth']:
                    sweep = False
            self.send_tx(tx, self.balance_by_mixdepth[tx['srcmixdepth']], sweep,
                         i, len(self.taker.tx_list))

        print 'total finished'
        self.taker.msgchan.shutdown()
        '''
		crow = self.taker.db.execute('SELECT COUNT(DISTINCT counterparty) FROM orderbook;').fetchone()
		counterparty_count = crow['COUNT(DISTINCT counterparty)']
		if counterparty_count < self.taker.makercount:
			print 'not enough counterparties to fill order, ending'
			self.taker.msgchan.shutdown()
			return
		'''


class Tumbler(takermodule.Taker):

    def __init__(self, msgchan, wallet, tx_list, txfee, maxcjfee):
        takermodule.Taker.__init__(self, msgchan)
        self.wallet = wallet
        self.tx_list = tx_list
        self.maxcjfee = maxcjfee
        self.txfee = txfee

    def on_welcome(self):
        takermodule.Taker.on_welcome(self)
        TumblerThread(self).start()


def main():
    parser = OptionParser(
        usage='usage: %prog [options] [seed] [destaddr...]',
        description=
        'Sends bitcoins to many different addresses using coinjoin in'
        ' an attempt to break the link between them. Sending to multiple '
        ' addresses is highly recommended for privacy. This tumbler can'
        ' be configured to ask for more address mid-run, giving the user'
        ' a chance to click `Generate New Deposit Address` on whatever service'
        ' they are using.')
    parser.add_option('-m',
                      '--mixdepthsource',
                      type='int',
                      dest='mixdepthsrc',
                      help='mixing depth to spend from, default=0',
                      default=0)
    parser.add_option('-f',
                      '--txfee',
                      type='int',
                      dest='txfee',
                      default=10000,
                      help='miner fee contribution, in satoshis, default=10000')
    parser.add_option(
        '-x',
        '--maxcjfee',
        type='float',
        dest='maxcjfee',
        default=0.03,
        help=
        'maximum coinjoin fee the tumbler is willing to pay to a single market maker. default=0.03 (3%)')
    parser.add_option(
        '-a',
        '--addrask',
        type='int',
        dest='addrask',
        default=2,
        help='How many more addresses to ask for in the terminal. Should '
        'be similar to --txcountparams. default=2')
    parser.add_option(
        '-N',
        '--makercountrange',
        type='float',
        nargs=2,
        action='store',
        dest='makercountrange',
        help=
        'Input the range of makers to use. e.g. 3-5 will random use between '
        '3 and 5 makers inclusive, default=3 4',
        default=(3, 1))
    parser.add_option('-M',
                      '--mixdepthcount',
                      type='int',
                      dest='mixdepthcount',
                      help='How many mixing depths to mix through',
                      default=4)
    parser.add_option(
        '-c',
        '--txcountparams',
        type='float',
        nargs=2,
        dest='txcountparams',
        default=(5, 1),
        help=
        'The number of transactions to take coins from one mixing depth to the next, it is'
        ' randomly chosen following a normal distribution. Should be similar to --addrask. '
        'This option controlls the parameters of that normal curve. (mean, standard deviation). default=(3, 1)')
    parser.add_option(
        '--amountpower',
        type='float',
        dest='amountpower',
        default=100.0,
        help=
        'The output amounts follow a power law distribution, this is the power, default=100.0')
    parser.add_option(
        '-l',
        '--timelambda',
        type='float',
        dest='timelambda',
        default=2,
        help=
        'Average the number of minutes to wait between transactions. Randomly chosen '
        ' following an exponential distribution, which describes the time between uncorrelated'
        ' events. default=5')

    parser.add_option(
        '-w',
        '--wait-time',
        action='store',
        type='float',
        dest='waittime',
        help='wait time in seconds to allow orders to arrive, default=5',
        default=5)
    (options, args) = parser.parse_args()

    if len(args) < 2:
        parser.error('Needs a seed and destination addresses')
        sys.exit(0)
    seed = args[0]
    destaddrs = args[1:]

    common.load_program_config()

    if len(destaddrs) + options.addrask <= 1:
        print '=' * 50
        print 'WARNING: You are only using one destination address'
        print 'this is very bad for privacy'
        print '=' * 50

    print 'seed=' + seed
    print 'destaddrs=' + str(destaddrs)
    print str(options)
    tx_list = generate_tumbler_tx(destaddrs, options)

    tx_list2 = copy.deepcopy(tx_list)
    tx_dict = {}
    for tx in tx_list2:
        srcmixdepth = tx['srcmixdepth']
        tx.pop('srcmixdepth')
        if srcmixdepth not in tx_dict:
            tx_dict[srcmixdepth] = []
        tx_dict[srcmixdepth].append(tx)
    dbg_tx_list = []
    for srcmixdepth, txlist in tx_dict.iteritems():
        dbg_tx_list.append({'srcmixdepth': srcmixdepth, 'tx': txlist})
    pprint(dbg_tx_list)

    total_wait = sum([tx['wait'] for tx in tx_list])
    print 'waits in total for ' + str(len(tx_list)) + ' blocks and ' + str(
        total_wait) + ' minutes'
    total_block_and_wait = len(tx_list) * 10 + total_wait
    print('estimated time taken ' + str(total_block_and_wait) + ' minutes or ' +
          str(round(total_block_and_wait / 60.0, 2)) + ' hours')

    ret = raw_input('tumble with these tx? (y/n):')
    if ret[0] != 'y':
        return

    #a couple of modes
    #im-running-from-the-nsa, takes about 80 hours, costs a lot
    #python tumbler.py -a 10 -N 10 5 -c 10 5 -l 50 -M 10 seed 1xxx
    #
    #quick and cheap, takes about 90 minutes
    #python tumbler.py -N 2 1 -c 3 0.001 -l 10 -M 3 -a 1 seed 1xxx
    #
    #default, good enough for most, takes about 5 hours
    #python tumbler.py seed 1xxx
    #
    #for quick testing
    #python tumbler.py -N 2 1 -c 3 0.001 -l 0.1 -M 3 -a 0 seed 1xxx 1yyy
    wallet = Wallet(seed,
                    max_mix_depth=options.mixdepthsrc + options.mixdepthcount)
    common.bc_interface.sync_wallet(wallet)
    wallet.print_debug_wallet_info()

    common.nickname = 'tumbler-' + binascii.hexlify(os.urandom(4))
    irc = IRCMessageChannel(common.nickname)
    tumbler = Tumbler(irc, wallet, tx_list, options.txfee, options.maxcjfee)
    try:
        debug('connecting to irc')
        irc.run()
    except:
        debug('CRASHING, DUMPING EVERYTHING')
        debug('wallet seed = ' + seed)
        debug_dump_object(wallet, ['addr_cache', 'keys', 'seed'])
        debug_dump_object(tumbler)
        import traceback
        debug(traceback.format_exc())


if __name__ == "__main__":
    main()
    print('done')
