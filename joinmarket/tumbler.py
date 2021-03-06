from __future__ import absolute_import, print_function

import copy
import pprint
import sys
from optparse import OptionParser

from twisted.internet import defer, reactor
from twisted.logger import Logger

from joinmarket.core import jmbtc as btc
import joinmarket.core as jm

log = Logger()


def lower_bounded_int(thelist, lowerbound):
    return [int(l) if int(l) >= lowerbound else lowerbound for l in thelist]


def generate_tumbler_tx(destaddrs, options):
    # sends the coins up through a few mixing depths
    # send to the destination addresses from different mixing depths

    # simple algo, move coins completely from one mixing depth to the next
    # until you get to the end, then send to destaddrs

    # txcounts for going completely from one mixdepth to the next
    # follows a normal distribution
    txcounts = jm.rand_norm_array(options.txcountparams[0],
                                  options.txcountparams[1],
                                  options.mixdepthcount)
    txcounts = lower_bounded_int(txcounts, options.mintxcount)
    tx_list = []
    for m, txcount in enumerate(txcounts):
        # assume that the sizes of outputs will follow a power law
        amount_fractions = jm.rand_pow_array(options.amountpower, txcount)
        amount_fractions = [1.0 - x for x in amount_fractions]
        amount_fractions = [x / sum(amount_fractions) for x in amount_fractions]
        # transaction times are uncorrelated
        # time between events in a poisson process followed exp
        waits = jm.rand_exp_array(options.timelambda, txcount)
        # number of makers to use follows a normal distribution
        makercounts = jm.rand_norm_array(options.makercountrange[0],
                                         options.makercountrange[1], txcount)
        makercounts = lower_bounded_int(makercounts, options.minmakercount)
        if (m == options.mixdepthcount - options.addrcount and
                options.donateamount):
            tx_list.append({'amount_fraction': 0,
                            'wait': round(waits[0], 2),
                            'srcmixdepth': m + options.mixdepthsrc,
                            'makercount': makercounts[0],
                            'destination': 'internal'})
        for amount_fraction, wait, makercount in zip(amount_fractions, waits,
                                                     makercounts):
            tx = {'amount_fraction': amount_fraction,
                  'wait': round(wait, 2),
                  'srcmixdepth': m + options.mixdepthsrc,
                  'makercount': makercount,
                  'destination': 'internal'}
            tx_list.append(tx)

    addrask = options.addrcount - len(destaddrs)
    external_dest_addrs = ['addrask'] * addrask + destaddrs
    for mix_offset in range(options.addrcount):
        srcmix = options.mixdepthsrc + options.mixdepthcount - mix_offset - 1
        for tx in reversed(tx_list):
            if tx['srcmixdepth'] == srcmix:
                tx['destination'] = external_dest_addrs[mix_offset]
                break
        if mix_offset == 0:
            # setting last mixdepth to send all to dest
            tx_list_remove = []
            for tx in tx_list:
                if tx['srcmixdepth'] == srcmix:
                    if tx['destination'] == 'internal':
                        tx_list_remove.append(tx)
                    else:
                        tx['amount_fraction'] = 1.0
            [tx_list.remove(t) for t in tx_list_remove]
    return tx_list


class Tumbler(jm.Taker):

    def __init__(self, block_instance, wallet, tx_list, options):
        super(Tumbler, self).__init__(block_instance)
        self.wallet = wallet
        self.tx_list = tx_list
        self.options = options
        self.tumbler_thread = None

        self.daemon = True
        self.ignored_makers = []
        self.sweeping = False
        self.started = False
        self.chooseOrdersFunc = self.weighted_order_choose


    def on_welcome(self):
        """
        When reconnect is enabled, this could be a problem
        :return:
        """
        if not self.started:
            self.started = True
            super(Tumbler, self).on_welcome()
            self.log.debug('waiting for all orders to  arrive')
            reactor.callLater(self.options.waittime, self.start)

    @defer.inlineCallbacks
    def tumbler_choose_orders(self, cj_amount, makercount,
                              nonrespondants=None, active_nicks=None):

        if nonrespondants is None:
            nonrespondants = []
        if active_nicks is None:
            active_nicks = []
        self.ignored_makers += nonrespondants
        orders, total_cj_fee = None, None
        while True:

            orders, total_cj_fee = self.choose_orders(
                self.db, cj_amount, makercount,
                self.ignored_makers + active_nicks)
            abs_cj_fee = 1.0 * total_cj_fee / makercount
            rel_cj_fee = abs_cj_fee / cj_amount
            self.log.debug('rel/abs average fee = {rel_cj_fee} / {abs_cj_fee}',
                           rel_cj_fee=rel_cj_fee, abs_cj_fee=abs_cj_fee)
            if rel_cj_fee > self.options.maxcjfee[
                    0] and abs_cj_fee > self.options.maxcjfee[1]:
                self.log.debug('cj fee higher than maxcjfee, waiting {delay}',
                               delay=self.options.liquiditywait)

                yield jm.sleepGenerator(self.options.liquiditywait)
                continue
            if orders is None:
                self.log.debug('waiting for liquidity',
                               delay=self.options.liquiditywait)

                yield jm.sleepGenerator(self.options.liquiditywait)
                continue
            break
        # self.log.debug('chosen orders to fill {} totalcjfee={}'.format(
        #         orders, total_cj_fee))

        defer.returnValue((orders, total_cj_fee))

    choose_orders_recover = tumbler_choose_orders

    @defer.inlineCallbacks
    def create_tx(self, tx, sweep, balance, destaddr):
        while True:
            self.log.debug('create_tx: sweep: {sweep}, balance: {balance}, '
                           'destaddr: {destaddr}, tx below...',
                           sweep=sweep, balance=balance, destaddr=destaddr)
            print(pprint.pformat(tx))
            orders = None
            cj_amount = 0
            change_addr = None
            if sweep:
                # todo: add informational output back in
                self.log.debug('sweeping')
                utxos = self.wallet.get_utxos_by_mixdepth()[
                    tx['srcmixdepth']]
                total_value = sum([addrval['value']
                                   for addrval in utxos.values()])
                while True:
                    orders, cj_amount = self.choose_sweep_orders(
                        self.db, total_value, self.options.txfee,
                        tx['makercount'], self.ignored_makers)
                    if orders is None:
                        self.log.debug('waiting for liquidity ',
                                       delay=self.options.liquiditywait)
                        yield jm.sleepGenerator(self.options.liquiditywait)
                        continue
                    abs_cj_fee = 1.0 * (
                        total_value - cj_amount) / tx['makercount']
                    rel_cj_fee = abs_cj_fee / cj_amount
                    self.log.debug('rel/abs average fee = {} / {}'.format(
                            rel_cj_fee, abs_cj_fee))
                    if rel_cj_fee > self.options.maxcjfee[
                            0] and abs_cj_fee > self.options.maxcjfee[1]:
                        self.log.debug('cj fee higher than maxcjfee, waiting ',
                                       delay=self.options.liquiditywait)
                        jm.sleepGenerator(self.options.liquiditywait)
                        continue
                    break
            else:
                self.log.debug('not sweeping')
                if tx['amount_fraction'] == 0:
                    cj_amount = int(
                            balance * self.options.donateamount / 100.0)
                    destaddr = None
                else:
                    cj_amount = int(tx['amount_fraction'] * balance)

                if cj_amount < self.options.mincjamount:
                    self.log.debug('cj amount too low, bringing up')
                    cj_amount = self.options.mincjamount

                change_addr = self.wallet.get_change_addr(tx[
                    'srcmixdepth'])

                self.log.debug('coinjoining: {cj_amount} ',
                               cj_amount=cj_amount)

                orders, total_cj_fee = yield self.tumbler_choose_orders(
                        cj_amount, tx['makercount'])

                total_amount = cj_amount + total_cj_fee + self.options.txfee

                self.log.debug('total amount spent', total_amount=total_amount)

                try:
                    bbm = self.wallet.get_btc_mixdepth_list()

                    fees = 2 * self.options.txfee * tx['makercount']

                    self.log.debug('select_utxos - amount:{amount}, '
                                   'fees:{fees} balance by mixdepth: {bbm}',
                                   amount=total_amount/1e8, fees=fees, bbm=bbm)

                    utxos = self.wallet.select_utxos(
                            tx['srcmixdepth'], total_amount + fees)
                except btc.InsufficientFunds:
                    log.debug("Failed to select total amount + twice txfee "
                              "from wallet; Now select total amount.")
                    try:
                        utxos = self.wallet.select_utxos(
                                tx['srcmixdepth'], total_amount)
                    except btc.InsufficientFunds:
                        self.log.debug('well, insufficient funds anyways')
                        raise
                except:
                    raise

            cjtx = jm.CoinJoinTX(self, cj_amount, orders, utxos, destaddr,
                                 change_addr, self.options.txfee)

            coinjointx = yield cjtx.phase1()

            if coinjointx.all_responded:

                self.wallet.remove_old_utxos(coinjointx.txd)
                coinjointx.self_sign_and_push()

                self.log.debug('register for notification: ')

                txd, txid, confirmations = yield cjtx.confirm()
                if confirmations:
                    self.wallet.add_new_utxos(txd, txid)
                    self.log.debug('confirmed create_tx', txid=txid)
                    break                               # <---SUCCESS!!!
                else:
                    self.log.debug('unconfirmed create_tx', txid=txid)
            else:
                # todo: need to handle the tx restart.  why did the old
                # todo: taker recovery code do what it did?
                if coinjointx.txd is None:
                    self.log.debug("Ugh, cjtx.txd is None... Nightmare")
                # todo: Could be this ignored_makers handling is wrong
                # self.ignored_makers += coinjointx.nonrespondants
                self.log.debug('coinjointx unsuccessful')
                # lets do it agaoin

    @defer.inlineCallbacks
    def init_tx(self, tx, balance, sweep):
        log.debug('init_tx: balance={balance}, by mixdepth={bbm}',
                  balance=balance,
                  bbm=self.wallet.get_btc_mixdepth_list())

        destaddr = None
        if tx['destination'] == 'internal':
            destaddr = self.wallet.get_receive_addr(tx['srcmixdepth'] + 1)
        elif tx['destination'] == 'addrask':
            while True:
                destaddr = raw_input('insert new address: ')
                addr_valid, errormsg = jm.validate_address(destaddr)
                if addr_valid:
                    break
                print('Address ' + destaddr + ' invalid. ' + errormsg +
                      ' try again')
        else:
            destaddr = tx['destination']
        try:
            yield self.create_tx(tx, sweep, balance, destaddr)
        except:
            self.log.debug('exception, stopping')
            raise
        waitTime = tx['wait'] * 60
        self.log.debug('sleeping for {waitTime}', waitTime=waitTime)
        yield jm.sleepGenerator(waitTime)

    @defer.inlineCallbacks
    def start(self):
        sqlorders = self.db.execute(
            'SELECT cjfee, ordertype FROM orderbook;').fetchall()
        orders = [o['cjfee'] for o in sqlorders if o['ordertype'] == 'relorder']
        orders = sorted(orders)
        if len(orders) == 0:
            self.log.debug('There are no orders at all in the orderbook! '
                           'Is the bot connecting to the right server?')
            return
        relorder_fee = float(orders[0])
        self.log.info('set relorder fee',relorder_fee=relorder_fee)
        maker_count = sum([tx['makercount'] for tx in self.tx_list])

        self.log.info(
                'uses {mc} makers, at {rf}% per maker, estimated total cost '
                '{cost}%', mc=maker_count, rf=relorder_fee*100,
                cost=round((1 - (1 - relorder_fee)**maker_count) * 100, 3))

        self.log.debug('starting the big tumbler loop')

        mixdepth_balance = {}

        for i, tx in enumerate(self.tx_list):

            bbm = self.wallet.get_balance_by_mixdepth()

            tx_depth = tx['srcmixdepth']
            if tx_depth not in mixdepth_balance:
                mixdepth_balance[tx_depth] = bbm[tx_depth]

            """
            looks like this is asking if the current depth exists anywhere
            in the remaining list
            """
            sweep = True
            for later_tx in self.tx_list[i + 1:]:
                if later_tx['srcmixdepth'] == tx_depth:
                    sweep = False
            try:
                balance = mixdepth_balance[tx_depth]
                log.debug('big loop: tx_src={tx_src}, balance={balance}',
                          tx_src=tx_depth, balance=balance)

                yield self.init_tx(tx, balance, sweep)

            except btc.InsufficientFunds:
                self.log.info('insufficient funds... stopping')
                print('-+' * 40)
                break
            except:
                self.log.failure('big tumbler loop fail...')
                print('-+' * 40)
                break

        self.log.debug('total finished')
        # todo: shutdown policy needed
        # jm.system_shutdown(0)


def build_objects(argv=None):
    if not argv:
        argv=sys.argv

    parser = OptionParser(
            usage='usage: %prog [options] [wallet file] [destaddr(s)...]',
            description=(
                'Sends bitcoins to many different addresses using coinjoin in '
                ' an attempt to break the link between them. Sending to '
                'multiple  addresses is highly recommended for privacy. This '
                'tumbler canbe configured to ask for more address mid-run, '
                'giving the user  a chance to click `Generate New Deposit '
                'Address` on whatever service they are using. '))
    parser.add_option(
            '-m',
            '--mixdepthsource',
            type='int',
            dest='mixdepthsrc',
            help=( 'Mixing depth to spend from. Useful if a previous tumbler '
                   'run prematurely ended with coins being left in higher '
                   'mixing levels, this option can be used to resume without '
                   'needing to send to another address. default=0'),
            default=0)
    parser.add_option(
            '-f',
            '--txfee',
            type='int',
            dest='txfee',
            default=10000,
            help='total miner fee in satoshis, default=10000')
    parser.add_option(
            '-a',
            '--addrcount',
            type='int',
            dest='addrcount',
            default=3,
            help=('How many destination addresses in total should be used. If '
                  'not enough are given as command line arguments, the script '
                  'will ask for more. This parameter is required to stop '
                  'amount correlation. default=3'))
    parser.add_option(
            '-x',
            '--maxcjfee',
            type='float',
            dest='maxcjfee',
            nargs=2,
            default=(0.01, 10000),
            help=('maximum coinjoin fee and bitcoin value the tumbler is '
                  'willing to pay to a single market maker. Both values need '
                  'to be exceeded, so if the fee is 30% but only 500satoshi '
                  'is paid the tx will go ahead. default=0.01, 10000 (1%, '
                  '10000satoshi)'))
    parser.add_option(
            '-N',
            '--makercountrange',
            type='float',
            nargs=2,
            action='store',
            dest='makercountrange',
            help=('Input the mean and spread of number of makers to use. e.g. '
                  '3 1.5 will be a normal distribution with mean 3 and '
                  'standard deveation 1.5 inclusive, default=3 1.5'),
            default=(3, 1.5))
    parser.add_option(
            '--minmakercount',
            type='int',
            dest='minmakercount',
            default=2,
            help=('The minimum maker count in a transaction, random values '
                  'below this are clamped at this number. default=2'))
    parser.add_option(
            '-M',
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
            default=(4, 1),
            help=('The number of transactions to take coins from one mixing '
                  'depth to the next, it is randomly chosen following a '
                  'normal distribution. Should be similar to --addrask. This '
                  'option controls the parameters of the normal distribution '
                  'curve. (mean, standard deviation). default=(4, 1)'))
    parser.add_option(
            '--mintxcount',
            type='int',
            dest='mintxcount',
            default=1,
            help='The minimum transaction count per mixing level, default=1')
    parser.add_option(
            '--donateamount',
            type='float',
            dest='donateamount',
            default=0,
            help=('percent of funds to donate to joinmarket development, '
                  'or zero to opt out (default=0%)'))
    parser.add_option(
            '--amountpower',
            type='float',
            dest='amountpower',
            default=100.0,
            help=('The output amounts follow a power law distribution, '
                  'this is the power, default=100.0'))
    parser.add_option(
            '-l',
            '--timelambda',
            type='float',
            dest='timelambda',
            default=30,
            help=('Average the number of minutes to wait between '
                  'transactions. Randomly chosen  following an exponential '
                  'distribution, which describes the time between '
                  'uncorrelated events. default=30'))
    parser.add_option(
            '-w',
            '--wait-time',
            action='store',
            type='float',
            dest='waittime',
            help='wait time in seconds to allow orders to arrive, default=20',
            default=20)
    parser.add_option(
            '-s',
            '--mincjamount',
            type='int',
            dest='mincjamount',
            default=100000,
            help=('minimum coinjoin amount in transaction in satoshi, '
                  'default 100k'))
    parser.add_option(
            '-q',
            '--liquiditywait',
            type='int',
            dest='liquiditywait',
            default=60,
            help=('amount of seconds to wait after failing to choose suitable '
                  'orders before trying again, default 60'))
    (options, args) = parser.parse_args(argv[1:])

    if len(args) < 1:
        parser.error('Needs a wallet file')
        sys.exit(0)
    wallet_file = args[0]
    destaddrs = args[1:]
    print(destaddrs)

    for addr in destaddrs:
        addr_valid, errormsg = jm.validate_address(addr)
        if not addr_valid:
            log.warn('ERROR: Address {addr} invalid. {msg}',
                     addr=addr, msg=errormsg)
            # todo: its throwing me out but still works.  sup?

    if len(destaddrs) > options.addrcount:
        options.addrcount = len(destaddrs)
    if options.addrcount + 1 > options.mixdepthcount:
        print('not enough mixing depths to pay to all destination addresses, '
              'increasing mixdepthcount')
        options.mixdepthcount = options.addrcount + 1
    if options.donateamount > 10.0:
        # fat finger probably, or misunderstanding
        options.donateamount = 0.9

    print(str(options))
    tx_list = generate_tumbler_tx(destaddrs, options)
    if not tx_list:
        return

    log.debug('tx_list')
    print(pprint.pformat(tx_list))

    # tx_list2 = copy.deepcopy(tx_list)
    # tx_dict = {}
    # for tx in tx_list2:
    #     srcmixdepth = tx['srcmixdepth']
    #     tx.pop('srcmixdepth')
    #     if srcmixdepth not in tx_dict:
    #         tx_dict[srcmixdepth] = []
    #     tx_dict[srcmixdepth].append(tx)

    # dbg_tx_list = []
    # for srcmixdepth, txlist in tx_dict.iteritems():
    #     dbg_tx_list.append({'srcmixdepth': srcmixdepth, 'tx': txlist})
    # log.debug('tumbler transaction list', tx_dict=tx_dict)

    total_wait = sum([tx['wait'] for tx in tx_list])
    print('creates {len_tx} transactions in total\n'
          'waits in total for {len_tx} blocks and {wait} minutes'.format(
            len_tx=len(tx_list), wait=total_wait))

    total_block_and_wait = len(tx_list) * 10 + total_wait
    print('estimated time taken {wait_min} minutes or {wait_hrs} hours'.format(
            wait_min=total_block_and_wait,
            wait_hrs=round(total_block_and_wait / 60.0, 2)))

    if options.addrcount <= 1:
        print('=' * 50)
        print('WARNING: You are only using one destination address')
        print('this is very bad for privacy')
        print('=' * 50)

    # todo: re-enable this
    # ret = raw_input('tumble with these tx? (y/n):')
    # if ret[0] != 'y':
    #     return

    # NOTE: possibly out of date documentation
    # a couple of modes
    # im-running-from-the-nsa, takes about 80 hours, costs a lot
    # python tumbler.py -a 10 -N 10 5 -c 10 5 -l 50 -M 10 wallet_file 1xxx
    #
    # quick and cheap, takes about 90 minutes
    # python tumbler.py -N 2 1 -c 3 0.001 -l 10 -M 3 -a 1 wallet_file 1xxx
    #
    # default, good enough for most, takes about 5 hours
    # python tumbler.py wallet_file 1xxx
    #
    # for quick testing
    # python tumbler.py -N 2 1 -c 3 0.001 -l 0.1 -M 3 -a 0 wallet_file 1xxx 1yyy

    nickname = jm.random_nick()

    block_instance = jm.BlockInstance(nickname)

    mmd = options.mixdepthsrc + options.mixdepthcount
    wallet = jm.Wallet(wallet_file, max_mix_depth=mmd)

    log.debug('\n {nickname}, {argv}', nickname=nickname, argv=argv)

    wallet.sync_wallet()
    wallet.nickname = nickname  # logging support

    log.debug('starting tumbler')
    Tumbler(block_instance, wallet, tx_list, options)
    return block_instance
