from __future__ import absolute_import, print_function
import sys
from time import ctime

from twisted.cred import checkers, portal
from twisted.internet import reactor, protocol
from twisted.internet.endpoints import TCP4ServerEndpoint
from twisted.logger import Logger
from twisted.words.protocols import irc
from twisted.words import service

from joinmarket.core.configure import config

log = Logger()

ROOM = 'room'


class JmIrc(irc.IRC):

    log = Logger()

    def __init__(self):
        self.log.debug('JmIrc launched')

    def connectionMade(self):
        self.log.debug('connectionMade')

    def __getattr__(self, name):
        return self.do_nothing

    def do_nothing(self, *args, **kwargs):
        log.debug('do_nothing: {args} {kwargs}', args=args, kwargs=kwargs)


class JmIRCFactory(protocol.Factory):

    protocol = JmIrc

    def __init__(self):
        pass

    def buildProtocol(self, addr):
        p = JmIrc()
        p.factory = self
        return p


def buildServer(users):

    # # Initialize the Cred authentication system used by the IRC server.
    # realm = service.InMemoryWordsRealm('testrealm')
    # realm.addGroup(service.Group(ROOM))
    # user_db = checkers.InMemoryUsernamePasswordDatabaseDontUse(**users)
    # _portal = portal.Portal(realm, [user_db])

    # IRC server factory.
    # ircfactory = JmIRCFactory(realm, _portal)

    # Connect a server to the TCP port 6667 endpoint and start listening.
    # endpoint = TCP4ServerEndpoint(reactor, 6667)
    # endpoint.listen(ircfactory)

    reactor.listenTCP(6667, JmIRCFactory())

USERS = dict(
    user1='pass',
    user2='pass',
    user3='pass',
    user4='pass')

def run():
    reactor.callWhenRunning(buildServer, USERS)
    reactor.run()

if __name__ == '__main__':
    run()
