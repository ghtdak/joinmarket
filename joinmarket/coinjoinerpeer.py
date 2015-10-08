from joinmarket.common import JM_VERSION


class CoinJoinerPeer(object):

    def __init__(self, msgchan):
        self.msgchan = msgchan

    def get_crypto_box_from_nick(self, nick):
        raise Exception()

    def on_set_topic(self, newtopic):
        chunks = newtopic.split('|')
        for msg in chunks[1:]:
            try:
                msg = msg.strip()
                params = msg.split(' ')
                min_version = int(params[0])
                max_version = int(params[1])
                alert = msg[msg.index(params[1]) + len(params[1]):].strip()
            except ValueError, IndexError:
                continue
            if min_version < JM_VERSION and max_version > JM_VERSION:
                print '=' * 60
                print 'JOINMARKET ALERT'
                print alert
                print '=' * 60
