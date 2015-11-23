from message_channel import MessageChannel
from message_channel import CJPeerError

import random
import socket, threading, time, ssl, Queue
import base64, os, re, json
import enc_wrapper, socks
from common import debug
import common

#dont allow yield generators to choose their own nicks, its silly

#joinmarket.cfg has an option which is clearnet-only, prefer-clearnet-over-tor, prefer-tor-over-clearnet, tor-only
#one ping thread tracks all the connection threads
#a connection thread handles building and decrypting private messages, runs a callback when it has the whole thing

#bot must make sure it has the same nicks on all channels

#each thread keeps track of all the makers and takers in the channel, add them to the sets after they've spoken
# if a nick leaves one channel/server it may still be on others
# if a nick leaves and its nowhere else, then emit on_nick_leave()

#what happens if a maker announces or cancels an order on just one place?
# a real maker wouldnt do that, but an impersonator of one might
# maybe best is to err on the side of less censorship? an annoucement on one channel is enough
# to add an order but many channels required to cancel? this requires keeping track
#could also put the responsibility on the maker, they themselves should be on all possible servers to stop others !cancel'ing
#all their offers
#so this would be "follow the last information received"

#for every private message command, just pick a random server to send it through

#each network needs a list of modes like -R to set

# ping thread needs to work for many servers

# waiting until all servers are connected before starting the next step, e.g. tumbler thread
#  this should be on_welcome()
#but on_welcome() announces orders and requests orderbook, it must be called if a single network disconnected us and we reconnect
#alternatively on_welcome() becomes when to start mixing and stuff
#and on_connect() is when to announce orders and request orderbook, it passes an argument called context which will allow
# just one IRCConnection to announce orders when it reconnects
#on_welcome() needs to be renamed to on_ready() maybe
#and a new call which is when all servers are disconnected, maybe called on_not_ready()

#ok they'll be called this
#on_join_subchannel() on_leave_subchannel()
# for individual irc channels, the future p2p implemention might just call these at the same time as on_connected
# these pass an argument for the individual subchannel context, which can be used to send just to that
#on_connected() on_disconnected()

#on_nick_leave() could fire if the same nick on every server has left? though that requires storing the set of names for every channel
#on_nick_change() could be similarly implemented but nobody ever does it so it can be unimplemented
# no its needed for updating the orderbook database

#after some more thinking, this is not too much of a problem
##PROBLEM if the taker only sees a maker's nick on one server, it might be 
#need to only allow maker's orders to reach the taker code if their name appears on several servers
#so maybe when it gets an order it will search the other names sets to see if you're on at least 3 servers in total?

#test when one server ping timeouts
# then reconnecting results in a nick collision, do connections to other ircs correctly change nicks
# tested, works

#tolerate failure to connect, check that you connected to at least N networks, otherwise refuse
# announce orders need to take context and makers announce it when they join the individual channel
# on_connected fires when N > threshold reached, not for all
# takers wont connect to every single server, makers will
# need a timeout for failing to connect, can use the timeout on queue.get()
# the taker can try to connect to another server, and eventually stop if not enough of the servers work
#load irc config from file
#have a simple boolean usetor that will switch to the tor version
#stop nick being used for common.debug() file, instead call that file "tumbler-run-debug-[date].log"

#PLAN
#add the blockchain interface to the irc realname
#test the TODO
#test the whole thing some more
#try sendpayment and tumbler on testnet

#words
#- To reduce centralization, multiple IRC servers could be used. Makers would idle on all of the servers, and takers would find partners using multiple randomly-selected IRC servers. If any servers are down, Joinmarket should issue a warning, as this may be a DoS attack on the IRC server designed to funnel people to attacker-controlled servers.

#taker will refuse to coinjoin unless it can connect to this many these servers
#aim is that a single network cant censor makers and make you only coinjoin with
# its own sybils, also to be robust against an individual network going down
#this should be in the irc config file instead
N_DECENTRALIZED = 3

MAX_PRIVMSG_LEN = 400
COMMAND_PREFIX = '!'
PING_INTERVAL = 60 * 4
PING_TIMEOUT = 60 * 1
DISCONNECT_WAIT = 60
NEW_NICK_WAIT = 5
encrypted_commands = ["auth", "ioauth", "tx", "sig"]
plaintext_commands = ["fill", "error", "pubkey", "orderbook", "relorder",
                      "absorder", "push"]


def random_nick(nick_len=9):
    vowels = "aeiou"
    consonants = ''.join([chr(
        c) for c in range(
            ord('a'), ord('z') + 1) if vowels.find(chr(c)) == -1])
    assert nick_len % 2 == 1
    N = (nick_len - 1) / 2
    rnd_consonants = [consonants[random.randrange(len(consonants))]
                      for c in range(N + 1)]
    rnd_vowels = [vowels[random.randrange(len(vowels))]
                  for v in range(N)] + ['']
    ircnick = ''.join([i for sl in zip(rnd_consonants, rnd_vowels) for i in sl])
    ircnick = ircnick.capitalize()
    print 'Generated random nickname: ' + ircnick  #not using debug because it might not know the logfile name at this point
    return ircnick
    #Other ideas for random nickname generation:
    # - weight randomness by frequency of letter appearance
    # - u always follows q
    # - generate different length nicks
    # - append two or more of these words together
    # - randomly combine phonetic sounds instead consonants, which may be two consecutive consonants
    #  - e.g. th, dj, g, p, gr, ch, sh, kr, 
    # - neutral network that generates nicks


def get_irc_channel(channel):
    if common.get_network() == 'testnet':
        channel += '-test'
    return channel


def get_irc_text(line):
    return line[line[1:].find(':') + 2:]


def get_irc_nick(source):
    return source[1:source.find('!')]


class PingThread(threading.Thread):

    def __init__(self, irc_connections, ping_lock):
        threading.Thread.__init__(self)
        self.daemon = True
        self.irc_connections = irc_connections
        self.ping_lock = ping_lock

    def run(self):
        debug('starting ping thread')
        #while at least one of i.give_up_reconnecting is false
        while not all([i.give_up_reconnecting for i in self.irc_connections]):
            time.sleep(PING_INTERVAL)
            try:
                self.ping_lock.acquire()
                for irc in self.irc_connections:
                    irc.ping_replied = False
                    irc.ping_reply_notify = False
                    #maybe use this to calculate the lag one day
                    irc.send_raw('PING LAG' + str(int(time.time() * 1000)))
                while True:
                    self.ping_lock.wait(PING_TIMEOUT)
                    notified_connections = [c
                                            for c in self.irc_connections
                                            if c.ping_reply_notify]
                    if len(notified_connections) > 0:
                        #server replied to ping
                        for c in notified_connections:
                            c.ping_reply_notify = False
                        if all([c.ping_replied for c in self.irc_connections]):
                            #all connections replied to ping
                            break
                    else:
                        #timed out
                        debug('irc ping timed out')
                        nonresponding_connections = [
                            c for c in self.irc_connections
                            if not c.ping_replied
                        ]
                        for c in nonresponding_connections:
                            try:
                                c.quit()
                            except:
                                pass
                            try:
                                c.fd.close()
                            except:
                                pass
                            try:
                                c.sock.shutdown(socket.SHUT_RDWR)
                                c.sock.close()
                            except:
                                pass
                        break
                self.ping_lock.release()
            except IOError as e:
                debug('ping thread: ' + repr(e), printexc=True)
        debug('ended ping thread')


class IRCConnection(threading.Thread):
    '''
	Runs a thread which connects to IRC, builds up private messages and calls back to
	the JoinMarket MessageChannel class
	'''

    def pubmsg(self, message):
        debug('(' + self.connection_config['host'][:9] + ')>> pubmsg ' +
              message)
        self.send_raw("PRIVMSG " + self.channel + " :" + message)

    def privmsg(self, nick, cmd, message):
        debug('(' + self.connection_config['host'][:9] + ')>> privmsg ' +
              'nick=' + nick + ' cmd=' + cmd + ' msg=' + message)
        #should we encrypt?
        box, encrypt = self.__get_encryption_box(cmd, nick)
        #encrypt before chunking
        if encrypt:
            if not box:
                debug('error, dont have encryption box object for ' + nick +
                      ', dropping message')
                return
            message = enc_wrapper.encrypt_encode(message, box)

        header = "PRIVMSG " + nick + " :"
        max_chunk_len = MAX_PRIVMSG_LEN - len(header) - len(cmd) - 4
        #1 for command prefix 1 for space 2 for trailer
        if len(message) > max_chunk_len:
            message_chunks = common.chunks(message, max_chunk_len)
        else:
            message_chunks = [message]
        for m in message_chunks:
            trailer = ' ~' if m == message_chunks[-1] else ' ;'
            if m == message_chunks[0]:
                m = COMMAND_PREFIX + cmd + ' ' + m
            self.send_raw(header + m + trailer)

    def __check_for_orders(self, nick, chunks):
        if chunks[0] in common.ordername_list:
            try:
                counterparty = nick
                oid = chunks[1]
                ordertype = chunks[0]
                minsize = chunks[2]
                maxsize = chunks[3]
                txfee = chunks[4]
                cjfee = chunks[5]
                if self.msgchan.on_order_seen:
                    self.irc_event_queue.put((self.msgchan.on_order_seen,
                                              (counterparty, oid, ordertype,
                                               minsize, maxsize, txfee, cjfee)))
            except IndexError as e:
                debug('(' + self.connection_config['host'][:9] +
                      ') index error parsing chunks')
                #TODO what now? just ignore iirc
            finally:
                return True
        return False

    def __on_privmsg(self, nick, message):
        '''handles the case when a private message is received'''
        if message[0] != COMMAND_PREFIX:
            return
        for command in message[1:].split(COMMAND_PREFIX):
            chunks = command.split(" ")
            #looks like a very similar pattern for all of these
            # check for a command name, parse arguments, call a function
            # maybe we need some eval() trickery to do it better

            try:
                #orderbook watch commands
                if self.__check_for_orders(nick, chunks):
                    pass

                #taker commands
                elif chunks[0] == 'pubkey':
                    maker_pk = chunks[1]
                    if self.msgchan.on_pubkey:
                        self.irc_event_queue.put((self.msgchan.on_pubkey, (
                            nick, maker_pk)))
                elif chunks[0] == 'ioauth':
                    utxo_list = chunks[1].split(',')
                    cj_pub = chunks[2]
                    change_addr = chunks[3]
                    btc_sig = chunks[4]
                    if self.msgchan.on_ioauth:
                        self.irc_event_queue.put((self.msgchan.on_ioauth, (
                            nick, utxo_list, cj_pub, change_addr, btc_sig)))
                elif chunks[0] == 'sig':
                    sig = chunks[1]
                    if self.msgchan.on_sig:
                        self.irc_event_queue.put((self.msgchan.on_sig, (nick,
                                                                        sig)))

                #maker commands
                if chunks[0] == 'fill':
                    try:
                        oid = int(chunks[1])
                        amount = int(chunks[2])
                        taker_pk = chunks[3]
                    except (ValueError, IndexError) as e:
                        self.send_error(nick, str(e))
                    if self.msgchan.on_order_fill:
                        self.irc_event_queue.put((self.msgchan.on_order_fill, (
                            nick, oid, amount, taker_pk)))
                elif chunks[0] == 'auth':
                    try:
                        i_utxo_pubkey = chunks[1]
                        btc_sig = chunks[2]
                    except (ValueError, IndexError) as e:
                        self.send_error(nick, str(e))
                    if self.msgchan.on_seen_auth:
                        self.irc_event_queue.put((self.msgchan.on_seen_auth, (
                            nick, i_utxo_pubkey, btc_sig)))
                elif chunks[0] == 'tx':
                    b64tx = chunks[1]
                    try:
                        txhex = base64.b64decode(b64tx).encode('hex')
                    except TypeError as e:
                        self.send_error(nick, 'bad base64 tx. ' + repr(e))
                    if self.msgchan.on_seen_tx:
                        self.irc_event_queue.put((self.msgchan.on_seen_tx, (
                            nick, txhex)))
                elif chunks[0] == 'push':
                    b64tx = chunks[1]
                    try:
                        txhex = base64.b64decode(b64tx).encode('hex')
                    except TypeError as e:
                        self.send_error(nick, 'bad base64 tx. ' + repr(e))
                    if self.msgchan.on_push_tx:
                        self.irc_event_queue.put((self.msgchan.on_push_tx, (
                            nick, txhex)))
            except CJPeerError:
                #TODO proper error handling
                debug('(' + self.connection_config['host'][:9] +
                      ') cj peer error TODO handle')
                continue

    def __on_pubmsg(self, nick, message):
        if message[0] != COMMAND_PREFIX:
            return
        for command in message[1:].split(COMMAND_PREFIX):
            chunks = command.split(" ")
            if self.__check_for_orders(nick, chunks):
                pass
            elif chunks[0] == 'cancel':
                #!cancel [oid]
                try:
                    oid = int(chunks[1])
                    if self.msgchan.on_order_cancel:
                        self.irc_event_queue.put((self.msgchan.on_order_cancel,
                                                  (nick, oid)))
                except ValueError as e:
                    debug("!cancel " + repr(e))
                    return
            elif chunks[0] == 'orderbook':
                if self.msgchan.on_orderbook_requested:
                    self.irc_event_queue.put((
                        self.msgchan.on_orderbook_requested, (nick,)))

    def __get_encryption_box(self, cmd, nick):
        '''Establish whether the message is to be
		encrypted/decrypted based on the command string.
		If so, retrieve the appropriate crypto_box object
		and return. Sending/receiving flag enables us
		to check which command strings correspond to which
		type of object (maker/taker).''' #old doc, dont trust
        if cmd in plaintext_commands:
            return None, False
        else:
            return self.msgchan.cjpeer.get_crypto_box_from_nick(nick), True

    def __handle_privmsg(self, source, target, message):
        nick = get_irc_nick(source)
        if target == self.nick:
            if message[0] == '\x01':
                endindex = message[1:].find('\x01')
                if endindex == -1:
                    return
                ctcp = message[1:endindex + 1]
                if ctcp.upper() == 'VERSION':
                    self.send_raw('PRIVMSG ' + nick +
                                  ' :\x01VERSION xchat 2.8.8 Ubuntu\x01')
                    return

            if nick not in self.msgchan.built_privmsg:
                if message[0] != COMMAND_PREFIX:
                    debug('(' + self.connection_config['host'][:9] +
                          ') message not a cmd')
                    return
                #new message starting
                cmd_string = message[1:].split(' ')[0]
                if cmd_string not in plaintext_commands + encrypted_commands:
                    debug('(' + self.connection_config['host'][:9] +
                          ') cmd not in cmd_list, line="' + message + '"')
                    return
                self.msgchan.built_privmsg[nick] = [cmd_string, message[:-2]]
            else:
                self.msgchan.built_privmsg[nick][1] += message[:-2]
            box, encrypt = self.__get_encryption_box(
                self.msgchan.built_privmsg[nick][0], nick)
            if message[-1] == ';':
                pass
            elif message[-1] == '~':
                if encrypt:
                    if not box:
                        debug('(' + self.connection_config['host'][:9] +
                              ') error, dont have encryption box object for ' +
                              nick + ', dropping message')
                        return
                    #need to decrypt everything after the command string
                    to_decrypt = ''.join(self.msgchan.built_privmsg[nick][
                        1].split(' ')[1])
                    try:
                        decrypted = enc_wrapper.decode_decrypt(to_decrypt, box)
                    except ValueError as e:
                        debug(
                            'valueerror when decrypting, skipping: ' + repr(e),
                            printexc=True)
                        return
                    parsed = self.msgchan.built_privmsg[nick][1].split(' ')[
                        0] + ' ' + decrypted
                else:
                    parsed = self.msgchan.built_privmsg[nick][1]
                #wipe the message buffer waiting for the next one
                del self.msgchan.built_privmsg[nick]
                debug('(' + self.connection_config['host'][:9] +
                      ")<< privmsg nick=%s message=%s" % (nick, parsed))
                self.__on_privmsg(nick, parsed)
            else:
                #drop the bad nick
                del self.msgchan.built_privmsg[nick]
        elif target == self.channel:
            debug('(' + self.connection_config['host'][:9] +
                  ")<< pubmsg nick=%s message=%s" % (nick, message))
            self.__on_pubmsg(nick, message)
        else:
            debug('(' + self.connection_config['host'][:9] +
                  ')what is this? privmsg src=%s target=%s message=%s;' % (
                      source, target, message))

    def quit(self):
        try:
            self.send_raw("QUIT")
        except IOError as e:
            debug('(' + self.connection_config['host'][:9] +
                  ') errored while trying to quit: ' + repr(e))

    def shutdown(self):
        self.quit()
        self.give_up_reconnecting = True

    def send_raw(self, line):
        #if not line.startswith('PING LAG'):
        #	debug(self.connection_config['host'][:9] + ' sendraw ' + line)
        self.sock.sendall(line + '\r\n')

    def __handle_line(self, line):
        #debug(self.connection_config['host'][:9] + '<< ' + line.rstrip())
        if line.startswith('PING '):
            self.send_raw(line.replace('PING', 'PONG'))
            return

        line = line.rstrip()
        chunks = line.split(' ')
        if chunks[1] == 'QUIT':
            nick = get_irc_nick(chunks[0])
            with self.names_lock:
                self.names.discard(nick)
            if nick == self.nick:
                raise IOError('we quit')
            else:
                self.irc_event_queue.put((self.msgchan.check_nick_event, (
                    nick, None)))
        elif chunks[1] in ['432', '433', '436'
                          ]:  #nick error, nick in use, nick collision
            self.irc_event_queue.put((self.msgchan.our_nick_unsuitable, ()))
        if self.password:
            if chunks[1] == 'CAP':
                if chunks[3] != 'ACK':
                    debug('(' + self.connection_config['host'][:9] +
                          ') server does not support SASL, quitting')
                    self.shutdown()
                self.send_raw('AUTHENTICATE PLAIN')
            elif chunks[0] == 'AUTHENTICATE':
                self.send_raw('AUTHENTICATE ' + base64.b64encode(
                    self.nick + '\x00' + self.nick + '\x00' + self.password))
            elif chunks[1] == '903':
                debug('(' + self.connection_config['host'][:9] +
                      ') Successfully authenticated')
                self.password = None
                self.send_raw('CAP END')
            elif chunks[1] == '904':
                debug('(' + self.connection_config['host'][:9] +
                      ') Failed authentication, wrong password')
                self.shutdown()
            return

        if chunks[1] == 'PRIVMSG':
            self.__handle_privmsg(chunks[0], chunks[2], get_irc_text(line))
            text = get_irc_text(line)
        if chunks[1] == 'PONG':
            with self.ping_lock:
                self.ping_reply_notify = True
                self.ping_replied = True
                self.ping_lock.notify()
        elif chunks[1] in ['376', '422']:  #end of motd, no motd file
            if 'usermodes' in self.connection_config:
                for mode in self.connection_config['usermodes']:
                    self.send_raw('MODE ' + self.nick + ' ' + mode)
            self.send_raw('JOIN ' + self.channel)
        elif chunks[1] == '353':  #names in channel
            nameslist = get_irc_text(line)
            with self.names_lock:
                for n in nameslist.split(' '):
                    self.names.add(n)
        elif chunks[1] == '366':  #end of names list
            debug('(' + self.connection_config['host'][:9] + ') names = ' + str(
                self.names))
            self.joined_channel = True
            self.irc_event_queue.put((self.msgchan.join_chan_callback, (self,)))
        elif chunks[1] == '332' or chunks[1] == 'TOPIC':
            topic = get_irc_text(line)
            if self.msgchan.on_set_topic:
                self.irc_event_queue.put((self.msgchan.on_set_topic, (topic,)))
        elif chunks[1] == 'KICK':
            target = chunks[3]
            nick = get_irc_nick(chunks[0])
            with self.names_lock:
                self.names.discard(nick)
            if target == self.nick:
                self.give_up_reconnecting = True
                raise IOError(get_irc_nick(chunks[
                    0]) + ' has kicked us from the irc channel! Reason=' +
                              get_irc_text(line))
            else:
                self.irc_event_queue.put((self.msgchan.check_nick_event, (
                    nick, None)))
        elif chunks[1] == 'PART':
            nick = get_irc_nick(chunks[0])
            with self.names_lock:
                self.names.discard(nick)
            self.irc_event_queue.put((self.msgchan.check_nick_event, (nick, None
                                                                     )))
        elif chunks[1] == 'JOIN':
            channel = chunks[2][1:]
            nick = get_irc_nick(chunks[0])
            with self.names_lock:
                self.names.add(nick)
        elif chunks[1] == 'NICK':
            #:nick!user@host NICK newnick
            oldnick = get_irc_nick(chunks[0])
            newnick = chunks[2]
            with self.names_lock:
                self.names.discard(oldnick)
                self.names.add(newnick)
            self.irc_event_queue.put((self.msgchan.check_nick_event, (oldnick,
                                                                      newnick)))

    def __init__(self,
                 msgchan,
                 irc_event_queue,
                 connection_config,
                 ping_lock,
                 nick,
                 username='username',
                 realname='realname'):
        threading.Thread.__init__(self)
        self.msgchan = msgchan
        self.irc_event_queue = irc_event_queue
        self.connection_config = connection_config
        self.ping_lock = ping_lock
        self.nick = nick

        self.daemon = True

        self.joined_channel = False
        self.give_up_reconnecting = False
        self.start_time = int(time.time())

        self.socks5_info = {'localhost':
                            common.config.get("MESSAGING", "socks5_host"),
                            'port':
                            int(common.config.get("MESSAGING", "socks5_port"))}

        self.names = set()
        self.names_lock = threading.Condition()

        self.userrealname = (username, realname)
        print connection_config
        self.channel = get_irc_channel(self.connection_config['channel'])

    def run(self):

        while not self.give_up_reconnecting:
            try:
                debug('(' + self.connection_config['host'][:9] +
                      ') connecting..')
                if self.msgchan.use_tor:
                    debug('(' + self.connection_config['host'][:9] +
                          ") Using socks5 proxy %s:%d" % (self.socks5_info[
                              'host'], self.socks5_info['port']))
                    socks.setdefaultproxy(socks.PROXY_TYPE_SOCKS5,
                                          self.socks5_info['host'],
                                          self.socks5_info['port'], True)
                    self.sock = socks.socksocket()
                else:
                    self.sock = socket.socket(socket.AF_INET,
                                              socket.SOCK_STREAM)
                self.sock.connect((self.connection_config['host'],
                                   self.connection_config['port']))
                if self.connection_config['usessl']:
                    self.sock = ssl.wrap_socket(self.sock)
                self.fd = self.sock.makefile()
                self.password = None
                if self.connection_config['password']:
                    self.password = self.connection_config['password']
                    self.send_raw('CAP REQ :sasl')
                self.send_raw('USER %s b c :%s' % self.userrealname)
                self.send_raw('NICK ' + self.nick)
                while 1:
                    try:
                        line = self.fd.readline()
                    except AttributeError as e:
                        raise IOError(repr(e))
                    if line == None:
                        debug('(' + self.connection_config['host'][:9] +
                              ') line returned null')
                        break
                    if len(line) == 0:
                        debug('(' + self.connection_config['host'][:9] +
                              ') line was zero length')
                        break
                    self.__handle_line(line)
            except IOError as e:
                debug('(' + self.connection_config['host'][:9] + ') IRC ' +
                      repr(e),
                      printexc=True)
            finally:
                try:
                    self.fd.close()
                    self.sock.close()
                except Exception as e:
                    debug('(' + self.connection_config['host'][:9] +
                          ') IRCclose ' + repr(e),
                          printexc=True)
            self.irc_event_queue.put((self.msgchan.leave_chan_callback, (self,)
                                     ))
            debug('(' + self.connection_config['host'][:9] +
                  ') disconnected irc')
            if not self.give_up_reconnecting:
                time.sleep(DISCONNECT_WAIT)
        debug('(' + self.connection_config['host'][:9] + ') ending irc')
        self.give_up_reconnecting = True


def list_get_remove(li, i):
    r = li[i]
    del li[i]
    return r


class IRCMessageChannel(MessageChannel):

    def __init__(self, connect_to_all=False):
        MessageChannel.__init__(self)
        self.connect_to_all = connect_to_all
        self.cjpeer = None  #subclasses of CoinJoinerPeer have to set this to self
        #load the config from file
        self.built_privmsg = {}

        irc_connection_config_path = common.config.get("MESSAGING",
                                                       "irc_config_file")
        fd = open(irc_connection_config_path, 'r')
        connection_config_str = fd.read()
        fd.close()

        self.use_tor = common.config.get("MESSAGING",
                                         "usetor").lower() == 'true'
        key = 'clearnet' if not self.use_tor else 'tor'
        self.connection_configs = json.loads(connection_config_str)
        self.connection_configs = [c
                                   for c in self.connection_configs[key]
                                   if c['use']]
        self.connection_configs = [list_get_remove(
            self.connection_configs,
            random.randrange(len(self.connection_configs)))
                                   for i in range(N_DECENTRALIZED)]
        #TODO test this

    def run(self):

        #create lots of irc threads here
        #create the ping thread, which pings for all irc

        self.irc_connections = None  #will be a list
        joined_channel_count = [
            0
        ]  # this is a list so it can be passed-by-reference

        def join_chan_callback(irc_connection):
            if self.on_join_subchannel:
                self.on_join_subchannel(irc_connection)
            joined_channel_count[0] += 1
            n_threshold = len(
                self.irc_connections) if self.connect_to_all else N_DECENTRALIZED
            if joined_channel_count[0] >= n_threshold:
                debug('Connected to IRC messaging channel')
                if self.on_connected:
                    self.on_connected()

        self.join_chan_callback = join_chan_callback

        def leave_chan_callback(irc_connection):
            if self.on_leave_subchannel:
                self.on_leave_subchannel(irc_connection)
            joined_channel_count[0] -= 1
            if joined_channel_count[0] == 0:
                if self.on_disconnected:
                    self.on_disconnected()

        self.leave_chan_callback = leave_chan_callback

        def check_nick_event(nick, newnick):
            #running in this thread to stop race conditions
            for c in self.irc_connections:
                with c.names_lock:
                    if nick in c.names:
                        return
            if newnick == None:
                if self.on_nick_leave:
                    self.on_nick_leave(nick)
            else:
                if self.on_nick_change:
                    self.on_nick_change(nick, newnick)

        self.check_nick_event = check_nick_event

        def our_nick_unsuitable():
            time.sleep(NEW_NICK_WAIT)
            newnick = random_nick()
            for c in self.irc_connections:
                c.nick = newnick
                c.send_raw('NICK ' + newnick)

        self.our_nick_unsuitable = our_nick_unsuitable

        ping_lock = threading.Condition()
        irc_event_queue = Queue.Queue()
        nick = random_nick()

        self.irc_connections = [
            IRCConnection(self, irc_event_queue, c, ping_lock, nick)
            for c in self.connection_configs
        ]
        pingthread = PingThread(self.irc_connections, ping_lock)
        pingthread.start()
        [c.start() for c in self.irc_connections]
        #while at least one of i.give_up_reconnecting is false
        while not all([i.give_up_reconnecting for i in self.irc_connections]):
            try:
                fun, args = irc_event_queue.get(block=True, timeout=10)
                fun(*args)
            except Queue.Empty:
                pass  #this allows you to use ctrl+c to kill the process
                #TODO use joined_channel and start_time to implement
                # a timeout to use another irc network if this one doesnt respond

        debug('Ended irc messaging channel')

    def shutdown(self):
        [c.shutdown() for c in self.irc_connections]

    def get_random_connection(self, nick):
        connections_with_nick = [c
                                 for c in self.irc_connections
                                 if nick in c.names]
        if len(connections_with_nick) == 0:
            #should do nothing, in line with the single irc network behavour
            #raise RuntimeError('nick ' + nick + ' not found in any irc')
            debug('ERROR nick ' + nick +
                  ' not found in any irc. Doing nothing.')

            #TODO fix this into not crashing
            class FakeIRCConnection(object):

                def send_raw(self, line):
                    pass

                def privmsg(self, nick, cmd, msg):
                    pass

                def pubmsg(self, line):
                    pass

            return FakeIRCConnection()

        return random.choice(connections_with_nick)

    def send_error(self, nick, errormsg):
        debug('error<%s> : %s' % (nick, errormsg))
        get_random_connection(nick).privmsg(nick, 'error', errormsg)
        raise CJPeerError()

    #OrderbookWatch callback
    def request_orderbook(self, context=None):
        if context:
            context.pubmsg(COMMAND_PREFIX + 'orderbook')
        else:
            for c in self.irc_connections:
                c.pubmsg(COMMAND_PREFIX + 'orderbook')

    #Taker callbacks
    def fill_orders(self, nickoid_dict, cj_amount, taker_pubkey):
        for nick, oid in nickoid_dict.iteritems():
            msg = str(oid) + ' ' + str(cj_amount) + ' ' + taker_pubkey
            self.get_random_connection(nick).privmsg(nick, 'fill', msg)

    def send_auth(self, nick, pubkey, sig):
        message = pubkey + ' ' + sig
        self.get_random_connection(nick).privmsg(nick, 'auth', message)

    def send_tx(self, nick_list, txhex):
        txb64 = base64.b64encode(txhex.decode('hex'))
        for nick in nick_list:
            self.get_random_connection(nick).privmsg(nick, 'tx', txb64)
            time.sleep(
                1)  #HACK! really there should be rate limiting, see issue#31

    def push_tx(self, nick, txhex):
        txb64 = base64.b64encode(txhex.decode('hex'))
        self.get_random_connection(nick).privmsg(nick, 'push', txb64)

    #Maker callbacks
    def announce_orders(self, orderlist, nick=None, context=None):
        #nick = None means announce to the channel(s), otherwise PM to the nick
        #context != None means announce to only that irc connection
        # None will send to every connection if publicly announcing
        # or randomly choose a connection if sending to single nick
        order_keys = ['oid', 'minsize', 'maxsize', 'txfee', 'cjfee']
        #note: this function is really hacky, not an example of elegant code
        longest_channel = max(
            [c.channel for c in self.irc_connections],
            key=len)

        def make_header(target):
            return 'PRIVMSG ' + target + ' :'

        header = make_header((nick if nick else longest_channel))
        ordertokens = []
        orders_to_send = []
        for i, order in enumerate(orderlist):
            orderparams = COMMAND_PREFIX + order['ordertype'] +\
             ' ' + ' '.join([str(order[k]) for k in order_keys])
            ordertokens.append(orderparams)
            line = ''.join(ordertokens)
            if len(header) + len(line) > MAX_PRIVMSG_LEN or i == len(
                    orderlist) - 1:
                if i < len(orderlist) - 1:
                    line = ''.join(ordertokens[:-1])
                orders_to_send.append(line)
                ordertokens = [ordertokens[-1]]
        if nick:
            if not context:
                context = self.get_random_connection(nick)
            for o in orders_to_send:
                line = make_header(nick) + o + ' ~'
                context.send_raw(line)
        else:
            if context:
                cons = [context]
            else:
                cons = self.irc_connections
            for c in cons:
                for o in orders_to_send:
                    line = make_header(c.channel) + o + ' ~'
                    c.send_raw(line)

    def cancel_orders(self, oid_list):
        clines = [COMMAND_PREFIX + 'cancel ' + str(oid) for oid in oid_list]
        for c in self.irc_connections:
            c.__pubmsg(''.join(clines))

    def send_pubkey(self, nick, pubkey):
        self.get_random_connection(nick).privmsg(nick, 'pubkey', pubkey)

    def send_ioauth(self, nick, utxo_list, cj_pubkey, change_addr, sig):
        authmsg = (str(','.join(utxo_list)) + ' ' + cj_pubkey + ' ' +
                   change_addr + ' ' + sig)
        self.get_random_connection(nick).privmsg(nick, 'ioauth', authmsg)

    def send_sigs(self, nick, sig_list):
        #TODO make it send the sigs on one line if there's space
        c = self.get_random_connection(nick)
        for s in sig_list:
            c.privmsg(nick, 'sig', s)
            time.sleep(
                0.5)  #HACK! really there should be rate limiting, see issue#31


if __name__ == "__main__":
    import sys
    irc = IRCMessageChannel()
    irc.register_channel_callbacks(
        lambda x: sys.stdout.write('onsettopic ' + x + '\r\n'),
        lambda: sys.stdout.write('onconnected\r\n'),
        lambda: sys.stdout.write('ondisconnected\r\n'),
        lambda c: sys.stdout.write('onjoinchan ctx=' + str(c) + '\r\n'),
        lambda c: sys.stdout.write('onleavechan ctx=' + str(c) + '\r\n'),
        lambda n: sys.stdout.write('onnickleave ' + n + '\r\n'),
        lambda no, nn: sys.stdout.write('onnickchange old=' + no + ' new=' + nn + '\r\n'))
    irc.register_orderbookwatch_callbacks(
        lambda c, o, t, n, x, f, cf: sys.stdout.write('onorderseen cp=%s oid=%s type=%s min=%s max=%s txf=%s cjf=%s\r\n' % (c, o, t, n, x, f, cf)),
        lambda n, o: sys.stdout.write('onordercancel nick=' + n + ' oid=' + str(o) + '\r\n'))
    #irc.register_taker_callbacks(self, on_error=None, on_pubkey=None, on_ioauth=None,
    #irc.register_maker_callbacks(self, on_orderbook_requested=None, on_order_fill=None,
    irc.run()
