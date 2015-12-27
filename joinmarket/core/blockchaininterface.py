from __future__ import absolute_import, print_function

import hashlib
import json
import random
import re
import time
import urllib
from decimal import Decimal

# This can be removed once CliJsonRpc is gone.
import subprocess

import collections
import binascii
from twisted.web import server as twisted_server
from twisted.web import resource as twisted_resource
from twisted.internet import defer, reactor
from twisted.internet.protocol import DatagramProtocol
from twisted.logger import Logger

import treq
from txzmq import ZmqEndpoint, ZmqFactory, ZmqSubConnection

from . import jmbtc as btc
from .abstracts import BlockchainInterface

from .jsonrpc import JsonRpcConnectionError, JsonRpc
from .support import chunks, system_shutdown
from .configure import config, get_network


log = Logger()


class CliJsonRpc(object):
    """
    Fake JsonRpc class that uses the Bitcoin CLI executable.  This is used
    as temporary fall back before we switch completely (and exclusively)
    to the real JSON-RPC interface.
    """

    def __init__(self, cli, testnet):
        self.cli = cli
        if testnet:
            self.cli.append("-testnet")

    def call(self, method, params):
        fullCall = []
        fullCall.extend(self.cli)
        fullCall.append(method)
        for p in params:
            if isinstance(p, basestring):
                fullCall.append(p)
            else:
                fullCall.append(json.dumps(p))

        res = subprocess.check_output(fullCall)

        if res == '':
            return None

        try:
            return json.loads(res)
        except ValueError:
            return res.strip()


class BlockrInterface(BlockchainInterface):
    BLOCKR_MAX_ADDR_REQ_COUNT = 20

    def __init__(self, testnet=False):
        super(BlockrInterface, self).__init__()

        # see bci.py in bitcoin module
        self.network = 'testnet' if testnet else 'btc'
        self.blockr_domain = 'tbtc' if testnet else 'btc'
        self.last_sync_unspent = 0

    def add_tx_notify(self, trw):
        unconfirm_timeout = 10 * 60  # seconds
        unconfirm_poll_period = 5
        confirm_timeout = 2 * 60 * 60
        confirm_poll_period = 5 * 60

        # AsyncWebSucker returns immediately.  The yields are non-blocking
        # calls managed by Twisted using the miracle of generators.  The
        # illusion of blocking

        # todo: Blockr async needs testing and rearchitecture!!

        @defer.inlineCallbacks
        def AsyncWebSucker():
            blockr_domain = self.blockr_domain
            tx_output_set = set([(sv['script'], sv['value']) for sv in trw.txd[
                'outs']])
            output_addresses = [
                btc.script_to_address(scrval[0])
                for scrval in tx_output_set
            ]

            # log.debug('txoutset=' + pprint.pformat(tx_output_set))
            # log.debug('outaddrs=' + ','.join(output_addresses))

            def sleepGenerator(seconds):
                d = defer.Deferred()
                reactor.callLater(seconds, d.callback, seconds)
                return d

            st = int(time.time())
            unconfirmed_txid = None
            unconfirmed_txhex = None
            while not unconfirmed_txid:

                # time.sleep(unconfirm_poll_period)
                yield sleepGenerator(unconfirm_poll_period)

                if int(time.time()) - st > unconfirm_timeout:
                    log.debug('checking for unconfirmed tx timed out')
                    return
                blockr_url = 'https://' + blockr_domain
                blockr_url += '.blockr.io/api/v1/address/unspent/'

                # seriously weird bug with blockr.io
                random.shuffle(output_addresses)

                # >>> A non-blocking call (Black Magic)
                res = yield treq.get('{}?unconfirmed=1'.format(
                    blockr_url + ','.join(output_addresses)))

                data = json.loads(res)['data']

                shared_txid = None
                for unspent_list in data:
                    txs = set([str(txdata['tx'])
                               for txdata in unspent_list['unspent']])
                    if not shared_txid:
                        shared_txid = txs
                    else:
                        shared_txid = shared_txid.intersection(txs)
                log.debug('sharedtxid = ' + str(shared_txid))
                if len(shared_txid) == 0:
                    continue

                # here for some race condition bullshit with blockr.io
                # time.sleep(2)
                yield sleepGenerator(2)

                blockr_url = 'https://' + blockr_domain
                blockr_url += '.blockr.io/api/v1/tx/raw/'

                # data = json.loads(btc.make_request(blockr_url + ','.join(
                #         shared_txid)))['data']

                res = yield treq.get(blockr_url + ','.join(shared_txid))

                data = json.loads(res)['data']

                if not isinstance(data, list):
                    data = [data]
                for txinfo in data:
                    txhex = str(txinfo['tx']['hex'])
                    outs = set([(sv['script'], sv['value'])
                                for sv in btc.deserialize(txhex)['outs']])

                    log.debug('unconfirm query outs = ' + str(outs))
                    if outs == tx_output_set:
                        unconfirmed_txid = txinfo['tx']['txid']
                        unconfirmed_txhex = str(txinfo['tx']['hex'])
                        break

            trw.unconfirmfun(btc.deserialize(unconfirmed_txhex),
                             unconfirmed_txid)

            st = int(time.time())
            confirmed_txid = None
            confirmed_txhex = None
            while not confirmed_txid:
                # time.sleep(confirm_poll_period)
                yield sleepGenerator(confirm_poll_period)

                if int(time.time()) - st > confirm_timeout:
                    log.debug('checking for confirmed tx timed out')
                    return
                blockr_url = 'https://' + blockr_domain
                blockr_url += '.blockr.io/api/v1/address/txs/'

                # data = json.loads(btc.make_request(blockr_url + ','.join(
                #         self.output_addresses)))['data']

                res = yield treq.get(blockr_url + ','.join(output_addresses))

                data = json.loads(res)['data']

                shared_txid = None
                for addrtxs in data:
                    txs = set(str(txdata['tx']) for txdata in addrtxs['txs'])
                    if not shared_txid:
                        shared_txid = txs
                    else:
                        shared_txid = shared_txid.intersection(txs)
                log.debug('sharedtxid = ' + str(shared_txid))
                if len(shared_txid) == 0:
                    continue
                blockr_url = 'https://' + blockr_domain
                blockr_url += '.blockr.io/api/v1/tx/raw/'
                # data = json.loads(
                #         btc.make_request(
                #                 blockr_url + ','.join(shared_txid)))['data']

                res = yield treq.get(blockr_url + ','.join(shared_txid))

                data = json.loads(res)['data']

                if not isinstance(data, list):
                    data = [data]
                for txinfo in data:
                    txhex = str(txinfo['tx']['hex'])
                    outs = set([(sv['script'], sv['value'])
                                for sv in btc.deserialize(txhex)['outs']])
                    log.debug('confirm query outs = ' + str(outs))
                    if outs == tx_output_set:
                        confirmed_txid = txinfo['tx']['txid']
                        confirmed_txhex = str(txinfo['tx']['hex'])
                        break
            trw.confirmfun(btc.deserialize(confirmed_txhex), confirmed_txid, 1)

        # AsyncWebSucker returns a deferred.. but it doesn't matter because
        # nobody uses the return... magic!!!
        AsyncWebSucker()

    def pushtx(self, txhex):
        try:
            json_str = btc.blockr_pushtx(txhex, self.network)
        except Exception:
            log.debug('failed blockr.io pushtx')
            return None
        data = json.loads(json_str)
        if data['status'] != 'success':
            log.debug(data)
            return None
        return data['data']

    def query_utxo_set(self, txout):
        if not isinstance(txout, list):
            txout = [txout]
        txids = [h[:64] for h in txout]
        txids = list(set(txids))  # remove duplicates
        # self.BLOCKR_MAX_ADDR_REQ_COUNT = 2
        if len(txids) > self.BLOCKR_MAX_ADDR_REQ_COUNT:
            txids = chunks(txids, self.BLOCKR_MAX_ADDR_REQ_COUNT)
        else:
            txids = [txids]
        data = []
        for ids in txids:
            blockr_url = 'https://' + self.blockr_domain + '.blockr.io/api/v1/tx/info/'
            blockr_data = json.loads(btc.make_request(blockr_url + ','.join(
                ids)))['data']
            if not isinstance(blockr_data, list):
                blockr_data = [blockr_data]
            data += blockr_data
        result = []
        for txo in txout:
            txdata = [d for d in data if d['tx'] == txo[:64]][0]
            vout = [v for v in txdata['vouts'] if v['n'] == int(txo[65:])][0]
            if vout['is_spent'] == 1:
                result.append(None)
            else:
                result.append({'value': int(Decimal(vout['amount']) * Decimal(
                    '1e8')),
                               'address': vout['address'],
                               'script': vout['extras']['script']})
        return result

class JmZmq(object):
    def __init__(self, endpoint):
        self.log = Logger(self.__module__)
        try:
            zf = ZmqFactory()
            e = ZmqEndpoint('connect', endpoint)
            s = ZmqSubConnection(zf, e)
            s.subscribe('rawtx')
            s.gotMessage = self.receive
        except:
            self.log.failure('ZMQ failure')

    @staticmethod
    def hexHashRaw(raw):
        # raw transaction (as provided by Zmq) to binhash
        return binascii.hexlify(
                hashlib.sha256(
                        hashlib.sha256(raw).digest()).digest()[::-1])

    def receive(self, *args):
        msg, channel = args

        if channel != 'rawtx':
            return

        txhash_hex = self.hexHashRaw(msg)

        txd = btc.json_changebase(btc.deserialize(msg),
                                  lambda x: btc.safe_hexlify(x))

        # self.log.debug('transaction: size={txlen}, {txhash_hex}',
        #                txlen=len(msg), txhash_hex=txhash_hex)

        bc_interface.process_raw_tx(txd, txhash_hex)


class MultiCast(DatagramProtocol):

    def __init__(self):
        reactor.listenMulticast(8005, self, listenMultiple=True)

    def startProtocol(self):
        # Set the TTL>1 so multicast will cross router hops:
        self.transport.setTTL(5)
        self.transport.joinGroup("228.0.0.5")
        log.debug('Multicast startProtocol')

    def writeDatagram(self, datagram):
        self.transport.write(datagram, ('228.0.0.5', 8005))

    def datagramReceived(self, datagram, address):
        self.process(datagram)

    @staticmethod
    def process(path):
        log.debug(path)
        pages = ('/walletnotify?', '/alertnotify?')

        if path.startswith('/walletnotify?'):
            txid = path[len(pages[0]):]
            if not re.match('^[0-9a-fA-F]*$', txid):
                log.debug('not a txid: {}'.format(txid))
                return
            tx = bc_interface.rpc('getrawtransaction', [txid])
            if not re.match('^[0-9a-fA-F]*$', tx):
                log.debug('not a txhex')
                return
            txd = btc.deserialize(tx)
            bc_interface.process_raw_tx(txd, txid)

        elif path.startswith('/alertnotify?'):
            core_alert = urllib.unquote(path[len(pages[1]):])
            log.warn('Bitcoin alert!: {core_alert}', core_alert=core_alert)


# noinspection PyMissingConstructor
class NotifyHttpServer(twisted_resource.Resource):

    isLeaf = True

    def __init__(self):
        log.debug('firing up http and multicast')
        self.using_port = None
        # launch multicast
        self.multicast = MultiCast()

    def render_GET(self, request):
        # log.debug('url received: {}'.format(request.uri))
        self.multicast.writeDatagram(request.uri)
        return ''


# TODO must add the tx addresses as watchonly if case we ever broadcast a tx
# with addresses not belonging to us
class BitcoinCoreInterface(BlockchainInterface):

    def __init__(self, jsonRpc, network):
        super(BitcoinCoreInterface, self).__init__()
        self.jsonRpc = jsonRpc

        blockchainInfo = self.jsonRpc.call("getblockchaininfo", [])
        actualNet = blockchainInfo['chain']

        netmap = {'main': 'mainnet', 'test': 'testnet', 'regtest': 'regtest'}
        if netmap[actualNet] != network:
            raise Exception('wrong network configured')

        self.http_server = None
        self.zmq_server = None

        self.stochQ = collections.deque()
        self.txnotify_fun = collections.defaultdict(set)
        if 'zmq_endpoint' in config.options('BLOCKCHAIN'):
            endpoint = config.get('BLOCKCHAIN', 'zmq_endpoint')
            self.zmq_server = JmZmq(endpoint)
        if 'notify_port' in config.options('BLOCKCHAIN'):
            self.start_http_server()

    def rpc(self, method, args, immediate=False):
        """
        wraps jsonrpc.  immediate returns Twisted deferred.  The intent
        is that many calls don't care about the result (or the deferred)
        :param method:
        :param args:
        :param immediate:
        :return:
        """
        # if method not in ['importaddress', 'walletpassphrase']:
        #      log.debug('rpc: ' + method + " " + str(args))
        res = self.jsonRpc.call(method, args, immediate)
        if not immediate and isinstance(res, unicode):
            res = str(res)
        return res

    def add_watchonly_addresses(self, addr_list, wallet_name):
        log.debug('importing {numaddr} addresses into account {acct}',
                  numaddr=len(addr_list), acct=wallet_name)
        for addr in addr_list:
            self.rpc('importaddress',
                     [addr, wallet_name, False],
                     immediate=True)
        log.debug('done importing - async possible')
        if config.get("BLOCKCHAIN", "blockchain_source") != 'regtest':
            system_shutdown('restart Bitcoin Core with -rescan if you\'re '
                            'recovering an existing wallet from backup seed\n'
                            ' otherwise just restart this joinmarket script')
            # sys.exit(0)


    def start_http_server(self):
        log.debug('**** start_http_server')

        class JmSrv(twisted_server.Site):
            def __init__(self, srv):
                twisted_server.Site.__init__(self, srv)

            def log(self, _):
                pass            # SHUT UP!!!

        srv = NotifyHttpServer()
        self.http_server = JmSrv(srv)
        notify_host = 'localhost'
        notify_port = 62602  # defaults
        if 'notify_host' in config.options("BLOCKCHAIN"):
            notify_host = config.get("BLOCKCHAIN", "notify_host").strip()
        if 'notify_port' in config.options("BLOCKCHAIN"):
            notify_port = int(config.get("BLOCKCHAIN", "notify_port"))
        for inc in range(10):
            hostport = (notify_host, notify_port + inc)
            log.debug('start_http_server trying hostport: {}:{:d}'.format(
                notify_host, notify_port))
            try:
                reactor.listenTCP(hostport[1], self.http_server)
            except:
                pass
            else:
                log.debug('start_http_server, using '
                          'hostport= {}'.format(hostport))
                srv.using_port = hostport[1]
                break

    def add_tx_notify(self, trw):

        one_addr_imported = False
        for outs in trw.txd['outs']:
            addr = btc.script_to_address(outs['script'])
            if self.rpc('getaccount', [addr]) != '':
                one_addr_imported = True
                break

        if not one_addr_imported:
            self.rpc('importaddress',
                     [trw.cj_addr, 'joinmarket-notify', False],
                     immediate=True)

        tx_output_set = frozenset([(sv['script'], sv['value'])
                                   for sv in trw.txd['outs']])

        trw.log.debug('inside add_tx_notify hash: {hash}',
                  hash=hash(tx_output_set))

        # todo: the one shared object... dangerous and beautiful
        self.txnotify_fun[tx_output_set].add(trw)

    def process_raw_tx(self, txd, txid):
        tx_output_set = frozenset([(sv['script'], sv['value'])
                                   for sv in txd['outs']])

        hashout = hash(tx_output_set)

        if tx_output_set in self.txnotify_fun:
            # on rare occasions people spend their output without waiting
            #  for a confirm
            txdata = None
            for n in range(len(txd['outs'])):
                txdata = bc_interface.rpc('gettxout', [txid, n, True])
                if txdata is not None:
                    break
            assert txdata is not None

            def doUnqFunc():
                callee, argv = self.stochQ.popleft()
                callee(*argv)

            # todo: the one shared object
            trw_list = list(self.txnotify_fun[tx_output_set])
            random.shuffle(trw_list)
            if txdata['confirmations'] == 0:
                log.debug('unconfirmfun: {txid}, {hash}', txid=txid,
                          hash=hashout)
                for trw in trw_list:
                    self.stochQ.append((trw.unconfirmfun, [txd, txid]))
                    delay = 1.0 + 2.0 * random.random()
                    reactor.callLater(delay, doUnqFunc)
            else:
                log.debug('CoNfIrMeDd: {txid}, {hash}', txid=txid,
                          hash=hashout)
                for trw in trw_list:
                    self.stochQ.append(
                            (trw.send_confirm,
                             [txd, txid, txdata['confirmations']]))
                    delay = 5.0 * random.random()
                    reactor.callLater(delay, doUnqFunc)

                del bc_interface.txnotify_fun[tx_output_set]


    def pushtx(self, txhex):
        try:
            # we don't need to wait for the return of this RPC.  Evaluate
            # locally and return after an immediate invocation
            self.rpc('sendrawtransaction', [txhex], immediate=True)
            return btc.txhash(txhex)
        except JsonRpcConnectionError:
            return None

    # noinspection PyTypeChecker
    def query_utxo_set(self, txout):
        if not isinstance(txout, list):
            txout = [txout]
        result = []
        for txo in txout:
            ret = self.rpc('gettxout', [txo[:64], int(txo[65:]), False])
            if ret is None:
                result.append(None)
            else:
                result.append({'value': int(Decimal(str(ret['value'])) *
                                            Decimal('1e8')),
                               'address': ret['scriptPubKey']['addresses'][0],
                               'script': ret['scriptPubKey']['hex']})
        return result


# class for regtest chain access
# running on local daemon. Only
# to be instantiated after network is up
# with > 100 blocks.
class RegtestBitcoinCoreInterface(BitcoinCoreInterface):

    def __init__(self, jsonRpc):
        super(RegtestBitcoinCoreInterface, self).__init__(jsonRpc, 'regtest')
        self.confirmTimer = None

    def notify(self):
        # only one confirm timer at a time
        if self.confirmTimer is None:
            self.confirmTimer = reactor.callLater(
                    15, self.tick_forward_chain, 1)

    def pushtx(self, txhex):
        self.log.debug('regtest pushtx: {ltx}', ltx=len(txhex))
        ret = super(RegtestBitcoinCoreInterface, self).pushtx(txhex)

        self.notify()

        return ret

    def tick_forward_chain(self, n):
        """
        generate() for regtest only;
        instruct to mine n blocks.
        """
        self.confirmTimer = None
        self.rpc('generate', [n], immediate=True)

    def grab_coins(self, receiving_addr, amt=50):
        """
        NOTE! amt is passed in Coins, not Satoshis!
        Special method for regtest only:
        take coins from bitcoind's own wallet
        and put them in the receiving addr.
        Return the txid.
        """
        if amt > 500:
            raise Exception("too greedy")
        """
        if amt > self.current_balance:
        #mine enough to get to the reqd amt
        reqd = int(amt - self.current_balance)
        reqd_blocks = int(reqd/50) +1
        if self.rpc('setgenerate', [True, reqd_blocks]):
        raise Exception("Something went wrong")
        """
        # now we do a custom create transaction and push to the receiver
        txid = self.rpc('sendtoaddress', [receiving_addr, amt])
        if not txid:
            raise Exception("Failed to broadcast transaction")
        # confirm
        self.tick_forward_chain(1)
        # self.notify()
        return txid

    def get_received_by_addr(self, addresses, query_params):
        # todo: this is unused
        # NB This will NOT return coinbase coins (but wont matter in our use
        # case). allow importaddress to fail in case the address is already
        # in the wallet
        res = []
        for address in addresses:
            self.rpc('importaddress', [address, 'watchonly'], immediate=True)
            r = self.rpc('getreceivedbyaddress', [address])
            res.append({'address': address,
                        'balance': int(Decimal(1e8) * Decimal(r))})
        return {'data': res}

def _get_blockchain_interface_instance():

    source = config.get("BLOCKCHAIN", "blockchain_source")
    network = get_network()
    testnet = network == 'testnet'
    if source == 'bitcoin-rpc':
        rpc_host = config.get("BLOCKCHAIN", "rpc_host")
        rpc_port = config.get("BLOCKCHAIN", "rpc_port")
        rpc_user = config.get("BLOCKCHAIN", "rpc_user")
        rpc_password = config.get("BLOCKCHAIN", "rpc_password")
        rpc = JsonRpc(rpc_host, rpc_port, rpc_user, rpc_password)
        bc_interface = BitcoinCoreInterface(rpc, network)
    elif source == 'json-rpc':
        bitcoin_cli_cmd = config.get("BLOCKCHAIN", "bitcoin_cli_cmd").split(' ')
        rpc = CliJsonRpc(bitcoin_cli_cmd, testnet)
        bc_interface = BitcoinCoreInterface(rpc, network)
    elif source == 'regtest':
        rpc_host = config.get("BLOCKCHAIN", "rpc_host")
        rpc_port = config.get("BLOCKCHAIN", "rpc_port")
        rpc_user = config.get("BLOCKCHAIN", "rpc_user")
        rpc_password = config.get("BLOCKCHAIN", "rpc_password")
        rpc = JsonRpc(rpc_host, rpc_port, rpc_user, rpc_password)
        bc_interface = RegtestBitcoinCoreInterface(rpc)
    elif source == 'blockr':
        bc_interface = BlockrInterface(testnet)
    else:
        raise ValueError("Invalid blockchain source")
    return bc_interface

# ----------------------------------------------
# construct the one shared instance
# ----------------------------------------------

bc_interface = _get_blockchain_interface_instance()

__all__ = ('bc_interface', 'BlockrInterface')
