#A wrapper for public key
#authenticated encryption
#using Diffie Hellman key
#exchange to set up a 
#symmetric encryption.

import libnacl.public
import binascii


def init_keypair(fname=None):
    '''Create a new encryption 
    keypair; stored in file fname
    if provided. The keypair object
    is returned.
    '''
    kp = libnacl.public.SecretKey()
    if fname:
        #Note: handles correct file permissions
        kp.save(fname)
    return kp


#the next two functions are useful 
#for exchaging pubkeys with counterparty
def get_pubkey(kp, as_hex=False):
    '''Given a keypair object,
    return its public key, 
    optionally in hex.'''
    return kp.hex_pk() if as_hex else kp.pk


def init_pubkey(hexpk, fname=None):
    '''Create a pubkey object from a
    hex formatted string.
    Save to file fname if specified.
    '''
    pk = libnacl.public.PublicKey(binascii.unhexlify(hexpk))
    if fname:
        pk.save(fname)
    return pk


def as_init_encryption(kp, c_pk):
    '''Given an initialised
    keypair kp and a counterparty
    pubkey c_pk, create a Box 
    ready for encryption/decryption.
    '''
    return libnacl.public.Box(kp.sk, c_pk)


'''
After initialisation, it's possible
to use the box object returned from
as_init_encryption to directly change
from plaintext to ciphertext:
    ciphertext = box.encrypt(plaintext)
    plaintext = box.decrypt(ciphertext)
Notes:
 1. use binary format for ctext/ptext
 2. Nonce is handled at the implementation layer.
'''

#TODO: Sign, verify. At the moment we are using
#bitcoin signatures so it isn't necessary.


def test_case(case_name,
              alice_box,
              bob_box,
              ab_message,
              ba_message,
              num_iterations=1):
    for i in range(num_iterations):
        otw_amsg = alice_box.encrypt(ab_message)
        bob_ptext = bob_box.decrypt(otw_amsg)
        assert bob_ptext == ab_message, "Encryption test: FAILED. Alice sent: "\
               +ab_message+" , Bob received: " + bob_ptext

        otw_bmsg = bob_box.encrypt(ba_message)
        alice_ptext = alice_box.decrypt(otw_bmsg)
        assert alice_ptext == ba_message, "Encryption test: FAILED. Bob sent: "\
               +ba_message+" , Alice received: " + alice_ptext

    print "Encryption test PASSED for case: " + case_name

#to test the encryption functionality
if __name__ == "__main__":
    alice_kp = init_keypair()
    bob_kp = init_keypair()

    #this is the DH key exchange part
    bob_otwpk = get_pubkey(bob_kp, True)
    alice_otwpk = get_pubkey(alice_kp, True)

    bob_pk = init_pubkey(bob_otwpk)
    alice_box = as_init_encryption(alice_kp, bob_pk)
    alice_pk = init_pubkey(alice_otwpk)
    bob_box = as_init_encryption(bob_kp, alice_pk)

    #now Alice and Bob can use their 'box'
    #constructs (both of which utilise the same
    #shared secret) to perform encryption/decryption

    test_case("short ascii", alice_box, bob_box, "Attack at dawn",
              "Not tonight Josephine!", 5)

    import base64, string, random
    longb641 = base64.b64encode(''.join(random.choice(string.ascii_letters) for
                                        x in range(5000)))
    longb642 = base64.b64encode(''.join(random.choice(string.ascii_letters) for
                                        x in range(5000)))
    test_case("long b64", alice_box, bob_box, longb641, longb642, 5)

    print "All test cases passed - encryption and decryption should work correctly."
