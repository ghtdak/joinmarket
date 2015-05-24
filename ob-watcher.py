import BaseHTTPServer, SimpleHTTPServer, threading
from decimal import Decimal
import io, base64, time, sys, os
data_dir = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(data_dir, 'lib'))

import taker
from irc import IRCMessageChannel, random_nick
from common import *
import common

tableheading = '''
<table>
 <tr>
  <th>Type</th>
  <th>Counterparty</th>
  <th>Order ID</th>
  <th>Fee</th>
  <th>Miner Fee Contribution</th>
  <th>Minimum Size</th>
  <th>Maximum Size</th>
 </tr>
'''

shutdownform = '<form action="shutdown" method="post"><input type="submit" value="Shutdown" /></form>'
shutdownpage = '<html><body><center><h1>Successfully Shut down</h1></center></body></html>'

refresh_orderbook_form = '<form action="refreshorderbook" method="post"><input type="submit" value="Check for timed-out counterparties" /></form>'


def calc_depth_data(db, value):
    pass


def calc_order_size_data(db):
    return ordersizes


def create_depth_chart(db, cj_amount):
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        return 'Install matplotlib to see graphs'
    sqlorders = db.execute('SELECT * FROM orderbook;').fetchall()
    orderfees = [calc_cj_fee(o['ordertype'], o['cjfee'], cj_amount) / 1e8
                 for o in sqlorders
                 if cj_amount >= o['minsize'] and cj_amount <= o['maxsize']]

    if len(orderfees) == 0:
        return 'No orders at amount ' + str(cj_amount / 1e8)
    fig = plt.figure()
    if len(orderfees) == 1:
        plt.hist(orderfees,
                 30,
                 rwidth=0.8,
                 range=(orderfees[0] / 2, orderfees[0] * 2))
    else:
        plt.hist(orderfees, 30, rwidth=0.8)
    plt.grid()
    plt.title('CoinJoin Orderbook Depth Chart for amount=' + str(cj_amount /
                                                                 1e8) + 'btc')
    plt.xlabel('CoinJoin Fee / btc')
    plt.ylabel('Frequency')
    return get_graph_html(fig)


def create_size_histogram(db):
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        return 'Install matplotlib to see graphs'
    rows = db.execute('SELECT maxsize FROM orderbook;').fetchall()
    ordersizes = [r['maxsize'] / 1e8 for r in rows]

    fig = plt.figure()
    plt.hist(ordersizes, 30, histtype='bar', rwidth=0.8)
    plt.grid()
    #plt.title('Order size distribution')
    plt.xlabel('Order sizes / btc')
    plt.ylabel('Frequency')
    return get_graph_html(fig)


def get_graph_html(fig):
    imbuf = io.BytesIO()
    fig.savefig(imbuf, format='png')
    b64 = base64.b64encode(imbuf.getvalue())
    return '<img src="data:image/png;base64,' + b64 + '" />'


def do_nothing(arg, order):
    return arg


def ordertype_display(ordertype, order):
    ordertypes = {'absorder': 'Absolute Fee', 'relorder': 'Relative Fee'}
    return ordertypes[ordertype]


def cjfee_display(cjfee, order):
    if order['ordertype'] == 'absorder':
        return satoshi_to_unit(cjfee, order)
    elif order['ordertype'] == 'relorder':
        return str(float(cjfee) * 100) + '%'


def satoshi_to_unit(sat, order):
    return str(Decimal(sat) / Decimal(1e8))


def order_str(s, order):
    return str(s)


class OrderbookPageRequestHeader(SimpleHTTPServer.SimpleHTTPRequestHandler):

    def __init__(self, request, client_address, base_server):
        self.taker = base_server.taker
        self.base_server = base_server
        SimpleHTTPServer.SimpleHTTPRequestHandler.__init__(
            self, request, client_address, base_server)

    def create_orderbook_table(self):
        result = ''
        rows = self.taker.db.execute('SELECT * FROM orderbook;').fetchall()
        for o in rows:
            result += ' <tr>\n'
            order_keys_display = (
                ('ordertype', ordertype_display), ('counterparty', do_nothing),
                ('oid', order_str), ('cjfee', cjfee_display),
                ('txfee', satoshi_to_unit), ('minsize', satoshi_to_unit),
                ('maxsize', satoshi_to_unit))
            for key, displayer in order_keys_display:
                result += '  <td>' + displayer(o[key], o) + '</td>\n'
            result += ' </tr>\n'
        return len(rows), result

    def get_counterparty_count(self):
        counterparties = self.taker.db.execute(
            'SELECT DISTINCT counterparty FROM orderbook;').fetchall()
        return str(len(counterparties))

    def do_GET(self):
        #SimpleHTTPServer.SimpleHTTPRequestHandler.do_GET(self)
        #print 'httpd received ' + self.path + ' request'
        pages = ['/', '/ordersize', '/depth']
        if self.path not in pages:
            return
        fd = open('orderbook.html', 'r')
        orderbook_fmt = fd.read()
        fd.close()
        alert_msg = ''
        if common.joinmarket_alert:
            alert_msg = '<br />JoinMarket Alert Message:<br />' + common.joinmarket_alert
        if self.path == '/':
            ordercount, ordertable = self.create_orderbook_table()
            replacements = {
                'PAGETITLE': 'JoinMarket Browser Interface',
                'MAINHEADING': 'JoinMarket Orderbook',
                'SECONDHEADING':
                (str(ordercount) + ' orders found by ' +
                 self.get_counterparty_count() + ' counterparties' + alert_msg),
                'MAINBODY': refresh_orderbook_form + shutdownform + tableheading
                + ordertable + '</table>\n'
            }
        elif self.path == '/ordersize':
            replacements = {
                'PAGETITLE': 'JoinMarket Browser Interface',
                'MAINHEADING': 'Order Sizes',
                'SECONDHEADING': 'Order Size Histogram' + alert_msg,
                'MAINBODY': create_size_histogram(self.taker.db)
            }
        elif self.path.startswith('/depth'):
            #if self.path[6] == '?':
            #	quantity =
            cj_amounts = [10 ** cja for cja in range(4, 10, 1)]
            mainbody = [create_depth_chart(self.taker.db, cja)
                        for cja in cj_amounts]
            replacements = {
                'PAGETITLE': 'JoinMarket Browser Interface',
                'MAINHEADING': 'Depth Chart',
                'SECONDHEADING': 'Orderbook Depth' + alert_msg,
                'MAINBODY': '<br />'.join(mainbody)
            }
        orderbook_page = orderbook_fmt
        for key, rep in replacements.iteritems():
            orderbook_page = orderbook_page.replace(key, rep)
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.send_header('Content-Length', len(orderbook_page))
        self.end_headers()
        self.wfile.write(orderbook_page)

    def do_POST(self):
        pages = ['/shutdown', '/refreshorderbook']
        if self.path not in pages:
            return
        if self.path == '/shutdown':
            self.taker.msgchan.shutdown()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', len(shutdownpage))
            self.end_headers()
            self.wfile.write(shutdownpage)
            self.base_server.__shutdown_request = True
        elif self.path == '/refreshorderbook':
            self.taker.msgchan.request_orderbook()
            time.sleep(5)
            self.path = '/'
            self.do_GET()


class HTTPDThread(threading.Thread):

    def __init__(self, taker):
        threading.Thread.__init__(self)
        self.daemon = True
        self.taker = taker

    def run(self):
        hostport = ('localhost', 62601)
        httpd = BaseHTTPServer.HTTPServer(hostport, OrderbookPageRequestHeader)
        httpd.taker = self.taker
        print '\nstarted http server, visit http://{0}:{1}/\n'.format(*hostport)
        httpd.serve_forever()


class GUITaker(taker.OrderbookWatch):

    def on_welcome(self):
        taker.OrderbookWatch.on_welcome(self)
        HTTPDThread(self).start()


def main():
    import bitcoin as btc
    import common
    import binascii, os
    common.nickname = random_nick()  #watcher' +binascii.hexlify(os.urandom(4))
    common.load_program_config()

    irc = IRCMessageChannel(common.nickname)
    taker = GUITaker(irc)
    print 'starting irc'
    irc.run()


if __name__ == "__main__":
    main()
    print('done')
