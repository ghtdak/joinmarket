from __future__ import absolute_import

import base64
import io
import json
import sys
import urllib2
from decimal import Decimal
from optparse import OptionParser

import matplotlib
from twisted.internet import reactor
from twisted.logger import Logger
from twisted.web import server, resource

import joinmarket as jm

matplotlib.use('Agg')
import matplotlib.pyplot as plt

log = Logger()

"""
https://stackoverflow.com/questions/2801882/generating-a-png-with-matplotlib-
when-display-is-undefined
"""

shutdownform = ('<form action="shutdown" method="post"><input type="submit"'
                ' value="Shutdown" /></form>')
shutdownpage = ('<html><body><center><h1>Successfully Shut down</h1></center>'
                '</body></html>')

refresh_orderbook_form = ('<form action="refreshorderbook" method="post">'
                          '<input type="submit" value="Check for timed-out '
                          'counterparties" /></form>')
sorted_units = ('BTC', 'mBTC', '&#956;BTC', 'satoshi')
unit_to_power = {'BTC': 8, 'mBTC': 5, '&#956;BTC': 2, 'satoshi': 0}
sorted_rel_units = ('%', '&#8241;', 'ppm')
rel_unit_to_factor = {'%': 100, '&#8241;': 1e4, 'ppm': 1e6}


def calc_depth_data(db, value):
    pass


def create_depth_chart(db, cj_amount, args=None):
    if args is None:
        args = {}
    sqlorders = db.execute('SELECT * FROM orderbook;').fetchall()
    orderfees = sorted(
            [jm.calc_cj_fee(o['ordertype'], o['cjfee'], cj_amount) / 1e8
             for o in sqlorders if o['minsize'] <= cj_amount <= o['maxsize']])

    if len(orderfees) == 0:
        return 'No orders at amount ' + str(cj_amount / 1e8)
    fig = plt.figure()
    scale = args.get("scale")
    if (scale is not None) and (scale[0] == "log"):
        orderfees = [float(fee) for fee in orderfees]
        if orderfees[0] > 0:
            ratio = orderfees[-1] / orderfees[0]
            step = ratio**0.0333  # 1/30
            bins = [orderfees[0] * (step**i) for i in range(30)]
        else:
            ratio = orderfees[-1] / 1e-8  # single satoshi placeholder
            step = ratio**0.0333  # 1/30
            bins = [1e-8 * (step**i) for i in range(30)]
            bins[0] = orderfees[0]  # replace placeholder
        plt.xscale('log')
    else:
        bins = 30
    if len(orderfees) == 1:  # these days we have liquidity, but just in case...
        plt.hist(orderfees, bins, rwidth=0.8, range=(0, orderfees[0] * 2))
    else:
        plt.hist(orderfees, bins, rwidth=0.8)
    plt.grid()
    plt.title('CoinJoin Orderbook Depth Chart for amount=' + str(cj_amount /
                                                                 1e8) + 'btc')
    plt.xlabel('CoinJoin Fee / btc')
    plt.ylabel('Frequency')
    return get_graph_html(fig)


def create_size_histogram(db, args):
    rows = db.execute('SELECT maxsize FROM orderbook;').fetchall()
    ordersizes = sorted([r['maxsize'] / 1e8 for r in rows])

    fig = plt.figure()
    scale = args.get("scale")
    if (scale is not None) and (scale[0] == "log"):
        ratio = ordersizes[-1] / ordersizes[0]
        step = ratio**0.0333  # 1/30
        bins = [ordersizes[0] * (step**i) for i in range(30)]
    else:
        bins = 30
    plt.hist(ordersizes, bins, histtype='bar', rwidth=0.8)
    if bins is not 30:
        fig.axes[0].set_xscale('log')
    plt.grid()
    plt.xlabel('Order sizes / btc')
    plt.ylabel('Frequency')
    return get_graph_html(fig) + ("<br/><a href='?scale=log'>log scale</a>" if
                                  bins == 30 else "<br/><a href='?'>linear</a>")


def get_graph_html(fig):
    imbuf = io.BytesIO()
    fig.savefig(imbuf, format='png')
    b64 = base64.b64encode(imbuf.getvalue())
    return '<img src="data:image/png;base64,' + b64 + '" />'


# callback functions for displaying order data
def do_nothing(arg, order, btc_unit, rel_unit):
    return arg


def ordertype_display(ordertype, order, btc_unit, rel_unit):
    ordertypes = {'absorder': 'Absolute Fee', 'relorder': 'Relative Fee'}
    return ordertypes[ordertype]


def cjfee_display(cjfee, order, btc_unit, rel_unit):
    if order['ordertype'] == 'absorder':
        return satoshi_to_unit(cjfee, order, btc_unit, rel_unit)
    elif order['ordertype'] == 'relorder':
        return str(float(cjfee) * rel_unit_to_factor[rel_unit]) + rel_unit


def satoshi_to_unit(sat, order, btc_unit, rel_unit):
    power = unit_to_power[btc_unit]
    return ("%." + str(power) + "f") % float(Decimal(sat) / Decimal(10**power))


def order_str(s, order, btc_unit, rel_unit):
    return str(s)


def create_orderbook_table(db, btc_unit, rel_unit):
    result = ''
    rows = db.execute('SELECT * FROM orderbook;').fetchall()
    if not rows:
        return 0, result
    order_keys_display = (('ordertype', ordertype_display),
                          ('counterparty', do_nothing), ('oid', order_str),
                          ('cjfee', cjfee_display), ('txfee', satoshi_to_unit),
                          ('minsize', satoshi_to_unit),
                          ('maxsize', satoshi_to_unit))

    # somewhat complex sorting to sort by cjfee but with absorders on top

    ol = ['absorder', 'relorder']
    def orderby_cmp(x, y):
        if x['ordertype'] == y['ordertype']:
            return cmp(Decimal(x['cjfee']), Decimal(y['cjfee']))
        return cmp(ol.index(x['ordertype']), ol.index(y['ordertype']))

    for o in sorted(rows, cmp=orderby_cmp):
        result += ' <tr>\n'
        for key, displayer in order_keys_display:
            result += '  <td>' + displayer(o[key], o, btc_unit,
                                           rel_unit) + '</td>\n'
        result += ' </tr>\n'
    return len(rows), result


def create_table_heading(btc_unit, rel_unit):
    col = '  <th>{1}</th>\n'  # .format(field,label)
    tableheading = '<table class="tftable sortable" border="1">\n <tr>' + ''.join(
        [
            col.format('ordertype', 'Type'), col.format(
                'counterparty', 'Counterparty'), col.format('oid', 'Order ID'),
            col.format('cjfee', 'Fee'), col.format(
                'txfee', 'Miner Fee Contribution / ' + btc_unit), col.format(
                    'minsize', 'Minimum Size / ' + btc_unit), col.format(
                        'maxsize', 'Maximum Size / ' + btc_unit)
        ]) + ' </tr>'
    return tableheading


def create_choose_units_form(selected_btc, selected_rel):
    choose_units_form = (
        '<form method="get" action="">' +
        '<select name="btcunit" onchange="this.form.submit();">' +
        ''.join(('<option>' + u + ' </option>' for u in sorted_units)) +
        '</select><select name="relunit" onchange="this.form.submit();">' +
        ''.join(('<option>' + u + ' </option>' for u in sorted_rel_units)) +
        '</select></form>')
    choose_units_form = choose_units_form.replace(
        '<option>' + selected_btc,
        '<option selected="selected">' + selected_btc)
    choose_units_form = choose_units_form.replace(
        '<option>' + selected_rel,
        '<option selected="selected">' + selected_rel)
    return choose_units_form


class NotifyHttpServer(resource.Resource):

    isLeaf = True

    def __init__(self, btcinterface):
        self.btcinterface = btcinterface
        self.using_port = None

    def render_GET(self, request):
        return ''


class OrderbookPageRequestHeader(resource.Resource):

    isLeaf = True

    def __init__(self, taker):
        self.taker = taker

    def create_orderbook_obj(self):
        rows = self.taker.db.execute('SELECT * FROM orderbook;').fetchall()
        if not rows:
            return []

        result = []
        for row in rows:
            o = dict(row)
            if 'cjfee' in o:
                o['cjfee'] = int(o['cjfee']) if o[
                    'ordertype'] == 'absorder' else float(o['cjfee'])
            result.append(o)
        return result

    def get_counterparty_count(self):
        counterparties = self.taker.db.execute(
            'SELECT DISTINCT counterparty FROM orderbook;').fetchall()
        return str(len(counterparties))

    def render_GET(self, request):
        path = request.uri

        path, query = path.split('?', 1) if '?' in path else (
            path, '')
        args = urllib2.urlparse.parse_qs(query)
        pages = ['/', '/ordersize', '/depth', '/orderbook.json']
        if path not in pages:
            return
        fd = open('orderbook.html', 'r')
        orderbook_fmt = fd.read()
        fd.close()
        alert_msg = ''
        if path == '/':
            btc_unit = args['btcunit'][
                0] if 'btcunit' in args else sorted_units[0]
            rel_unit = args['relunit'][
                0] if 'relunit' in args else sorted_rel_units[0]
            if btc_unit not in sorted_units:
                btc_unit = sorted_units[0]
            if rel_unit not in sorted_rel_units:
                rel_unit = sorted_rel_units[0]
            ordercount, ordertable = create_orderbook_table(self.taker.db,
                                                            btc_unit, rel_unit)
            choose_units_form = create_choose_units_form(btc_unit, rel_unit)
            table_heading = create_table_heading(btc_unit, rel_unit)
            replacements = {
                'PAGETITLE': 'JoinMarket Browser Interface',
                'MAINHEADING': 'JoinMarket Orderbook',
                'SECONDHEADING':
                (str(ordercount) + ' orders found by ' +
                 self.get_counterparty_count() + ' counterparties' + alert_msg),
                'MAINBODY': (
                    shutdownform + refresh_orderbook_form + choose_units_form +
                    table_heading + ordertable + '</table>\n')
            }
        elif path == '/ordersize':
            replacements = {
                'PAGETITLE': 'JoinMarket Browser Interface',
                'MAINHEADING': 'Order Sizes',
                'SECONDHEADING': 'Order Size Histogram' + alert_msg,
                'MAINBODY': create_size_histogram(self.taker.db, args)
            }
        elif path.startswith('/depth'):
            # if path[6] == '?':
            #	quantity =
            cj_amounts = [10**cja for cja in range(4, 12, 1)]
            mainbody = [create_depth_chart(self.taker.db, cja, args) \
                        for cja in cj_amounts] + \
                       ["<br/><a href='?'>linear</a>" if args.get("scale") \
                            else "<br/><a href='?scale=log'>log scale</a>"]
            replacements = {
                'PAGETITLE': 'JoinMarket Browser Interface',
                'MAINHEADING': 'Depth Chart',
                'SECONDHEADING': 'Orderbook Depth' + alert_msg,
                'MAINBODY': '<br />'.join(mainbody)
            }
        elif path == '/orderbook.json':
            replacements = {}
            orderbook_fmt = json.dumps(self.create_orderbook_obj())
        orderbook_page = orderbook_fmt
        for key, rep in replacements.iteritems():
            orderbook_page = orderbook_page.replace(key, rep)
        # self.send_response(200)
        # if path.endswith('.json'):
        #     self.send_header('Content-Type', 'application/json')
        # else:
        #     self.send_header('Content-Type', 'text/html')
        # self.send_header('Content-Length', len(orderbook_page))
        # self.end_headers()
        return orderbook_page

    def render_POST(self, request):
        log.debug('post', request=request)

        # path = request.uri
        # pages = ['/shutdown', '/refreshorderbook']
        # if path not in pages:
        #     return
        # if path == '/shutdown':
        #     self.taker.msgchan.shutdown()
        #     self.send_response(200)
        #     self.send_header('Content-Type', 'text/html')
        #     self.send_header('Content-Length', len(shutdownpage))
        #     self.end_headers()
        #     self.wfile.write(shutdownpage)
        #     self.base_server.__shutdown_request = True
        # todo: no idea.  come back
        # elif path == '/refreshorderbook':
        #     self.taker.msgchan.request_orderbook()
        #     path = '/'
        #     self.do_GET()


class JmSrv(server.Site):
    def __init__(self, srv):
        server.Site.__init__(self, srv)

    def log(self, _):
        pass            # SHUT UP!!!


class GUITaker(jm.OrderbookWatch):
    def __init__(self, block_instance, hostport):
        self.hostport = hostport
        super(GUITaker, self).__init__(block_instance)
        srv = OrderbookPageRequestHeader(self)
        reactor.listenTCP(self.hostport[1], JmSrv(srv))


def build_objects(argv=None):
    if argv is None:
        argv=sys.argv

    parser = OptionParser(
        usage='usage: %prog [options]',
        description='Runs a webservice which shows the orderbook.')
    parser.add_option(
            '-H',
            '--host',
            action='store',
            type='string',
            dest='host',
            default='localhost',
            help='hostname or IP to bind to, default=localhost')
    parser.add_option(
            '-p',
            '--port',
            action='store',
            type='int',
            dest='port',
            help='port to listen on, default=8080',
            default=8080)
    (options, args) = parser.parse_args(argv[1:])

    nickname = jm.random_nick()
    block_instance = jm.BlockInstance(nickname)

    GUITaker(block_instance, (options.host, options.port))

    return block_instance
