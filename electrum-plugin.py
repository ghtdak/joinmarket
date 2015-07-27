from PyQt4 import QtCore
from PyQt4.QtGui import *

from electrum.plugins import BasePlugin, hook
from electrum.i18n import _
from electrum.wallet import Abstract_Wallet

from electrum_gui.qt.util import EnterButton

import time, traceback, threading, socket, json


def find_tx_details(tx, wallet):
    cj_addr = None
    change_addr = None
    cj_amount = 0
    for otype, addr, value in tx.outputs:
        if otype != 'address':
            print('whoops, cant send to places other than addresses')
            return None
        if wallet.is_change(addr):
            change_addr = addr
        else:
            cj_addr = addr
            cj_amount = value
    return cj_addr, change_addr, cj_amount


#best to replace this in init_qt() and then put it back when plugin disabled
class JoinMarketWallet(Abstract_Wallet):
    underlying_wallet = None

    def __init__(self, plugin, underlying_wallet):
        JoinMarketWallet.underlying_wallet = underlying_wallet
        JoinMarketWallet.plugin = plugin
        Abstract_Wallet.__init__(self, underlying_wallet.storage)

    def __getattribute__(self, name):
        if name not in JoinMarketWallet.__dict__:
            return JoinMarketWallet.underlying_wallet.__getattribute__(name)
        else:
            return object.__getattribute__(self, name)

    def calc_joinmarket_fee(self, tx):
        ret = find_tx_details(tx, self)
        if not ret:
            return 0
        cj_addr, change_addr, cj_amount = ret
        js = self.plugin.send_json_wait({'command': 'reqtxfee',
                                         'cjamount': cj_amount,
                                         'makercount': 2})
        if js['command'] == 'cjfee':
            return js['cjfee']
        raise RuntimeError('bad cmd')

    def estimated_fee(self, tx):
        fee = self.underlying_wallet.estimated_fee(tx)
        cjfee = self.calc_joinmarket_fee(tx)
        print 'estimated fee ' + str(fee) + ' cjfee = ' + str(cjfee)
        return fee + cjfee

    def get_tx_fee(self, tx):
        fee = self.underlying_wallet.get_tx_fee(tx)
        cjfee = self.calc_joinmarket_fee(tx)
        print 'get_tx_fee ' + str(fee) + ' cjfee = ' + str(cjfee)
        return fee + cjfee


class DaemonCommuncationThread(threading.Thread):

    def __init__(self):
        threading.Thread.__init__(self)
        self.daemon = True
        self.reply_notify_lock = threading.Condition()
        self.last_recv_js = None

    def connect(self):
        try:
            print 'connecting to daemon'
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.fd = self.sock.makefile()
            self.sock.connect(('localhost', 62600))
            return True
        except IOError as e:
            print 'connecting ' + repr(e)
            try:
                self.sock.close()
                self.fd.close()
            except Exception:
                pass
            return False

    def close(self):
        self.sock.close()
        self.closed = True

    def send_json(self, js):
        jsonstr = json.dumps(js)
        print '>> ' + str(js)
        self.sock.sendall(jsonstr + '\r\n')

    def recv_json(self):
        line = self.fd.readline()
        if line == None:
            return None
        if len(line) == 0:
            return None
        js = json.loads(line)
        print '<< ' + str(js)
        return js

    def send_json_wait(self, js):
        '''used for multithread communcation'''
        self.reply_notify_lock.acquire()
        self.send_json(js)
        self.reply_notify_lock.wait()
        self.reply_notify_lock.release()
        return self.last_recv_js

    def handle_js(self, js):
        if js['command'] == '':
            pass
        self.last_recv_js = js
        self.reply_notify_lock.acquire()
        self.reply_notify_lock.notify()
        self.reply_notify_lock.release()

    def run(self):
        print 'started daemon communication thread'
        try:
            self.fd = self.sock.makefile()
            self.send_json({'command': 'handshake'})
            js = self.recv_json()
            if js['command'] != 'handshake':
                raise IOError('failed handshake')
            self.closed = False
            while not self.closed:
                js = self.recv_json()
                print ' >> ' + str(js)
                if js == None:
                    self.closed = True
                    break
                if js['command'] == 'close':
                    self.closed = True
                    break
                handle_js(js)
            print 'closed connection'
        except IOError as e:
            print repr(e)
        finally:
            try:
                self.server_sock.close()
                self.fd.close()
                self.sock.close()
            except Exception as e:
                print repr(e)


class Plugin(BasePlugin):

    gui = None
    daemon_comm = None

    def fullname(self):
        return 'JoinMarket Send'

    def description(self):
        return _("Send Bitcoins with CoinJoin using JoinMarket.\n\nRequires JoinMarket daemon.")

    def requires_settings(self):
        return True

    def settings_widget(self, window):
        return EnterButton(_('Settings'), self.settings_dialog)

    def settings_dialog(self):
        print 'pressed settings'
        #self.daemon_comm.send_json({'command': 'echo', 'stuff': 'here'})
        d = QDialog()
        d.setWindowTitle("Settings")

        #blockhash = '00000000000000000bbbb62880ccc9f97520f94c2143279e7047b6b6752a4dc9'
        #blockhash = 10
        #raw = self.gui.network.synchronous_get([ ('blockchain.block.get_header', [blockhash]) ])
        #print raw

        #blockhash = 10
        #raw = self.gui.network.synchronous_get([ ('blockchain.block.get_chunk', [blockhash]) ])
        #jsonraw = str(json.loads(raw))
        #print jsonraw.keys()

        if d.exec_():
            return True
        else:
            return False

    def start_daemon_connection(self):
        if self.daemon_comm:
            return
        self.daemon_comm = DaemonCommuncationThread()
        if not self.daemon_comm.connect():
            self.daemon_comm = None
            print 'error connecting to daemon'
        self.daemon_comm.start()

    def stop_daemon_connection(self):
        if not self.daemon_comm:
            return
        print 'ending daemon'
        self.daemon_comm.close()
        self.daemon_comm = None

    @hook
    def set_enabled(self, enabled):
        BasePlugin.set_enabled(self, enabled)
        print 'set enabled = ' + str(enabled)
        '''
        if enabled:
            self.start_daemon_connection()
        else:
            self.stop_daemon_connection()
        if not enabled and isinstance(self.gui.main_window.wallet, JoinMarketWallet):
            print 'returning wallet to normal'
            self.gui.main_window.wallet = self.gui.main_window.wallet.underlying_wallet
         '''

    @hook
    def load_wallet(self, wallet):
        print 'load wallet'
        #if self.gui and self.gui.main_window.wallet and not isinstance(self.gui.main_window.wallet, JoinMarketWallet):
        #    print 'creating JM wallet'
        #    self.gui.main_window.wallet = JoinMarketWallet(self.gui.main_window.wallet, self.gui.main_window.wallet)

        #in ./gui/qt/main_window.py there is broadcast_transaction()

    @hook
    def init_qt(self, gui):
        print 'init gui'
        self.gui = gui
        #if self.gui.main_window.wallet and not isinstance(self.gui.main_window.wallet, JoinMarketWallet):
        #    print 'creating JM wallet'
        #    self.gui.main_window.wallet = JoinMarketWallet(self.gui.main_window.wallet, self.gui.main_window.wallet)

    @hook
    def make_unsigned_transaction(self, tx):
        '''called when the user presses send or edits the send dialog'''
        print 'make unsigned tx'
        #print str(tx)

        #see https://electrum.orain.org/wiki/Stratum_protocol_specification
        '''
        #get the address and value like this
        tx_hash = '6719c7e225972bb2f935d0f923748a69868fab094fc302bbd64b7d10d89a16b0'
        print 'getting txhash = ' + tx_hash
        raw = self.gui.network.synchronous_get([ ('blockchain.transaction.get',[tx_hash]) ])
        print str(raw)

        #check its unspent and confirmed
        #addr = '1JfbZRwdDHKZmuiZgYArJZhcuuzuw2HuMu'
        addr = '1EtnpHTBthhXbjLn2TRfJMXu982fjPKcwM'
        print 'getting addr listunspent = ' + addr
        data = self.gui.network.synchronous_get([ ('blockchain.address.listunspent', [addr]) ])
        print str(data)
        #taker needs the following information
        # info on tx being spent, unconfirmed or genuinly just in the utxo set
        #  the value and scriptpubkey (/ address) of such a utxo
        # pushtx, should be easy
        '''
        if len(tx.outputs) > 2:
            self.print_error(
                'cant make coinjoins with more than one output address yet')
            return

        cj_addr = None
        change_addr = None
        cj_amount = 0
        for otype, addr, value in tx.outputs:
            if otype != 'address':
                self.print_error(
                    'whoops, cant send to places other than addresses')
                return
            if self.wallet.is_change(addr):
                change_addr = addr
            else:
                cj_addr = addr
                cj_amount = value
        print 'cj=' + cj_addr + ' change=' + change_addr

    @hook
    def sign_transaction(self, tx, password):
        '''called when the user types in password after send is clicked'''
        print 'sign tx ' + str(type(tx))
        print str(tx)
        #tx is of instance Transaction
        # need to somehow find out which is the change address
        address = ''
        if len(address) > 0:
            priv = self.wallet.get_private_key(address, password)
            print 'priv = ' + str(priv)
        #the tx needs enough inputs to pay the coinjoin fee, check it has enough
        # if not, repeat the process in make_unsigned_transaction() to get more
        # trustedcoin.py adds a fee to a tx, look how they do it
        #  they override Wallet class and override get_tx_fee()
        # for us it might be worth overriding the function pointer
        #if/when the user clicks boardcast, then send it to electrum or somehow halt the broadcast
        # and send it to a maker
        # need to add a hook to electrum that has the ability to halt a broadcast
        # ThomasV says he would accept such a hook
        # probably best is that it is able to raise an exception

    @hook
    def transaction_dialog(self, d):
        '''called when the transaction is displayed to the user right before broadcast'''
        print 'transaction dialog ' + str(type(d))

    @hook
    def create_send_tab(self, grid):
        print 'create send tab, put coinjoin fee amount here that updates as the user types in amounts'
        self.start_daemon_connection()
        #maybe get access to the address/amount field, to be able to tell between output and change
        #better way would be to hook mktx() so you can see outputs
