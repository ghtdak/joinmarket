#! /usr/bin/env python

import sys, os
data_dir = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(data_dir, 'lib'))

import socket, json

from common import *
import common
import taker as takermodule
from irc import IRCMessageChannel, random_nick
import blockchaininterface

#https://github.com/neocogent/electrum/blob/mixer_plugin/plugins/mixer.py

#the error dialog should give a button to start the daemon


class DaemonInterface(blockchaininterface.BlockchainInterface):

    def __init__(self, serv):
        super(DaemonInterface, self).__init__()
        self.serv = serv

    def sync_addresses(self, wallet):
        raise RuntimeError('not implemented'
                          )  #not needed for the electrum plugin

    def sync_unspent(self, wallet):
        raise RuntimeError('not implemented'
                          )  #not needed for the electrum plugin

    def add_tx_notify(self, txd, unconfirmfun, confirmfun, notifyaddr):
        raise RuntimeError('not implemented'
                          )  #not needed for the electrum plugin

    def pushtx(self, txhex):
        pass

    def query_utxo_set(self, txouts):
        '''
		takes a utxo or a list of utxos
		returns None if they are spend or unconfirmed
		otherwise returns value in satoshis, address and output script
		'''
        #address and output script contain the same information btw


class ServerThread(threading.Thread):

    def __init__(self, taker):
        threading.Thread.__init__(self)
        self.daemon = True
        self.taker = taker
        self.order_cache = {}

    def send_json(self, js):
        jsonstr = json.dumps(js)
        debug('>> ' + str(js))
        self.sock.sendall(jsonstr + '\r\n')

    def recv_json(self):
        line = self.fd.readline()
        if line == None:
            return None
        if len(line) == 0:
            return None
        js = json.loads(line)
        debug('<< ' + str(js))
        return js

    def handle_js(self, js):
        if js['command'] == 'echo':
            self.send_json(js)
        elif js['command'] == 'reqtxfee':
            orders, total_cj_fee = choose_order(self.taker.db, js['cjamount'],
                                                js['makercount'],
                                                cheapest_order_choose)
            if not orders:
                debug('ERROR not enough liquidity in the orderbook, exiting')
                return  # TODO return an error to plugin
            print 'chosen orders to fill ' + str(orders) + ' totalcjfee=' + str(
                total_cj_fee)
            send.self_json({'command': 'cjfee', 'cjfee': total_cj_fee})
        #js = self.plugin.send_json_wait({'command': 'reqtxfee', 'cjamount': cj_amount, 'makercount': 2})

    def run(self):
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.bind(('localhost', 62600))
        server_sock.listen(1)
        while True:
            try:
                debug('listening on daemon port')
                self.sock, addr = server_sock.accept()
                debug('accepted connection')
                self.fd = self.sock.makefile()
                self.send_json({'command': 'handshake'})
                js = self.recv_json()
                if js['command'] != 'handshake':
                    raise IOError('failed handshake')
                self.closed = False
                while not self.closed:
                    js = self.recv_json()
                    if js == None:
                        self.closed = True
                        break
                    if js['command'] == 'close':
                        self.closed = True
                        break
                    self.handle_js(js)
                    #parse json
                    #handle command
                debug('closed connection')
            except IOError as e:
                debug(repr(e))
            finally:
                try:
                    self.server_sock.close()
                    self.fd.close()
                    self.sock.close()
                except Exception as e:
                    print repr(e)


class DaemonTaker(takermodule.Taker):

    def __init__(self, msgchan):
        takermodule.Taker.__init__(self, msgchan)

    def on_welcome(self):
        takermodule.Taker.on_welcome(self)
        #DaemonCommunicationThread(self).start()

    def on_order_seen(self, counterparty, oid, ordertype, minsize, maxsize,
                      txfee, cjfee):
        takermodule.Taker.on_order_seen(self, counterparty, oid, ordertype,
                                        minsize, maxsize, txfee, cjfee)

    def on_order_cancel(self, counterparty, oid):
        takermodule.Taker.on_order_cancel(self, counterparty, oid)

    def orderbook_updated(self):
        pass


def main():
    load_program_config()

    common.nickname = random_nick()
    debug('starting daemon')

    irc = IRCMessageChannel(common.nickname)
    taker = DaemonTaker(irc)

    serv = ServerThread(taker)
    common.bc_interface = DaemonInterface(serv)
    serv.run()
    try:
        debug('starting irc')
        #irc.run()
    except:
        debug('CRASHING, DUMPING EVERYTHING')
        debug_dump_object(taker)
        import traceback
        debug(traceback.format_exc())


if __name__ == "__main__":
    main()
    print('done')
