# Copyright (c) Twisted Matrix Laboratories.
# See LICENSE for details.

import base64
import random
import traceback

import collections

from twisted.internet import reactor, protocol
from twisted.internet.endpoints import TCP4ClientEndpoint
from twisted.internet.ssl import ClientContextFactory
from twisted.logger import Logger
from twisted.words.protocols import irc
from txsocksx.client import SOCKS5ClientEndpoint
from txsocksx.tls import TLSWrapClientEndpoint

from .configure import get_config_irc_channel
from .enc_wrapper import encrypt_encode, decode_decrypt
from .jsonrpc import JsonRpcError
from .support import chunks

log = Logger()
log.debug('Twisted Logging Starts in txirc')


class txIRC_Client(irc.IRCClient, object):
    """
    lineRate is a class variable in the superclass used to limit
    messages / second.  heartbeat is what you'd think
    """
    lineRate = 1
    heartbeatinterval = 60

    def __init__(self, block_instance, nickname, password, hostname):
        self.block_instance = block_instance
        self.nickname = nickname
        self.password = password
        self.hostname = hostname

        self.block_instance.set_tx_irc_client(self)

        ns = self.__module__ + '@' + self.nickname
        self.log = Logger(namespace=ns)

        # stochastic network delay support
        self._receiveQ = collections.deque()

        # todo: build pong timeout watchdot

    def __getattr__(self, name):
        if name == 'irc_market':
            return self.block_instance.irc_market
        else:
            raise AttributeError


    # --------------------------------------------------
    # stochastic line delay simulation
    # --------------------------------------------------

    def lineReceived(self, line):
        self._receiveQ.append(line)
        delay = 0.05 + 0.2 * random.random()
        reactor.callLater(delay, self._jm_reallyReceive)

    def _jm_reallyReceive(self):
        return irc.IRCClient.lineReceived(self, self._receiveQ.popleft())

    def dccSend(self, user, _file):
        return irc.IRCClient.dccSend(self, user, _file)

    def rawDataReceived(self, data):
        log.error('rawDataReceived shouldn\'t be called')

    def connectionMade(self):
        self.log.debug('connectionMade: ')
        self._receiveQ = collections.deque()
        reactor.callLater(0.0, self.irc_market.connectionMade)
        return irc.IRCClient.connectionMade(self)

    def connectionLost(self, reason=protocol.connectionDone):
        self.log.debug('connectionLost: {}'.format(reason))
        reactor.callLater(0.0, self.irc_market.connectionLost, reason)
        return irc.IRCClient.connectionLost(self, reason)

    # ---------------------------------------------
    # callbacks from superclass
    # ---------------------------------------------

    def signedOn(self):
        self.log.debug('signedOn:')
        self.join(self.factory.channel)

    def joined(self, channel):
        self.log.debug('joined: {channel}', channel=channel)
        reactor.callLater(0.0, self.irc_market.joined, channel)

    def privmsg(self, userIn, channel, msg):
        self.log.debug('<-privmsg: {userIn} {channel} {msg}...',
                       userIn=userIn, channel=channel, msg=msg[:80])

        reactor.callLater(0.0, self.irc_market.handle_privmsg,
                          userIn, channel, msg)

    def action(self, user, channel, msg):
        self.log.debug('action: {user}, {channel}, {msg}',
                       user=user, channel=channel, msg=msg)

    def alterCollidedNick(self, nickname):
        """
        Generate an altered version of a nickname that caused a collision in an
        effort to create an unused related name for subsequent registration.
        :param nickname:
        """
        return nickname + '^'

    def modeChanged(self, user, channel, _set, modes, args):
        self.log.debug(
            'modeChanged: {user}, {channel}, {_set}, {modes}, {args}',
            user=user, channel=channel, _set=_set, modes=modes,
            args=args)

    def pong(self, user, secs):
        self.log.debug('pong: {:d}'.format(secs))

    def userJoined(self, user, channel):
        self.log.debug('user joined: {user}, {channel}', user=user,
                       channel=channel)
        reactor.callLater(0.0, self.irc_market.userJoined, user, channel)

    def userKicked(self, kickee, channel, kicker, message):
        self.log.debug('kicked: {} {} by {} {}'.format(kickee, channel, kicker,
                                                       message))

    def userLeft(self, user, channel):
        self.log.debug('left: {} {}'.format(user, channel))
        reactor.callLater(0.0, self.irc_market.userLeft, user, channel)

    def userRenamed(self, oldname, newname):
        self.log.debug('rename: {} {}'.format(oldname, newname))
        reactor.callLater(0.0, self.irc_market.userRenamed, oldname, newname)

    def userQuit(self, user, quitMessage):
        self.log.debug(('quit: {} {}'.format(user, quitMessage)))

    def topicUpdated(self, user, channel, newTopic):
        self.log.debug('topic: {}, {}, {}'.format(user, channel, newTopic))
        reactor.callLater(0.0, self.irc_market.topicUpdated, channel, newTopic)

    def receivedMOTD(self, motd):
        self.log.debug('motd: {}'.format(motd))

    def created(self, when):
        self.log.debug('created: {}'.format(when))

    def yourHost(self, info):
        self.log.debug('yourhost: {}'.format(info))

    def myInfo(self, servername, version, umodes, cmodes):
        self.log.debug('myInfo: {} {} {} {}'.format(servername, version, umodes,
                                                    cmodes))

    def luserChannels(self, channels):
        self.log.debug('luserChannels: {}'.format(channels))

    def bounce(self, info):
        self.log.debug('bounce: {}'.format(info))

    def left(self, channel):
        self.log.debug('left: {}'.format(channel))

    def noticed(self, user, channel, message):
        self.log.debug('notice: {} {} {}'.format(user, channel, message))


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
    # - randomly combine phonetic sounds instead consonants,
    #     which may be two consecutive consonants
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

    def __init__(self, block_instance):
        self.block_instance = block_instance

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

    def announce_orders(self, orderlist, nick=None):
        pass

    def cancel_orders(self, oid_list):
        pass

    def send_pubkey(self, nick, pubkey):
        pass

    def send_ioauth(self, nick, utxo_list, cj_pubkey, change_addr, sig):
        pass

    def send_sigs(self, nick, sig_list):
        pass


class IRC_Market(CommSuper):
    log = Logger()

    def __init__(self,
                 channel,
                 block_instance,
                 username='username',
                 realname='realname',
                 password=None):
        super(IRC_Market, self).__init__(block_instance)

        self.nickname = block_instance.nickname
        self.userrealname = (username, realname)
        if password and len(password) == 0:
            password = None
        self.given_password = password
        ns = self.__module__ + '@' + self.block_instance.nickname
        self.log = Logger(namespace=ns)

        # todo: things like errno need to be documented
        self.errno = 0
        self.channel = channel
        # self.channel = get_config_irc_channel()

        # todo: rename this.  too confusing
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

    def __getattr__(self, name):
        if name == 'cjp':
            return self.block_instance.coinjoinerpeer
        else:
            raise AttributeError('name: {} doesn\'t exist'.format(name))

    def run(self):
        """
        Run defined here for consistency
        :return:
        """
        self.log.debug('Inside IRC_Market.run()')

        def reactor_running():
            self.log.debug('***** RUNNING!!!')

        reactor.callWhenRunning(reactor_running)
        reactor.run()

    def shutdown(self, errno=-1):
        self.errno = errno
        self.log.debug('SHUTDOWN Called (kidding): errno: {errno}',
                       errno=errno)
        traceback.print_stack()

        # todo: disconnection policy
        # disconnect will cause connectionLost which stops the reactor

        # self.block_instance.tcp_connector.disconnect()

    def send(self, send_to, msg):
        self.log.debug('send-> {send_to} {msg}...', send_to=send_to,
                       msg=msg[:80])
        omsg = 'PRIVMSG %s :' % (send_to,) + msg
        self.block_instance.tx_irc_client.sendLine(omsg.encode('ascii'))

    def send_error(self, nick, errormsg):
        self.log.debug('send_error', nick=nick, errormsg=errormsg)
        if 'Unknown format code' in errormsg:
            raise Exception('The Susquehanna Hat Company!!!')
        self.__privmsg(nick, 'error', errormsg)
        raise CJPeerError()

    # -----------------------------------
    # connection callbacks
    # -----------------------------------

    # noinspection PyBroadException
    def joined(self, channel):
        # todo: mode changes needed?
        try:
            self.cjp.on_welcome()
        except:
            self.log.error(traceback.format_exc())
            self.shutdown()

    def userJoined(self, user, channel):
        pass

    # noinspection PyBroadException
    def connectionMade(self, *args, **kwargs):
        try:
            self.log.debug('IRC connection made')
            self.cjp.on_connect(*args, **kwargs)
        except:
            self.log.failure('connectionMade')
            self.shutdown()

    # noinspection PyBroadException
    def connectionLost(self, reason):
        try:
            self.log.debug('IRC connection lost: {}'.format(reason))
            # todo: need policy.  Back on reconnect
            # self.cjp.on_disconnect(reason)
        except:
            self.log.failure('connectionLost')
            self.shutdown()

    # noinspection PyBroadException
    def userLeft(self, *args, **kwargs):
        try:
            self.cjp.on_nick_leave(*args, **kwargs)
        except:
            self.log.failure('userLeft')
            self.shutdown()

    # noinspection PyBroadException
    def userRenamed(self, *args, **kwargs):
        try:
            self.cjp.on_nick_change(*args, **kwargs)
        except:
            self.log.failure('userRenamed')
            self.shutdown()

    # noinspection PyBroadException
    def topicUpdated(self, *args, **kwargs):
        try:
            self.cjp.on_set_topic(*args, **kwargs)
        except:
            self.log.failure('topicUpdated')
            self.shutdown()

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
        # todo: rateLimit handles excessive messages.  NO SLEEPING!!!
        txb64 = base64.b64encode(txhex.decode('hex'))
        for nick in nick_list:
            self.__privmsg(nick, 'tx', txb64)

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
        header = ''  # todo: HACK!! fix this
        orderlines = []
        for i, order in enumerate(orderlist):
            orderparams = (COMMAND_PREFIX + order['ordertype'] +
                           ' ' + ' '.join([str(order[k]) for k in order_keys]))
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
        # todo: message rate is handled at the irc client level. solved.
        for s in sig_list:
            self.__privmsg(nick, 'sig', s)

    def __pubmsg(self, message):
        # self.log.debug('>>pubmsg ' + message)
        # self.send_raw("PRIVMSG " + self.channel + " :" + message)
        self.send(self.channel, message)

    def __privmsg(self, nick, cmd, message):
        # self.log.debug('>>privmsg {nick}, {cmd}, {msg}...',
        #           nick=nick, cmd=cmd, msg=message[:80])
        # should we encrypt?
        box, encrypt = self.__get_encryption_box(cmd, nick)
        # encrypt before chunking
        if encrypt:
            if not box:
                self.log.debug(
                    'error, dont have encryption box object for {nick}, '
                    'dropping message', nick=nick)
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

    # noinspection PyBroadException
    def check_for_orders(self, nick, _chunks):
        if _chunks[0] in ['absorder', 'relorder']:
            try:
                counterparty = nick
                oid = _chunks[1]
                ordertype = _chunks[0]
                minsize = _chunks[2]
                maxsize = _chunks[3]
                txfee = _chunks[4]
                cjfee = _chunks[5]
                self.log.debug(
                        '->on_order_seen, counterparty={counterparty}, '
                        'order type={ordertype}, '
                        'minsize={minsize}, txfee={txfee}, cjfee={cjfee}',
                        counterparty=counterparty, oid=oid,
                        ordertype=ordertype,
                        minsize=minsize, maxsize=maxsize,
                        txfee=txfee, cjfee=cjfee)
                self.cjp.on_order_seen(
                        counterparty, oid, ordertype, minsize, maxsize,
                        txfee, cjfee)
            except:
                self.log.failure('check_for_orders')
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
                    self.cjp.on_pubkey(
                            nick, maker_pk)
                elif _chunks[0] == 'ioauth':
                    utxo_list = _chunks[1].split(',')
                    cj_pub = _chunks[2]
                    change_addr = _chunks[3]
                    btc_sig = _chunks[4]
                    self.cjp.on_ioauth(
                            nick, utxo_list, cj_pub, change_addr, btc_sig)
                elif _chunks[0] == 'sig':
                    sig = _chunks[1]
                    self.cjp.on_sig(nick, sig)

                # maker commands
                if _chunks[0] == 'fill':
                    try:
                        oid = int(_chunks[1])
                        amount = int(_chunks[2])
                        taker_pk = _chunks[3]
                    except (ValueError, IndexError) as e:
                        self.send_error(nick, str(e))
                    # todo: oid , amount, taker_pk referenced before assignment
                    self.cjp.on_order_fill(
                            nick, oid, amount, taker_pk)
                elif _chunks[0] == 'auth':
                    try:
                        i_utxo_pubkey = _chunks[1]
                        btc_sig = _chunks[2]
                    except (ValueError, IndexError) as e:
                        self.send_error(nick, str(e))
                    # todo: i_utxo_pubkey, btc_sig referenced before assignment
                    self.cjp.on_seen_auth(nick, i_utxo_pubkey, btc_sig)
                elif _chunks[0] == 'tx':
                    b64tx = _chunks[1]
                    try:
                        txhex = base64.b64decode(b64tx).encode('hex')
                    except TypeError as e:
                        self.send_error(nick, 'bad base64 tx. ' + repr(e))
                    self.cjp.on_seen_tx(nick, txhex)
                elif _chunks[0] == 'push':
                    b64tx = _chunks[1]
                    try:
                        txhex = base64.b64decode(b64tx).encode('hex')
                    except TypeError as e:
                        self.send_error(nick, 'bad base64 tx. ' + repr(e))
                    self.cjp.on_push_tx(nick, txhex)
            except CJPeerError:
                # TODO proper error handling
                self.log.debug('cj peer error TODO handle')

                # continue ^

    def __on_pubmsg(self, nick, message):
        if message[0] != COMMAND_PREFIX:
            return
        for command in message[1:].split(COMMAND_PREFIX):
            _chunks = command.split(" ")
            if self.check_for_orders(nick, _chunks):
                pass
            elif _chunks[0] == 'cancel':
                # !cancel [oid]
                try:
                    oid = int(_chunks[1])

                    self.cjp.on_order_cancel(
                            nick, oid)
                except ValueError as e:
                    self.log.debug("!cancel " + repr(e))
                    return
            elif _chunks[0] == 'orderbook':
                self.cjp.on_orderbook_requested(nick)

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
            return self.cjp.get_crypto_box_from_nick(nick), True

    def handle_privmsg(self, sent_from, sent_to, message):
        try:

            nick = get_irc_nick(sent_from)
            # todo: kludge - we need this elsewhere. rearchitect!!

            self.from_to = (nick, sent_to)

            if sent_to == self.nickname:
                if nick not in self.built_privmsg:
                    if message[0] != COMMAND_PREFIX:
                        self.log.debug('bad command', msg=message[0])
                        return

                    # new message starting
                    cmd_string = message[1:].split(' ')[0]
                    if (cmd_string not in
                                plaintext_commands + encrypted_commands):
                        self.log.debug('cmd not in cmd_list',
                                       cmd_string=cmd_string)
                        return
                    self.built_privmsg[nick] = [cmd_string, message[:-2]]
                else:
                    self.built_privmsg[nick][1] += message[:-2]
                box, encrypt = self.__get_encryption_box(
                        self.built_privmsg[nick][0], nick)

                # todo: change format, use regex etc
                if message[-1] == ';':
                    self.waiting[nick] = True
                elif message[-1] == '~':
                    self.waiting[nick] = False
                    if encrypt:
                        if not box:
                            self.log.debug('no encryption box, dropping',
                                           nick=nick)
                            return
                        # need to decrypt everything after the command string
                        to_decrypt = ''.join(
                                self.built_privmsg[nick][1].split(' ')[1])
                        try:
                            decrypted = decode_decrypt(to_decrypt, box)
                        except ValueError:
                            self.log.failure('bad format decrypt')
                            return
                        parsed = self.built_privmsg[nick][1].split(' ')[0]
                        parsed += ' ' + decrypted
                    else:
                        parsed = self.built_privmsg[nick][1]
                    # wipe the message buffer waiting for the next one
                    # todo: kinda tricky here.  rearchitect!!
                    del self.built_privmsg[nick]

                    # self.log.debug("<<privmsg:", nick=nick, parsed=parsed)
                    self.__on_privmsg(nick, parsed)
                else:
                    # drop the bad nick
                    del self.built_privmsg[nick]
            elif sent_to == self.channel:
                # self.log.debug("<<pubmsg", nick=nick, message=message)
                self.__on_pubmsg(nick, message)
            else:
                self.log.debug('what is this?: {sent_from}, {sent_to}, {msg}',
                               sent_from=sent_from, sent_to=sent_to,
                               msg=message[:80])
        except JsonRpcError:
            self.log.failure('general I guess')
        except:
            self.log.failure('severe')
            self.shutdown()


# -----------------------------------------------------
# Twisted Infrastructure
# -----------------------------------------------------


class LogBotFactory(protocol.ClientFactory):
    def __init__(self, channel, the_cred):
        self.channel = channel
        self.the_cred = the_cred

    def buildProtocol(self, addr):
        p = txIRC_Client(**self.the_cred)
        p.factory = self
        return p

    # todo: connection info in IRC_Market.  Need reconnect policy
    def clientConnectionLost(self, connector, reason):
        log.info('IRC connection lost: {}'.format(reason))
        # connector.connect()


    def clientConnectionFailed(self, connector, reason):
        log.info("IRC connection failed: {}".format(reason))


ght_cred = {'nickname': 'anutxhg', 'password': '', 'hostname': 'localhost'}


def tor_cyber():
    factory = LogBotFactory('#joinmarket-pit', ght_cred)

    ctx = ClientContextFactory()

    torEndpoint = TCP4ClientEndpoint(reactor, '192.168.1.200', 9050)
    ircEndpoint = SOCKS5ClientEndpoint('6dvj6v5imhny3anf.onion', 6697,
                                       torEndpoint)
    tlsEndpoint = TLSWrapClientEndpoint(ctx, ircEndpoint)

    return tlsEndpoint.connect(factory)


def ssl_cyber():
    factory = LogBotFactory('#joinmarket-pit', ght_cred)

    ctx = ClientContextFactory()
    # ctx = CertificateOptions(verify=False)

    return reactor.connectSSL("irc.cyberguerrilla.org", 6697, factory, ctx)


def home_nosec():
    factory = LogBotFactory('#anarchy', ght_cred)

    return reactor.connectTCP('192.168.1.200', 6667, factory)


def localhost_nosec():
    factory = LogBotFactory('#anarchy', ght_cred)

    return reactor.connectTCP('localhost', 6667, factory)


class BlockInstance(object):
    # todo: we need to do the instance collection thing
    instances = []

    def __init__(self, nickname,
                 username='username',
                 realname='realname',
                 password=None):

        self.nickname = nickname
        self.username = username
        self.realname = realname
        self.password = password

        self.tcp_connector = None
        self.tx_irc_client = None
        self.coinjoinerpeer = None

        self.channel = get_config_irc_channel()
        self.irc_market = IRC_Market(self.channel, self,
                                     username=self.username,
                                     realname=self.realname,
                                     password=self.password)

        BlockInstance.instances.append(self)  # list of everyone important

    def set_coinjoinerpeer(self, cjp):
        log.debug('set_coinjoinerpeer')
        self.coinjoinerpeer = cjp

    def set_tx_irc_client(self, txircclt):
        log.debug('set_tx_irc_client')
        self.tx_irc_client = txircclt

        # from IRC_blah constructor
        # serverport = (config.get("MESSAGING", "host"),
        #               int(config.get("MESSAGING", "port")))
        # socks5_host = config.get("MESSAGING", "socks5_host")
        # socks5_port = int(config.get("MESSAGING", "socks5_port"))

    def build_irc(self):
        if self.tx_irc_client:
            raise Exception('irc already built')

        try:
            log.debug('build_irc')
            # todo: hack password
            cr = {'block_instance': self,
                  'nickname': self.nickname,
                  'password': 'nimDid[Quoc6',
                  'hostname': 'nowhere.com'}

            factory = LogBotFactory(self.channel, cr)

            # todo: hack!!!
            serverport = ('192.168.1.200', 6667)

            self.tcp_connector = reactor.connectTCP(
                    serverport[0], serverport[1], factory)
        except:
            log.failure('build_irc')


__all__ = ('random_nick', 'BlockInstance')
