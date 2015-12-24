from __future__ import absolute_import, print_function

import io
import sys

from ConfigParser import SafeConfigParser, NoOptionError
from twisted.logger import Logger, textFileLogObserver, globalLogPublisher

globalLogPublisher.addObserver(
        textFileLogObserver(sys.stdout, timeFormat='%Y-%m-%d %H:%M:%S.%f'))

# noinspection PyTypeChecker
# globalLogPublisher.addObserver(
#         jsonFileLogObserver(io.open("logs/log.json", "a")))


log = Logger()
log.info('log started')


defaultconfig = \
    """
[BLOCKCHAIN]
blockchain_source = blockr
#options: blockr, bitcoin-rpc, json-rpc, regtest
# for instructions on bitcoin-rpc read
# https://github.com/chris-belcher/joinmarket/wiki/Running-JoinMarket-with-Bitcoin-Core-full-node
network = mainnet
rpc_host = localhost
rpc_port = 8332
rpc_user = bitcoin
rpc_password = password

[MESSAGING]
host = irc.cyberguerrilla.org
channel = joinmarket-pit
port = 6697
usessl = true
socks5 = false
socks5_host = localhost
socks5_port = 9050
#for tor
#host = 6dvj6v5imhny3anf.onion
#port = 6697
#usessl = true
#socks5 = true
maker_timeout_sec = 30

[POLICY]
# for dust sweeping, try merge_algorithm = gradual
# for more rapid dust sweeping, try merge_algorithm = greedy
# for most rapid dust sweeping, try merge_algorithm = greediest
# but don't forget to bump your miner fees!
merge_algorithm = default
"""

config = SafeConfigParser()
config_location = 'joinmarket.cfg'

# FIXME: Add rpc_* options here in the future!
required_options = {'BLOCKCHAIN': ['blockchain_source', 'network'],
                    'MESSAGING': ['host', 'channel', 'port']}

loadedFiles = config.read([config_location])
# Create default config file if not found
if len(loadedFiles) != 1:
    config.readfp(io.BytesIO(defaultconfig))
    with open(config_location, "w") as configfile:
        configfile.write(defaultconfig)

# check for sections
for s in required_options:
    if s not in config.sections():
        raise Exception("Config file does not contain "
                        "the required section: " + s)
# then check for specific options
for k, v in required_options.iteritems():
    for o in v:
        if o not in config.options(k):
            raise Exception("Config file does not contain "
                            "the required option: " + o)

try:
    maker_timeout_sec = config.getint('MESSAGING', 'maker_timeout_sec')
except NoOptionError:
    log.debug('maker_timeout_sec not found in .cfg file, '
              'using default value')


def get_config_irc_channel():
    channel = '#' + config.get("MESSAGING", "channel")
    # todo: this isn't right, at least from the testing I was doing
    # if get_network() == 'testnet':
    #     channel += '-test'
    return channel


def get_network():
    """Returns network name"""
    return config.get("BLOCKCHAIN", "network")


def get_p2sh_vbyte():
    if get_network() == 'testnet':
        return 0xc4
    else:
        return 0x05


def get_p2pk_vbyte():
    if get_network() == 'testnet':
        return 0x6f
    else:
        return 0x00


def validate_address(addr):
    from . import jmbtc as btc
    try:
        ver = btc.get_version_byte(addr)
    except AssertionError:
        return False, 'Checksum wrong. Typo in address?'
    if ver != get_p2pk_vbyte() and ver != get_p2sh_vbyte():
        return False, 'Wrong address version. Testnet/mainnet confused?'
    return True, 'address validated'

# todo: this should be in config
DUST_THRESHOLD = 2730

JM_VERSION = 2

__all__ = ('config', 'get_network', 'maker_timeout_sec',
           'get_config_irc_channel', 'validate_address',
           'DUST_THRESHOLD')
