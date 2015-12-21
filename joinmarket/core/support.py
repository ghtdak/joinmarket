from __future__ import absolute_import, print_function

import io
import signal

"""
Random functions - replacing some NumPy features
NOTE THESE ARE NEITHER CRYPTOGRAPHICALLY SECURE
NOR PERFORMANT NOR HIGH PRECISION!
Only for sampling purposes
"""

import pprint
import random
import traceback

from decimal import Decimal

from twisted.internet import defer, reactor
from twisted.logger import Logger, eventsFromJSONLogFile

log = Logger()


def signal_shutdown_handler(*args, **kwargs):
    log.debug('keyboard interrupt')
    reactor.stop()
    # sys.exit(-1)


def keyboard_signal_handler():
    signal.signal(signal.SIGINT, signal_shutdown_handler)

# todo: this might be risky. but control-c is kinda critical

reactor.callWhenRunning(keyboard_signal_handler)

# observer = twisted_log.PythonLoggingObserver()
# observer.start()

# log.startLogging(sys.stdout)

# todo: I'm not sure I understand exactly why this is or isn't needed
# logging.getLogger('twisted').addHandler(logging.NullHandler())
#
# logFormatter = logging.Formatter(
#     "%(asctime)s [%(threadName)-12.12s] [%(levelname)-5.5s]  %(message)s")
# log = logging.getLogger('joinmarket')
# log.setLevel(logging.DEBUG)
#
# consoleHandler = logging.StreamHandler(stream=sys.stdout)
# consoleHandler.setFormatter(logFormatter)
# log.addHandler(consoleHandler)

log.info('logger started')


# def get_log():
#     """
#     provides joinmarket logging instance
#     :return: log instance
#     """
#     return log

def loggerMath():
    """
    Found this sitting around in the Twisted Howto module.  Very interesting
    :return:
    """
    for event in eventsFromJSONLogFile(io.open("logs/log.json")):
        print(sum(event["values"]))


def system_shutdown(errno, reason='none given'):

    if errno:
        log.error('Unhappy Shutdown: {errno} {reason}',
                  errno=errno, reason=reason)
        traceback.print_stack()
    else:
        traceback.print_stack()
        log.info('Normal Shutdown')

    reactor.stop()


def sleepGenerator(seconds):
    """
    Mimics sleeping when using Twisted's inlineCallbacks
    https://twistedmatrix.com/pipermail/twisted-python/2009-October/020788.html
    :param seconds:
    :return:
    """
    d = defer.Deferred()
    reactor.callLater(seconds, d.callback, seconds)
    return d


def rand_norm_array(mu, sigma, n):
    # use normalvariate instead of gauss for thread safety
    return [random.normalvariate(mu, sigma) for _ in range(n)]


def rand_exp_array(lamda, n):
    # 'lambda' is reserved (in case you are triggered by spelling errors)
    return [random.expovariate(1.0 / lamda) for _ in range(n)]


def rand_pow_array(power, n):
    # rather crude in that uses a uniform sample which is a multiple of 1e-4
    # for basis of formula, see: http://mathworld.wolfram.com/RandomNumber.html
    return [y**(1.0 / power)
            for y in [x * 0.0001 for x in random.sample(
                xrange(10000), n)]]


# End random functions


def chunks(d, n):
    return [d[x:x + n] for x in xrange(0, len(d), n)]


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


def calc_cj_fee(ordertype, cjfee, cj_amount):
    if ordertype == 'absorder':
        real_cjfee = int(cjfee)
    elif ordertype == 'relorder':
        real_cjfee = int((Decimal(cjfee) * Decimal(cj_amount)).quantize(Decimal(
            1)))
    else:
        raise RuntimeError('unknown order type: ' + str(ordertype))
    return real_cjfee


def debug_dump_object(obj, skip_fields=None):
    if skip_fields is None:
        skip_fields = []
    log.debug('Class debug dump, name:' + obj.__class__.__name__)
    for k, v in obj.__dict__.iteritems():
        if k in skip_fields:
            continue
        if k == 'password' or k == 'given_password':
            continue
        log.debug('key=' + k)
        if isinstance(v, str):
            log.debug('string: len:' + str(len(v)))
            log.debug(v)
        elif isinstance(v, dict) or isinstance(v, list):
            log.debug(pprint.pformat(v))
        else:
            log.debug(str(v))

__all__ = ('calc_cj_fee', 'debug_dump_object', 'chunks', 'sleepGenerator',
           'rand_norm_array', 'rand_pow_array', 'rand_exp_array',
           'system_shutdown')
