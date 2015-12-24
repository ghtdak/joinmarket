from __future__ import absolute_import, print_function

import bitcoin as btc

from .configure import get_p2pk_vbyte

def privtoaddr(privkey):
    return btc.privtoaddr(privkey, get_p2pk_vbyte())


def privkey_to_address(privkey):
    return privtoaddr(privkey)


def encode_privkey(priv, formt):
    return btc.encode_privkey(priv, formt, get_p2pk_vbyte())


def script_to_address(script):
    return btc.script_to_address(script, get_p2pk_vbyte())


def pubkey_to_address(pubkey):
    return btc.pubkey_to_address(pubkey, get_p2pk_vbyte())


def pubtoaddr(pubkey):
    """
    bitcoin.main has this redefined... we're doing the verbose thing
    :param pubkey:
    :return:
    """
    return pubkey_to_address(pubkey)

serialize = btc.serialize

sign = btc.sign

sha256 = btc.sha256

dbl_sha256 = btc.dbl_sha256

bin_dbl_sha256 = btc.bin_dbl_sha256

get_privkey_format = btc.get_privkey_format

deserialize = btc.deserialize

blockr_pushtx = btc.blockr_pushtx

make_request = btc.make_request

safe_hexlify = btc.safe_hexlify

txhash = btc.txhash

get_version_byte = btc.get_version_byte

ecdsa_verify = btc.ecdsa_verify

privtopub = btc.privtopub

ecdsa_sign = btc.ecdsa_sign

sign = btc.sign

mktx = btc.mktx

verify_tx_input = btc.verify_tx_input

deserialize_script = btc.deserialize_script

SIGHASH_ALL = btc.SIGHASH_ALL

privkey_to_pubkey = btc.privkey_to_pubkey

signature_form = btc.signature_form

bin_txhash = btc.bin_txhash

hash_to_int = btc.hash_to_int

fast_multiply = btc.fast_multiply

inv = btc.inv

decode_privkey = btc.decode_privkey

der_encode_sig = btc.der_encode_sig

encode = btc.encode

serialize_script = btc.serialize_script

deterministic_generate_k = btc.deterministic_generate_k

multiply = btc.multiply

add_pubkeys = btc.add_pubkeys

select = btc.select

bip32_master_key = btc.bip32_master_key

bip32_ckd = btc.bip32_ckd

bip32_extract_key = btc.bip32_extract_key

json_changebase = btc.json_changebase

mk_pubkey_script = btc.mk_pubkey_script

G = btc.G

N = btc.N

# don't need __all__ here cuz everything is meant for export
