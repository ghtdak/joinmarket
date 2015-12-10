class CJPeerError(StandardError):
    pass


class MessageChannel(object):
    """
	Abstract class which implements a way for bots to communicate
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
