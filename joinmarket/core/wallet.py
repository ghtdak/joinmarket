from __future__ import absolute_import, print_function

from collections import defaultdict
import json
import os
import time
from decimal import Decimal
from getpass import getpass

from configparser import NoSectionError
from twisted.logger import Logger

from . import jmbtc as btc
from .abstracts import AbstractWallet
from .blockchaininterface import BitcoinCoreInterface, bc_interface
from .configure import config
from .configure import get_network
from .jsonrpc import JsonRpcError
from .slowaes import decryptData
from .support import system_shutdown

log = Logger()

class LessAbstractWallet(AbstractWallet):
    """
    Abstract Wallet wasn't sufficiently abstract
    """

    def __init__(self):
        super(LessAbstractWallet, self).__init__()
        self.utxo_selector = btc.select  # default fallback: upstream
        try:
            if config.get("POLICY", "merge_algorithm") == "gradual":
                self.utxo_selector = select_gradual
            elif config.get("POLICY", "merge_algorithm") == "greedy":
                self.utxo_selector = select_greedy
            elif config.get("POLICY", "merge_algorithm") == "greediest":
                self.utxo_selector = select_greediest
            elif config.get("POLICY", "merge_algorithm") != "default":
                raise Exception("Unknown merge algorithm")
        except NoSectionError:
            pass

    def select_utxos(self, mixdepth, amount):
        utxo_list = self.get_utxos_by_mixdepth()[mixdepth]
        unspent = [{'utxo': utxo,
                    'value': addrval['value']}
                   for utxo, addrval in utxo_list.iteritems()]

        self.log.debug('select_utxos unspent')
        # print(pprint.pformat(unspent))
        try:
            inputs = self.utxo_selector(unspent, amount)
        except:
            self.log.debug('insufficient funds')
            raise

        log.debug('for mixdepth={} amount={} selected:'.format(
                mixdepth, amount))

        return dict([(i['utxo'],
                      {'value': i['value'],
                       'address': utxo_list[i['utxo']]['address']})
                     for i in inputs])

    def get_balance_by_mixdepth(self):
        mix_balance = defaultdict(int)
        for mixdepth, utxos in self.get_utxos_by_mixdepth().iteritems():
            for addrval in utxos.values():
                mix_balance[mixdepth] += addrval['value']
        return mix_balance


class Wallet(LessAbstractWallet):

    def __init__(self, seedarg, max_mix_depth=2, gaplimit=6,
                 extend_mixdepth=False, storepassword=False):
        super(Wallet, self).__init__()
        self.max_mix_depth = max_mix_depth
        self.storepassword = storepassword
        self.wallet_synced = False
        self.addr_cache = {}
        self.unspent = {}
        self.spent_utxos = []
        self.imported_privkeys = {}
        self.path = None
        self.index_cache = None
        self.password_key = None
        self.walletdata = None
        self.seed = None
        self.gaplimit = gaplimit
        self.keys = None
        self.index = None
        self.doInit(seedarg, extend_mixdepth, max_mix_depth)

    def doInit(self, seedarg, extend_mixdepth, max_mix_depth):
        """
        key is address, value is (mixdepth, forchange, index) if mixdepth =
        -1 it's an imported key and index refers to imported_privkeys
        """
        self.seed = self.read_wallet_file_data(seedarg)
        if extend_mixdepth and len(self.index_cache) > max_mix_depth:
            self.max_mix_depth = len(self.index_cache)
        master = btc.bip32_master_key(self.seed)
        m_0 = btc.bip32_ckd(master, 0)
        mixing_depth_keys = [btc.bip32_ckd(m_0, c)
                             for c in range(self.max_mix_depth)]
        self.keys = [(btc.bip32_ckd(m, 0), btc.bip32_ckd(m, 1))
                     for m in mixing_depth_keys]

        # self.index = [[0, 0]]*max_mix_depth
        self.index = []
        for i in range(self.max_mix_depth):
            self.index.append([0, 0])

    def get_wallet_name(self):
        return 'joinmarket-wallet-' + btc.dbl_sha256(self.keys[0][0])[:6]

    def is_index_ahead_of_cache(self, mix_depth, forchange):
        if mix_depth >= len(self.index_cache):
            return True
        return (self.index[mix_depth][forchange] >=
                self.index_cache[mix_depth][forchange])

    def read_wallet_file_data(self, filename):
        self.path = None
        self.index_cache = [[0, 0]] * self.max_mix_depth
        path = os.path.join('wallets', filename)
        if not os.path.isfile(path):
            if get_network() == 'testnet':
                log.debug('filename interpreted as seed, only available in '
                          'testnet because this probably has lower entropy')
                return filename
            else:
                raise IOError('wallet file not found')
        self.path = path
        with open(path, 'r') as fd:
            walletfile = fd.read()

        walletdata = json.loads(walletfile)
        if walletdata['network'] != get_network():
            system_shutdown('wallet network(%s) does not match '
                            'joinmarket configured network(%s)' % (
                                walletdata['network'], get_network()))

        if 'index_cache' in walletdata:
            self.index_cache = walletdata['index_cache']
        decrypted = False
        while not decrypted:
            password = getpass('Enter wallet decryption passphrase: ')
            password_key = btc.bin_dbl_sha256(password)
            encrypted_seed = walletdata['encrypted_seed']
            try:
                decrypted_seed = decryptData(
                    password_key, encrypted_seed.decode('hex')).encode('hex')
                # there is a small probability of getting a valid PKCS7
                # padding by chance from a wrong password; sanity check the
                # seed length
                if len(decrypted_seed) == 32:
                    decrypted = True
                else:
                    raise ValueError
            except ValueError:
                log.error('Incorrect password')
                decrypted = False
        if self.storepassword:
            # todo: password_key referenced before assignment
            self.password_key = password_key
            self.walletdata = walletdata
        if 'imported_keys' in walletdata:
            for epk_m in walletdata['imported_keys']:
                privkey = decryptData(
                    password_key,
                    epk_m['encrypted_privkey'].decode('hex')).encode('hex')
                privkey = btc.encode_privkey(privkey, 'hex_compressed')
                if epk_m['mixdepth'] not in self.imported_privkeys:
                    self.imported_privkeys[epk_m['mixdepth']] = []
                self.addr_cache[btc.privtoaddr(privkey)] = (
                    epk_m['mixdepth'], -1,
                    len(self.imported_privkeys[epk_m['mixdepth']]))
                self.imported_privkeys[epk_m['mixdepth']].append(privkey)
        # todo: decrpted_seed referened before assignment
        return decrypted_seed

    def update_cache_index(self):
        if not self.path:
            return
        if not os.path.isfile(self.path):
            return
        with open(self.path, 'r') as fd:
            walletfile = fd.read()

        walletdata = json.loads(walletfile)
        walletdata['index_cache'] = self.index
        walletfile = json.dumps(walletdata)
        fd = open(self.path, 'w')
        fd.write(walletfile)
        fd.close()

    def get_key(self, mixing_depth, forchange, i):
        return btc.bip32_extract_key(btc.bip32_ckd(self.keys[mixing_depth][
            forchange], i))

    def get_addr(self, mixing_depth, forchange, i):
        return btc.privtoaddr(self.get_key(mixing_depth, forchange, i))

    def get_new_addr(self, mixing_depth, forchange):
        index = self.index[mixing_depth]
        addr = self.get_addr(mixing_depth, forchange, index[forchange])
        self.addr_cache[addr] = (mixing_depth, forchange, index[forchange])
        index[forchange] += 1
        # self.update_cache_index()
        if isinstance(bc_interface, BitcoinCoreInterface):
            # do not import in the middle of sync_wallet()
            if self.wallet_synced:
                if bc_interface.rpc('getaccount', [addr]) == '':
                    log.debug('importing {addr}', addr=addr)
                    bc_interface.rpc(
                        'importaddress',
                        [addr, self.get_wallet_name(), False])
        return addr

    def get_receive_addr(self, mixing_depth):
        return self.get_new_addr(mixing_depth, False)

    def get_change_addr(self, mixing_depth):
        return self.get_new_addr(mixing_depth, True)

    def get_key_from_addr(self, addr):
        if addr not in self.addr_cache:
            return None
        ac = self.addr_cache[addr]
        if ac[1] >= 0:
            return self.get_key(*ac)
        else:
            return self.imported_privkeys[ac[0]][ac[2]]

    def remove_old_utxos(self, txd):

        removed_utxos = {}
        for ins in txd['ins']:
            utxo = ins['outpoint']['hash'] + ':'
            utxo += str(ins['outpoint']['index'])
            if utxo not in self.unspent:
                continue

            removed_utxos[utxo] = self.unspent[utxo]
            del self.unspent[utxo]

        self.log.debug('removed utxo')
        # print(pprint.pformat(removed_utxos))

        self.spent_utxos += removed_utxos.keys()
        return removed_utxos

    def add_new_utxos(self, tx, txid):
        added_utxos = {}
        for index, outs in enumerate(tx['outs']):
            addr = btc.script_to_address(outs['script'])
            if addr not in self.addr_cache:
                continue
            addrdict = {'address': addr, 'value': outs['value']}
            utxo = txid + ':' + str(index)
            added_utxos[utxo] = addrdict
            self.unspent[utxo] = addrdict
        log.debug('added utxos, wallet:')

        # print(pprint.pformat((self.get_utxos_by_mixdepth()))

        return added_utxos

    def get_utxos_by_mixdepth(self):
        """
        returns a list of utxos sorted by different mix levels
        """
        mix_utxo_list = defaultdict(dict)
        for utxo, addrvalue in self.unspent.iteritems():
            mixdepth = self.addr_cache[addrvalue['address']][0]
            mix_utxo_list[mixdepth][utxo] = addrvalue
        # log.debug('get_utxos_by_mixdepth = \n{}'.format(mix_utxo_list))
        return mix_utxo_list


    def sync_unspent(self):
        st = time.time()
        wallet_name = self.get_wallet_name()
        self.unspent = {}
        unspent_list = bc_interface.rpc('listunspent', [])
        self.log.debug('sync_unspent: {num} returned', num=len(unspent_list))
        for u in unspent_list:
            if 'account' not in u:
                continue
            if u['account'] != wallet_name:
                continue
            if u['address'] not in self.addr_cache:
                continue

            self.unspent[u['txid'] + ':' + str(u['vout'])] = {
                'address': u['address'],
                'value': int(Decimal(str(u['amount'])) * Decimal('1e8'))
            }
        et = time.time()
        self.log.debug('bitcoind sync_unspent took ' + str((et - st)) + 'sec')


    def sync_addresses(self):
        self.log.debug('requesting wallet history')
        wallet_name = self.get_wallet_name()
        addr_req_count = 20
        wallet_addr_list = []
        for mix_depth in range(self.max_mix_depth):
            for forchange in [0, 1]:
                wallet_addr_list += [self.get_new_addr(mix_depth, forchange)
                                     for _ in range(addr_req_count)]
                self.index[mix_depth][forchange] = 0

        # makes more sense to add these in an account called
        # "joinmarket-imported" but its much simpler to add to the same
        # account here

        for privkey_list in self.imported_privkeys.values():
            for privkey in privkey_list:
                imported_addr = btc.privtoaddr(privkey)
                wallet_addr_list.append(imported_addr)
        imported_addr_list = bc_interface.rpc(
                'getaddressesbyaccount', [wallet_name])

        if not set(wallet_addr_list).issubset(set(imported_addr_list)):
            bc_interface.add_watchonly_addresses(wallet_addr_list, wallet_name)
            return

        buf = bc_interface.rpc('listtransactions', [wallet_name, 1000, 0, True])
        txs = buf
        # If the buffer's full, check for more, until it ain't
        while len(buf) == 1000:
            buf = bc_interface.rpc('listtransactions',
                           [wallet_name, 1000, len(txs), True])
            txs += buf
        # TODO check whether used_addr_list can be a set, may be faster (if
        # its a hashset) and allows using issubset() here and setdiff() for
        # finding which addresses need importing

        # TODO also check the fastest way to build up python lists, i suspect
        #  using += is slow
        used_addr_list = [tx['address']
                          for tx in txs if tx['category'] == 'receive']

        too_few_addr_mix_change = []
        for mix_depth in range(self.max_mix_depth):
            for forchange in [0, 1]:
                unused_addr_count = 0
                last_used_addr = ''
                breakloop = False
                while not breakloop:
                    if (unused_addr_count >= self.gaplimit and
                            self.is_index_ahead_of_cache(
                                    mix_depth, forchange)):
                        break
                    mix_change_addrs = [
                        self.get_new_addr(mix_depth, forchange)
                        for _ in range(addr_req_count)
                    ]
                    for mc_addr in mix_change_addrs:
                        if mc_addr not in imported_addr_list:
                            too_few_addr_mix_change.append(
                                    (mix_depth, forchange))

                            breakloop = True
                            break
                        if mc_addr in used_addr_list:
                            last_used_addr = mc_addr
                            unused_addr_count = 0
                        else:
                            unused_addr_count += 1

                if last_used_addr == '':
                    self.index[mix_depth][forchange] = 0
                else:
                    self.index[mix_depth][forchange] = \
                        self.addr_cache[last_used_addr][2] + 1

        wallet_addr_list = []
        if len(too_few_addr_mix_change) > 0:
            log.debug('too few addresses in ' + str(too_few_addr_mix_change))
            for mix_depth, forchange in too_few_addr_mix_change:
                wallet_addr_list += [
                    self.get_new_addr(mix_depth, forchange)
                    for _ in range(addr_req_count * 3)
                ]

            bc_interface.add_watchonly_addresses(wallet_addr_list, wallet_name)
            return

        self.wallet_synced = True


# --------------------------------------------------
# BitcoinCoreWallet
# --------------------------------------------------

class BitcoinCoreWallet(LessAbstractWallet):

    def __init__(self, fromaccount):
        super(BitcoinCoreWallet, self).__init__()
        if not isinstance(bc_interface, BitcoinCoreInterface):
            raise RuntimeError('Bitcoin Core wallet can only be used when '
                               'blockchain interface is BitcoinCoreInterface')
        self.fromaccount = fromaccount
        self.max_mix_depth = 1

    def get_key_from_addr(self, addr):
        self.ensure_wallet_unlocked()
        return bc_interface.rpc('dumpprivkey', [addr])

    def get_utxos_by_mixdepth(self):
        unspent_list = bc_interface.rpc('listunspent', [])
        result = {0: {}}
        for u in unspent_list:
            if not u['spendable']:
                continue
            if self.fromaccount and (('account' not in u) or
                                     u['account'] != self.fromaccount):
                continue
            result[0][u['txid'] + ':' + str(u['vout'])] = {
                'address': u['address'],
                'value': int(Decimal(str(u['amount'])) * Decimal('1e8'))
            }
        return result

    def get_change_addr(self, mixing_depth):
        return bc_interface.rpc('getrawchangeaddress', [])

    @staticmethod
    def ensure_wallet_unlocked():
        wallet_info = bc_interface.rpc('getwalletinfo', [])
        if 'unlocked_until' in wallet_info and wallet_info[
                'unlocked_until'] <= 0:
            while True:
                password = getpass('Enter passphrase to unlock wallet: ')
                if password == '':
                    raise RuntimeError('Aborting wallet unlock')
                try:
                    # TODO cleanly unlock wallet after use - arbitrary timeout
                    bc_interface.rpc('walletpassphrase', [password, 10])
                    break
                except JsonRpcError as exc:
                    if exc.code != -14:
                        raise exc
                        # Wrong passphrase, try again.

def select_gradual(unspent, value):
    """
    UTXO selection algorithm for gradual dust reduction
    If possible, combines outputs, picking as few as possible of the largest
    utxos less than the target value; if the target value is larger than the
    sum of all smaller utxos, uses the smallest utxo larger than the value.
    """
    value, key = int(value), lambda u: u["value"]
    high = sorted([u for u in unspent if key(u) >= value], key=key)
    low = sorted([u for u in unspent if key(u) < value], key=key)
    lowsum = reduce(lambda x, y: x + y, map(key, low), 0)
    if value > lowsum:
        if len(high) == 0:
            raise Exception('Not enough funds')
        else:
            return [high[0]]
    else:
        start, end, total = 0, 0, 0
        while total < value:
            total += low[end]['value']
            end += 1
        while total >= value + low[start]['value']:
            total -= low[start]['value']
            start += 1
        return low[start:end]


def select_greedy(unspent, value):
    """
    UTXO selection algorithm for greedy dust reduction, but leaves out
    extraneous utxos, preferring to keep multiple small ones.
    """
    value, key, cursor = int(value), lambda u: u['value'], 0
    utxos, picked = sorted(unspent, key=key), []
    for utxo in utxos:  # find the smallest consecutive sum >= value
        value -= key(utxo)
        if value == 0:  # perfect match! (skip dilution stage)
            return utxos[0:cursor + 1]  # end is non-inclusive
        elif value < 0:  # overshot
            picked += [utxo]  # definitely need this utxo
            break  # proceed to dilution
        cursor += 1
    for utxo in utxos[cursor - 1::-1]:  # dilution loop
        value += key(utxo)  # see if we can skip this one
        if value > 0:  # no, that drops us below the target
            picked += [utxo]  # so we need this one too
            value -= key(utxo)  # 'backtrack' the counter
    if len(picked) > 0:
        return picked
    raise Exception('Not enough funds')  # if all else fails, we do too


def select_greediest(unspent, value):
    """
    UTXO selection algorithm for speediest dust reduction
    Combines the shortest run of utxos (sorted by size, from smallest) which
    exceeds the target value; if the target value is larger than the sum of
    all smaller utxos, uses the smallest utxo larger than the target value.
    """
    value, key = int(value), lambda u: u["value"]
    high = sorted([u for u in unspent if key(u) >= value], key=key)
    low = sorted([u for u in unspent if key(u) < value], key=key)
    lowsum = reduce(lambda x, y: x + y, map(key, low), 0)
    if value > lowsum:
        if len(high) == 0:
            raise Exception('Not enough funds')
        else:
            return [high[0]]
    else:
        end, total = 0, 0
        while total < value:
            total += low[end]['value']
            end += 1
        return low[0:end]




__all__ = ('Wallet', 'BitcoinCoreWallet')
