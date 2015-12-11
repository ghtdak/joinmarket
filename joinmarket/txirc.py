# Copyright (c) Twisted Matrix Laboratories.
# See LICENSE for details.

import base64
import random
import time

from joinmarket.configure import jm_single, get_config_irc_channel
from joinmarket.enc_wrapper import encrypt_encode, decode_decrypt
from joinmarket.support import get_log, chunks

from twisted.internet import reactor, protocol
from twisted.internet.endpoints import TCP4ClientEndpoint
from twisted.internet.ssl import ClientContextFactory
from twisted.words.protocols import irc
from twisted.words.protocols.irc import stripFormatting
from txsocksx.client import SOCKS5ClientEndpoint
from txsocksx.tls import TLSWrapClientEndpoint

log = get_log()
log.debug('Twisted Logging Starts in txirc')

class txIRC_Client(irc.IRCClient, object):
    def __init__(self, irc_market, nickname, password, hostname):
        self.irc_market = irc_market

        #register with the irc_market
        self.irc_market.set_tx_irc_client(self)

        self.nickname = nickname
        self.password = password
        self.hostname = hostname

        # todo: build pong timeout watchdot
        self.heartbeatinterval = 120
        self.heartbeattimeout = 30

    def set_irc_market(self, mkt):
        self.irc_market = mkt

    # ---------------------------------------------
    # callbacks from superclass
    # ---------------------------------------------

    def lineReceived(self, line):
        #std_log.debug('lineReceived', line)
        return irc.IRCClient.lineReceived(self, line)

    def rawDataReceived(self, data):
        #std_log.debug('rawDataReceived', data)
        return irc.IRCClient.rawDataReceived(self, data)

    def dccSend(self, user, _file):
        return irc.IRCClient.dccSend(self, user, _file)

    def connectionMade(self):
        log.debug('connectionMade: ')
        reactor.callLater(0.0, self.irc_market.connectionMade)
        return irc.IRCClient.connectionMade(self)

    def connectionLost(self, reason=protocol.connectionDone):
        log.debug('connectionLost: {}'.format(reason))
        reactor.callLater(0.0, self.irc_market.connectionLost)
        return irc.IRCClient.connectionLost(self, reason)

    def signedOn(self):
        log.debug('signedOn:')
        self.join(self.factory.channel)

    def joined(self, channel):
        log.debug('joined: {}'.format(channel))
        reactor.callLater(0.0, self.irc_market.joined, channel)

    def privmsg(self, userIn, channel, msg):
        log.debug('privmsg: {} {} {:d} {}'.format(userIn, channel,
                                                  len(msg), msg))

        reactor.callLater(0.0,
                          self.irc_market.handle_privmsg,
                          userIn, channel, msg)

        # user = userIn.split('!', 1)[0]
        #
        # if channel == self.nickname:
        #     msg = "tradingBot -- testing"
        #     self.msg(user, msg)
        #     return
        #
        # if msg.startswith(self.nickname + ":"):
        #     msg = '{}: tradingBot -- testing'.format(user)
        #     self.msg(channel, msg)
        #     std_log.debug('- sent: {} {}'.format((self.nickname, msg)))

    def action(self, user, channel, msg):
        log.debug('action: {} {] {}'.format(user, channel, msg))

    def alterCollidedNick(self, nickname):
        """
        Generate an altered version of a nickname that caused a collision in an
        effort to create an unused related name for subsequent registration.
        :param nickname:
        """
        return nickname + '^'

    def modeChanged(self, user, channel, _set, modes, args):
        log.debug('modeChanged: {} {} {} {} {}'.format(
                user, channel, _set, modes, args))

    def pong(self, user, secs):
        log.debug('pong: {:d}'.format(secs))

    def userJoined(self, user, channel):
        log.debug('user joined: {} {}'.format(user, channel))
        reactor.callLater(0.0, self.irc_market.userJoined, user, channel)

    def userKicked(self, kickee, channel, kicker, message):
        log.debug('kicked: {} {} by {} {}'.format(
            kickee, channel, kicker, message))

    def userLeft(self, user, channel):
        log.debug('left: {} {}'.format(user, channel))
        reactor.callLater(0.0, self.irc_market.userLeft, user, channel)

    def userRenamed(self, oldname, newname):
        log.debug('rename: {} {}'.format(oldname, newname))
        reactor.callLater(0.0, self.irc_market.userRenamed, oldname, newname)

    def userQuit(self, user, quitMessage):
        log.debug(('quit: {} {}'.format(user, quitMessage)))

    def topicUpdated(self, user, channel, newTopic):
        log.debug('topic: {}, {}, {}'.format(user, channel, newTopic))
        reactor.callLater(0.0, self.irc_market.topicUpdated, channel, newTopic)

    def receivedMOTD(self, motd):
        log.debug('motd: {}'.format(motd))

    def created(self, when):
        log.debug('created: {}'.format(when))

    def yourHost(self, info):
        log.debug('yourhost: {}'.format(info))

    def myInfo(self, servername, version, umodes, cmodes):
        log.debug('myInfo: {} {} {} {}'.format(
            servername, version, umodes, cmodes))

    def luserChannels(self, channels):
        log.debug('luserChannels: {}'.format(channels))

    def bounce(self, info):
        log.debug('bounce: {}'.format(info))

    def left(self, channel):
        log.debug('left: {}'.format(channel))

    def noticed(self, user, channel, message):
        log.debug('notice: {} {} {}'.format(user, channel, message))

"""
17:40 <marketeer> !relorder 4 2220656169 2494728195 1000 0.0000649!relorder 5
2494728196 2823231150 1000 0.000069!absorder 0 2731 38458808 1000 2654!relorder
1 38458809 788535195 1000 0.000039!relorder 2 788535196 1675859185 1000
0.0000449!relorder 3 1675859186 2220656168 1000 0.000049 ~
"""

MAX_PRIVMSG_LEN = 400
COMMAND_PREFIX = '!'
PING_INTERVAL = 180
PING_TIMEOUT = 30
encrypted_commands = ["auth", "ioauth", "tx", "sig"]
plaintext_commands = ["fill", "error", "pubkey", "orderbook", "relorder",
                      "absorder", "push"]

class CJPeerError(StandardError):
    pass


def random_nick(nick_len=9):
    vowels = "aeiou"
    consonants = ''.join([chr(
        c) for c in range(
            ord('a'), ord('z') + 1) if vowels.find(chr(c)) == -1])
    assert nick_len % 2 == 1
    N = (nick_len - 1) / 2
    rnd_consonants = [consonants[random.randrange(len(consonants))]
                      for _ in range(N + 1)]
    rnd_vowels = [vowels[random.randrange(len(vowels))]
                  for _ in range(N)] + ['']
    ircnick = ''.join([i for sl in zip(rnd_consonants, rnd_vowels) for i in sl])
    ircnick = ircnick.capitalize()
    # not using debug because it might not know the logfile name at this point
    print('Generated random nickname: ' + ircnick)
    return ircnick
    # Other ideas for random nickname generation:
    # - weight randomness by frequency of letter appearance
    # - u always follows q
    # - generate different length nicks
    # - append two or more of these words together
    # - randomly combine phonetic sounds instead consonants, which may be two consecutive consonants
    #  - e.g. th, dj, g, p, gr, ch, sh, kr,
    # - neutral network that generates nicks


def get_irc_text(line):
    return line[line[1:].find(':') + 2:]


def get_irc_nick(source):
    return source[0:source.find('!')]

# -------------------------------------------------------------
#    IRC_Market
# -------------------------------------------------------------

class CommSuper(object):
    """
    There were a bunch of methods defined on message_channel... so...
    """

    def __init__(self):
        self.coinjoinerpeer = None

    def set_coinjoiner_peer(self, cj):
        self.coinjoinerpeer = cj

    def run(self):
        pass

    def shutdown(self):
        pass

    def send_error(self, nick, errormsg):
        pass

    def request_orderbook(self):
        pass

    def fill_orders(self, nickoid_dict, cj_amount, taker_pubkey):
        pass

    def send_auth(self, nick, pubkey, sig):
        pass

    def send_tx(self, nick_list, txhex):
        pass

    def push_tx(self, nick, txhex):
        pass

    # maker commands
    def announce_orders(self, orderlist, nick=None):
        pass  # nick=None means announce publicly

    def cancel_orders(self, oid_list):
        pass

    def send_pubkey(self, nick, pubkey):
        pass

    def send_ioauth(self, nick, utxo_list, cj_pubkey, change_addr, sig):
        pass

    def send_sigs(self, nick, sig_list):
        pass


class IRC_Market(CommSuper):
    def __init__(
            self, channel, given_nick, username='username',
            realname='realname', password=None):
        super(IRC_Market, self).__init__()

        self.given_nick = given_nick
        self.nick = given_nick
        self.userrealname = (username, realname)
        if password and len(password) == 0:
            password = None
        self.given_password = password

        self.channel = channel
        # self.channel = get_config_irc_channel()

        # todo: rename this.  too confusing
        self.tx_irc_client = None
        self.from_to = None
        self.built_privmsg = []
        self.waiting = []
        # todo: from irc.py
        self.waiting = {}
        self.built_privmsg = {}
        self.give_up = False
        self.ping_reply = True

        # todo: outta here!!!
        self.give_up = True

        # todo: how to end??? kicked?  timeout? etc...

    def set_tx_irc_client(self, tx_irc_client):
        self.tx_irc_client = tx_irc_client

    def run(self):
        """
        Run defined here for consistency
        :return:
        """
        log.debug('Inside IRC_Market.run()')
        def reactor_running():
            log.debug('***** RUNNING!!!')

        reactor.callWhenRunning(reactor_running)
        reactor.run()

    def shutdown(self):
        reactor.stop()

    def send(self, send_to, msg):
        log.debug('send: {} {:d}: {}'.format(send_to, len(msg), msg))
        omsg = 'PRIVMSG %s :' % (send_to,) + msg
        self.tx_irc_client.sendLine(omsg.encode('ascii'))

    def send_error(self, nick, errormsg):
        log.debug('error<%s> : %s' % (nick, errormsg))
        self.__privmsg(nick, 'error', errormsg)
        raise CJPeerError()

    # -----------------------------------
    # connection callbacks
    # -----------------------------------

    def joined(self, channel):
        # todo: mode changes needed?
        try:
            self.coinjoinerpeer.on_welcome()
        except Exception as e:
            log.exception(e)
            reactor.stop()

    def userJoined(self, user, channel):
        pass

    def connectionMade(self, *args, **kwargs):
        try:
            self.coinjoinerpeer.on_connect(*args, **kwargs)
        except Exception as e:
            log.exception(e)
            reactor.stop()

    def connectionLost(self, *args, **kwargs):
        try:
            self.coinjoinerpeer.on_disconnect(*args, **kwargs)
        except Exception as e:
            log.exception(e)
            reactor.stop()

    def userLeft(self, *args, **kwargs):
        try:
            self.coinjoinerpeer.on_nick_leave(*args, **kwargs)
        except Exception as e:
            log.exception(e)
            reactor.stop()

    def userRenamed(self, *args, **kwargs):
        try:
            self.coinjoinerpeer.on_nick_change(*args, **kwargs)
        except Exception as e:
            log.exception(e)
            reactor.stop()

    def topicUpdated(self, *args, **kwargs):
        try:
            self.coinjoinerpeer.on_set_topic(*args, **kwargs)
        except Exception as e:
            log.exception(e)
            reactor.stop()

    # OrderbookWatch callback
    def request_orderbook(self):
        self.__pubmsg(COMMAND_PREFIX + 'orderbook')

    # -----------------------------------
    # Taker callbacks
    # -----------------------------------

    def fill_orders(self, nickoid_dict, cj_amount, taker_pubkey):
        for c, oid in nickoid_dict.iteritems():
            msg = str(oid) + ' ' + str(cj_amount) + ' ' + taker_pubkey
            self.__privmsg(c, 'fill', msg)

    def send_auth(self, nick, pubkey, sig):
        message = pubkey + ' ' + sig
        self.__privmsg(nick, 'auth', message)

    def send_tx(self, nick_list, txhex):
        txb64 = base64.b64encode(txhex.decode('hex'))
        for nick in nick_list:
            self.__privmsg(nick, 'tx', txb64)
            # HACK! really there should be rate limiting, see issue#31
            time.sleep(1)

    def push_tx(self, nick, txhex):
        txb64 = base64.b64encode(txhex.decode('hex'))
        self.__privmsg(nick, 'push', txb64)

    # -----------------------------------
    # Maker callbacks
    # -----------------------------------

    def announce_orders(self, orderlist, nick=None):
        # nick=None means announce publicly
        order_keys = ['oid', 'minsize', 'maxsize', 'txfee', 'cjfee']
        # header = 'PRIVMSG ' + (nick if nick else self.channel) + ' :'
        send_to = nick if nick else self.channel
        header = '' # todo: HACK!! fix this
        orderlines = []
        for i, order in enumerate(orderlist):
            orderparams = COMMAND_PREFIX + order['ordertype'] + \
                          ' ' + ' '.join([str(order[k]) for k in order_keys])
            orderlines.append(orderparams)
            line = header + ''.join(orderlines) + ' ~'
            if len(line) > MAX_PRIVMSG_LEN or i == len(orderlist) - 1:
                if i < len(orderlist) - 1:
                    line = header + ''.join(orderlines[:-1]) + ' ~'
                self.send(send_to, line)
                orderlines = [orderlines[-1]]

    def cancel_orders(self, oid_list):
        clines = [COMMAND_PREFIX + 'cancel ' + str(oid) for oid in oid_list]
        self.__pubmsg(''.join(clines))

    def send_pubkey(self, nick, pubkey):
        self.__privmsg(nick, 'pubkey', pubkey)

    def send_ioauth(self, nick, utxo_list, cj_pubkey, change_addr, sig):
        authmsg = (str(','.join(utxo_list)) + ' ' + cj_pubkey + ' ' +
                   change_addr + ' ' + sig)
        self.__privmsg(nick, 'ioauth', authmsg)

    def send_sigs(self, nick, sig_list):
        # TODO make it send the sigs on one line if there's space
        for s in sig_list:
            self.__privmsg(nick, 'sig', s)
            time.sleep(
                    0.5)  # HACK! really there should be rate limiting, see issue#31

    def __pubmsg(self, message):
        log.debug('>>pubmsg ' + message)
        # self.send_raw("PRIVMSG " + self.channel + " :" + message)
        self.send(self.channel, message)

    def __privmsg(self, nick, cmd, message):
        log.debug(
            '>>privmsg ' + 'nick=' + nick + ' cmd=' + cmd + ' msg=' + message)
        # should we encrypt?
        box, encrypt = self.__get_encryption_box(cmd, nick)
        # encrypt before chunking
        if encrypt:
            if not box:
                log.debug('error, dont have encryption box object for ' + nick +
                          ', dropping message')
                return
            message = encrypt_encode(message, box)

        header = "PRIVMSG " + nick + " :"
        max_chunk_len = MAX_PRIVMSG_LEN - len(header) - len(cmd) - 4
        # 1 for command prefix 1 for space 2 for trailer
        if len(message) > max_chunk_len:
            message_chunks = chunks(message, max_chunk_len)
        else:
            message_chunks = [message]
        for m in message_chunks:
            trailer = ' ~' if m == message_chunks[-1] else ' ;'
            if m == message_chunks[0]:
                m = COMMAND_PREFIX + cmd + ' ' + m
            # self.send_raw(header + m + trailer)
            self.send(nick, m + trailer)

    def check_for_orders(self, nick, _chunks):
        if _chunks[0] in jm_single().ordername_list:
            try:
                counterparty = nick
                oid = _chunks[1]
                ordertype = _chunks[0]
                minsize = _chunks[2]
                maxsize = _chunks[3]
                txfee = _chunks[4]
                cjfee = _chunks[5]
                self.coinjoinerpeer.on_order_seen(
                        counterparty, oid, ordertype, minsize,
                        maxsize, txfee, cjfee)
            except IndexError as e:
                log.exception(e)
                log.debug('index error parsing chunks')
                # TODO what now? just ignore iirc
            finally:
                return True
        return False

    def __on_privmsg(self, nick, message):
        """private message received"""
        if message[0] != COMMAND_PREFIX:
            return
        for command in message[1:].split(COMMAND_PREFIX):
            _chunks = command.split(" ")

            # todo: getattr magic perhaps though doubtful
            try:
                # orderbook watch commands
                if self.check_for_orders(nick, _chunks):
                    pass

                # taker commands
                elif _chunks[0] == 'pubkey':
                    maker_pk = _chunks[1]
                    self.coinjoinerpeer.on_pubkey(nick, maker_pk)
                elif _chunks[0] == 'ioauth':
                    utxo_list = _chunks[1].split(',')
                    cj_pub = _chunks[2]
                    change_addr = _chunks[3]
                    btc_sig = _chunks[4]
                    self.coinjoinerpeer.on_ioauth(
                            nick, utxo_list, cj_pub, change_addr, btc_sig)
                elif _chunks[0] == 'sig':
                    sig = _chunks[1]
                    self.coinjoinerpeer.on_sig(nick, sig)

                # maker commands
                if _chunks[0] == 'fill':
                    try:
                        oid = int(_chunks[1])
                        amount = int(_chunks[2])
                        taker_pk = _chunks[3]
                        # todo: moved... correct?
                        self.coinjoinerpeer.on_order_fill(
                                nick, oid, amount, taker_pk)
                    except (ValueError, IndexError) as e:
                        self.send_error(nick, str(e))

                elif _chunks[0] == 'auth':
                    try:
                        i_utxo_pubkey = _chunks[1]
                        btc_sig = _chunks[2]
                        # todo: shouldn't this be inside try?
                        self.coinjoinerpeer.on_seen_auth(
                                nick, i_utxo_pubkey, btc_sig)
                    except (ValueError, IndexError) as e:
                        self.send_error(nick, str(e))

                elif _chunks[0] == 'tx':
                    b64tx = _chunks[1]
                    try:
                        txhex = base64.b64decode(b64tx).encode('hex')
                        # todo: inside try!
                        self.coinjoinerpeer.on_seen_tx(nick, txhex)
                    except TypeError as e:
                        self.send_error(nick, 'bad base64 tx. ' + repr(e))

                elif _chunks[0] == 'push':
                    b64tx = _chunks[1]
                    try:
                        txhex = base64.b64decode(b64tx).encode('hex')
                        self.coinjoinerpeer.on_push_tx(nick, txhex)
                    except TypeError as e:
                        self.send_error(nick, 'bad base64 tx. ' + repr(e))
            except CJPeerError:
                # TODO proper error handling
                log.debug('cj peer error TODO handle')

            # continue ^

    def __on_pubmsg(self, nick, message):
        if message[0] != COMMAND_PREFIX:
            return
        for command in message[1:].split(COMMAND_PREFIX):
            _chunks = command.split(" ")
            # todo: logic seems twisted... but I'm sure its right
            if self.check_for_orders(nick, _chunks):
                pass
            elif _chunks[0] == 'cancel':
                # !cancel [oid]
                try:
                    oid = int(_chunks[1])

                    self.coinjoinerpeer.on_order_cancel(nick, oid)
                except ValueError as e:
                    log.debug("!cancel " + repr(e))
                    return
            elif _chunks[0] == 'orderbook':
                self.coinjoinerpeer.on_orderbook_requested(nick)


    def __get_encryption_box(self, cmd, nick):
        """
        Establish whether the message is to be
        encrypted/decrypted based on the command string.
        If so, retrieve the appropriate crypto_box object
        and return. Sending/receiving flag enables us
        to check which command strings correspond to which
        type of object (maker/taker)."""

        # todo: comment says # old doc, dont trust

        if cmd in plaintext_commands:
            return None, False
        else:
            return self.coinjoinerpeer.get_crypto_box_from_nick(nick), True

    def handle_privmsg(self, sent_from, sent_to, message):
        try:

            nick = get_irc_nick(sent_from)
            # todo: kludge - we need this elsewhere. rearchitect!!

            self.from_to = (nick, sent_to)

            if sent_to == self.nick:
                # todo: this is some ctcp thing handled elsewhere. check
                # if message[0] == '\x01':
                #     endindex = message[1:].find('\x01')
                #     if endindex == -1:
                #         return
                #     ctcp = message[1:endindex + 1]
                #     if ctcp.upper() == 'VERSION':
                #         self.send_raw('PRIVMSG ' + nick +
                #                       ' :\x01VERSION xchat 2.8.8 Ubuntu\x01')
                #         return

                if nick not in self.built_privmsg:
                    if message[0] != COMMAND_PREFIX:
                        log.debug('Expecting Command, got: {}'.format(message))
                        return

                    # new message starting
                    cmd_string = message[1:].split(' ')[0]
                    if cmd_string not in plaintext_commands + encrypted_commands:
                        log.debug('cmd not in cmd_list, line="' + message + '"')
                        return
                    self.built_privmsg[nick] = [cmd_string, message[:-2]]
                else:
                    self.built_privmsg[nick][1] += message[:-2]
                box, encrypt = self.__get_encryption_box(
                        self.built_privmsg[nick][0], nick)

                # todo: this is sensitive command parser stuff I'm guessing
                # todo: change format, use regex etc
                if message[-1] == ';':
                    self.waiting[nick] = True
                elif message[-1] == '~':
                    self.waiting[nick] = False
                    if encrypt:
                        if not box:
                            log.debug('error, dont have encryption box object '
                                      'for {}, dropping message'.format(nick))
                            return
                        # need to decrypt everything after the command string
                        to_decrypt = ''.join(
                                self.built_privmsg[nick][1].split(' ')[1])
                        try:
                            decrypted = decode_decrypt(to_decrypt, box)
                        except ValueError as e:
                            log.debug('valueerror when decrypting, '
                                      'skipping: {}'.format(repr(e)))
                            return
                        parsed = self.built_privmsg[nick][1].split(' ')[0]
                        parsed += ' ' + decrypted
                    else:
                        parsed = self.built_privmsg[nick][1]
                    # wipe the message buffer waiting for the next one
                    # todo: kinda tricky here.  rearchitect!!
                    del self.built_privmsg[nick]

                    log.debug("<<privmsg nick=%s message=%s" % (nick, parsed))
                    self.__on_privmsg(nick, parsed)
                else:
                    # drop the bad nick
                    del self.built_privmsg[nick]
            elif sent_to == self.channel:
                log.debug("<<pubmsg nick=%s message=%s" % (nick, message))
                self.__on_pubmsg(nick, message)
            else:
                log.debug('what is this? privmsg src=%s target=%s message=%s;' %
                          (sent_from, sent_to, message))
        except Exception as e:
            log.exception(e)
            reactor.stop()




"""
Twisted Infrastructure
"""

class LogBotFactory(protocol.ClientFactory):
    def __init__(self, channel, the_cred):
        self.channel = channel
        self.the_cred = the_cred

    def buildProtocol(self, addr):
        p = txIRC_Client(**self.the_cred)
        p.factory = self
        return p

    def clientConnectionLost(self, connector, reason):
        connector.connect()

    def clientConnectionFailed(self, connector, reason):
        print("connection failed:", reason)
        reactor.stop()

# todo: all this in config

# ght_cred = {'nickname': 'anutxhg',
#             'password': '',
#             'hostname': '6dvj6v5imhny3anf.onion'}

ght_cred = {'nickname': 'anutxhg',
            'password': '',
            'hostname': 'localhost'}

def tor_cyber():

    factory = LogBotFactory('#joinmarket-pit', ght_cred)

    ctx = ClientContextFactory()

    torEndpoint = TCP4ClientEndpoint(reactor, '192.168.1.200', 9050)
    ircEndpoint = SOCKS5ClientEndpoint('6dvj6v5imhny3anf.onion',
                                       6697, torEndpoint)
    tlsEndpoint = TLSWrapClientEndpoint(ctx, ircEndpoint)

    return tlsEndpoint.connect(factory)

def ssl_cyber():

    factory = LogBotFactory('#joinmarket-pit',
                            ght_cred)

    ctx = ClientContextFactory()
    # ctx = CertificateOptions(verify=False)

    return reactor.connectSSL("irc.cyberguerrilla.org", 6697, factory, ctx)

def home_nosec():

    factory = LogBotFactory('#anarchy', ght_cred)

    return reactor.connectTCP('192.168.1.200', 6667, factory)

def localhost_nosec():

    factory = LogBotFactory('#anarchy', ght_cred)

    return reactor.connectTCP('localhost', 6667, factory)

def build_irc_communicator(
        given_nick,
        username='username',
        realname='realname',
        password=None):

    # from IRC_blah constructor
    config = jm_single().config
    serverport = (config.get("MESSAGING", "host"),
                       int(config.get("MESSAGING", "port")))
    socks5_host = config.get("MESSAGING", "socks5_host")
    socks5_port = int(config.get("MESSAGING", "socks5_port"))

    # todo: channel set in too many places.  Should be only one
    channel = get_config_irc_channel()

    irc_market = IRC_Market(channel,
                            given_nick,
                            username=username,
                            realname=realname,
                            password=password)

    # todo: hack password
    cr = {'irc_market': irc_market,
          'nickname': given_nick,
          'password': 'nimDid[Quoc6',
          'hostname': 'nowhere.com'}

    factory = LogBotFactory(channel, cr)

    # todo: hack!!!
    serverport = ('192.168.1.200', 6667)

    reactor.connectTCP(serverport[0], serverport[1], factory)

    return irc_market
