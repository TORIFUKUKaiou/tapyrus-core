#!/usr/bin/env python3
# Copyright (c) 2016-2018 The Bitcoin Core developers
# Copyright (c) 2019 Chaintope Inc.
# Distributed under the MIT software license, see the accompanying
# file COPYING or http://www.opensource.org/licenses/mit-license.php.
"""Test segwit transactions and blocks on P2P network."""
from binascii import hexlify
import math
import random
import struct
import time

from test_framework.blocktools import create_block, create_coinbase, add_witness_commitment, get_witness_script, WITNESS_COMMITMENT_HEADER, createTestGenesisBlock
from test_framework.key import CECKey, CPubKey
from test_framework.messages import (
    BIP125_SEQUENCE_NUMBER,
    CBlock,
    CBlockHeader,
    CInv,
    COutPoint,
    CTransaction,
    CTxIn,
    CTxInWitness,
    CTxOut,
    CTxWitness,
    MAX_BLOCK_BASE_SIZE,
    NODE_NETWORK,
    NODE_WITNESS,
    msg_block,
    msg_getdata,
    msg_headers,
    msg_inv,
    msg_tx,
    msg_witness_block,
    msg_witness_tx,
    ser_uint256,
    ser_vector,
    sha256,
    uint256_from_str,
    ser_string_vector,
    ser_compact_size
)
from test_framework.mininode import (
    P2PInterface,
    mininode_lock,
    wait_until,
)
from test_framework.script import (
    CScript,
    CScriptNum,
    CScriptOp,
    MAX_SCRIPT_ELEMENT_SIZE,
    OP_0,
    OP_1,
    OP_16,
    OP_2DROP,
    OP_CHECKMULTISIG,
    OP_CHECKSIG,
    OP_DROP,
    OP_DUP,
    OP_ELSE,
    OP_ENDIF,
    OP_EQUAL,
    OP_EQUALVERIFY,
    OP_HASH160,
    OP_IF,
    OP_RETURN,
    OP_TRUE,
    SIGHASH_ALL,
    SIGHASH_ANYONECANPAY,
    SIGHASH_NONE,
    SIGHASH_SINGLE,
    SegwitVersion1SignatureHash,
    SignatureHash,
    hash160,
)
from test_framework.test_framework import BitcoinTestFramework
from test_framework.util import (
    assert_equal,
    bytes_to_hex_str,
    connect_nodes,
    disconnect_nodes,
    get_bip9_status,
    hex_str_to_bytes,
    sync_blocks,
    sync_mempools,
    assert_raises_rpc_error
)

# The versionbit bit used to signal activation of SegWit
VB_WITNESS_BIT = 1
VB_PERIOD = 144
VB_TOP_BITS = 0x20000000

MAX_SIGOP_COST = 80000

class UTXO():
    """Used to keep track of anyone-can-spend outputs that we can use in the tests."""
    def __init__(self, sha256, n, value):
        self.sha256 = sha256
        self.n = n
        self.nValue = value

def get_p2pkh_script(pubkeyhash):
    """Get the script associated with a P2PKH."""
    return CScript([CScriptOp(OP_DUP), CScriptOp(OP_HASH160), pubkeyhash, CScriptOp(OP_EQUALVERIFY), CScriptOp(OP_CHECKSIG)])

def sign_p2pk_witness_input(script, tx_to, in_idx, hashtype, value, key):
    """Add signature for a P2PK witness program."""
    tx_hash = SegwitVersion1SignatureHash(script, tx_to, in_idx, hashtype, value)
    signature = key.sign(tx_hash) + chr(hashtype).encode('latin-1')
    tx_to.wit.vtxinwit[in_idx].scriptWitness.stack = [signature, script]
    tx_to.rehash()

def get_virtual_size(witness_block):
    """Calculate the virtual size of a witness block.

    Virtual size is base + witness/4."""
    base_size = len(witness_block.serialize(with_witness=False))
    total_size = len(witness_block.serialize(with_witness=True))
    # the "+3" is so we round up
    vsize = int((3 * base_size + total_size + 3) / 4)
    return vsize

def test_transaction_acceptance(rpc, p2p, tx, with_witness, accepted, reason=None):
    """Send a transaction to the node and check that it's accepted to the mempool

    - Submit the transaction over the p2p interface
    - use the getrawmempool rpc to check for acceptance."""
    tx_message = msg_tx(tx)
    if with_witness:
        tx_message = msg_witness_tx(tx)
    p2p.send_message(tx_message)
    p2p.sync_with_ping()
    assert_equal(tx.hashMalFix in rpc.getrawmempool(), accepted)
    if (reason is not None and not accepted):
        # Check the rejection reason as well.
        with mininode_lock:
            assert_equal(p2p.last_message["reject"].reason, reason)

def test_witness_block(rpc, p2p, block, with_witness, accepted, reason=None):
    """Send a block to the node and check that it's accepted

    - Submit the block over the p2p interface
    - use the getbestblockhash rpc to check for acceptance."""
    if with_witness:
        p2p.send_message(msg_witness_block(block))
    else:
        p2p.send_message(msg_block(block))
    p2p.sync_with_ping()
    assert_equal(rpc.getbestblockhash() == block.hash, accepted)
    if (reason is not None and not accepted):
        # Check the rejection reason as well.
        with mininode_lock:
            assert_equal(p2p.last_message["reject"].reason, reason)

class TestP2PConn(P2PInterface):
    def __init__(self):
        super().__init__()
        self.getdataset = set()

    def on_getdata(self, message):
        for inv in message.inv:
            self.getdataset.add(inv.hash)

    def announce_tx_and_wait_for_getdata(self, tx, timeout=60, success=True):
        with mininode_lock:
            self.last_message.pop("getdata", None)
        self.send_message(msg_inv(inv=[CInv(1, tx.malfixsha256)]))
        if success:
            self.wait_for_getdata(timeout)
        else:
            time.sleep(timeout)
            assert not self.last_message.get("getdata")

    def announce_block_and_wait_for_getdata(self, block, use_header, timeout=60):
        with mininode_lock:
            self.last_message.pop("getdata", None)
            self.last_message.pop("getheaders", None)
        msg = msg_headers()
        msg.headers = [CBlockHeader(block)]
        if use_header:
            self.send_message(msg)
        else:
            self.send_message(msg_inv(inv=[CInv(2, block.sha256)]))
            self.wait_for_getheaders()
            self.send_message(msg)
        self.wait_for_getdata()

    def request_block(self, blockhash, inv_type, timeout=60):
        with mininode_lock:
            self.last_message.pop("block", None)
        self.send_message(msg_getdata(inv=[CInv(inv_type, blockhash)]))
        self.wait_for_block(blockhash, timeout)
        return self.last_message["block"].block

class SegWitTest(BitcoinTestFramework):
    def set_test_params(self):
        self.setup_clean_chain = True
        self.num_nodes = 3
        # This test tests SegWit both pre and post-activation, so use the normal BIP9 activation.
        self.extra_args = [["-whitelist=127.0.0.1"], ["-whitelist=127.0.0.1", "-acceptnonstdtxn=0"], ["-whitelist=127.0.0.1"]]
        #this is needed as some tests based on block length are optimized based on proof length for one signature.
        self.signblockthreshold = 1
        self.signblockpubkeys = "0201c537fd7eb7928700927b48e51ceec621fc8ba1177ee2ad67336ed91e2f63a1"
        self.signblockprivkeys = ["aa3680d5d48a8283413f7a108367c7299ca73f553735860a87b08f39395618b7"]
        self.genesisBlock = createTestGenesisBlock(self.signblockpubkeys, self.signblockthreshold, self.signblockprivkeys, int(time.time() - 100))

    def setup_network(self):
        self.setup_nodes()
        connect_nodes(self.nodes[0], 1)
        connect_nodes(self.nodes[0], 2)
        self.sync_all()

    # Helper functions

    def build_next_block(self, version=4):
        """Build a block on top of node0's tip."""
        tip = self.nodes[0].getbestblockhash()
        height = self.nodes[0].getblockcount() + 1
        block_time = self.nodes[0].getblockheader(tip)["mediantime"] + 1
        block = create_block(int(tip, 16), create_coinbase(height), block_time)
        block.version = version
        block.rehash()
        return block

    def update_witness_block_with_transactions(self, block, tx_list, with_witness=True, nonce=0):
        """Add list of transactions to block, adds witness commitment, then solves."""
        block.vtx.extend(tx_list)
        if with_witness:
            add_witness_commitment(block, nonce)
        else:
            block.hashMerkleRoot = block.calc_merkle_root()
            block.hashImMerkleRoot = block.calc_immutable_merkle_root()
        block.solve(self.signblockprivkeys)

    def run_test(self):
        # Setup the p2p connections
        # self.test_node sets NODE_WITNESS|NODE_NETWORK
        self.test_node = self.nodes[0].add_p2p_connection(TestP2PConn(), services=NODE_NETWORK | NODE_WITNESS, wait_for_verack = False)
        self.test_node.wait_for_disconnect(timeout=10)
        self.test_node = self.nodes[0].add_p2p_connection(TestP2PConn(), services=NODE_NETWORK)

        # self.old_node sets only NODE_NETWORK
        self.old_node = self.nodes[0].add_p2p_connection(TestP2PConn(), services=NODE_NETWORK)
        # self.std_node is for testing node1 (fRequireStandard=true)
        self.std_node = self.nodes[1].add_p2p_connection(TestP2PConn(), services=NODE_NETWORK)

        assert self.test_node.nServices & NODE_WITNESS == 0
        assert self.old_node.nServices & NODE_WITNESS == 0
        assert self.std_node.nServices & NODE_WITNESS == 0

        # Keep a place to store utxo's that can be used in later tests
        self.utxo = []

        # Segwit status 'defined'

        self.test_non_witness_transaction()
        self.test_unnecessary_witness_before_segwit_activation()
        self.test_v0_outputs_are_spendable()
        self.test_block_relay()

        # Segwit status 'started'

        self.test_getblocktemplate_before_lockin()

        # Segwit status 'locked_in'

        self.test_unnecessary_witness_before_segwit_activation()
        self.test_witness_tx_relay_before_segwit_activation()
        self.test_block_relay()
        self.test_standardness_v0()

        # Segwit status 'active'

        self.test_p2sh_witness()
        self.test_witness_commitments()
        self.test_block_malleability()
        self.test_witness_block_size()
        self.test_submit_block()
        self.test_extra_witness_data()
        self.test_max_witness_push_length()
        self.test_max_witness_program_length()
        self.test_witness_input_length()
        self.test_block_relay()
        self.test_tx_relay_after_segwit_activation()
        self.test_standardness_v0()
        self.test_segwit_versions()
        self.test_premature_coinbase_witness_spend()
        self.test_uncompressed_pubkey()
        #self.test_signature_version_1()
        self.test_non_standard_witness_blinding()
        self.test_non_standard_witness()
        self.test_upgrade_after_activation()
        self.test_witness_sigops()

    # Individual tests

    def subtest(func):  # noqa: N805
        """Wraps the subtests for logging and state assertions."""
        def func_wrapper(self, *args, **kwargs):
            self.log.info("Subtest: {} ".format(func.__name__))
            func(self, *args, **kwargs)
            # Each subtest should leave some utxos for the next subtest
            assert self.utxo
            sync_blocks(self.nodes)

        return func_wrapper

    @subtest
    def test_non_witness_transaction(self):
        """See if sending a regular transaction works, and create a utxo to use in later tests."""
        # Mine a block with an anyone-can-spend coinbase,
        # let it mature, then try to spend it.

        block = self.build_next_block(version=1)
        block.solve(self.signblockprivkeys)
        self.test_node.send_message(msg_block(block))
        self.test_node.sync_with_ping()  # make sure the block was processed
        txid = block.vtx[0].malfixsha256

        self.nodes[0].generate(99, self.signblockprivkeys)  # let the block mature

        # Create a transaction that spends the coinbase
        tx = CTransaction()
        tx.vin.append(CTxIn(COutPoint(txid, 0), b""))
        tx.vout.append(CTxOut(49 * 100000000, CScript([OP_TRUE, OP_DROP] * 15 + [OP_TRUE])))
        tx.calc_sha256()

        # Check that serializing it with or without witness is the same
        # This is a sanity check of our testing framework.
        assert_equal(msg_tx(tx).serialize(), msg_witness_tx(tx).serialize())

        self.test_node.send_message(msg_witness_tx(tx))
        self.test_node.sync_with_ping()  # make sure the tx was processed
        assert(tx.hashMalFix in self.nodes[0].getrawmempool())
        # Save this transaction for later
        self.utxo.append(UTXO(tx.malfixsha256, 0, 49 * 100000000))
        self.nodes[0].generate(1, self.signblockprivkeys)

    @subtest
    def test_unnecessary_witness_before_segwit_activation(self):
        """Verify that blocks with witnesses are rejected before activation."""

        tx = CTransaction()
        tx.vin.append(CTxIn(COutPoint(self.utxo[0].sha256, self.utxo[0].n), b""))
        tx.vout.append(CTxOut(self.utxo[0].nValue - 1000, CScript([OP_TRUE])))
        tx.wit.vtxinwit.append(CTxInWitness())
        tx.wit.vtxinwit[0].scriptWitness.stack = [CScript([CScriptNum(1)])]

        # Verify the hash with witness differs from the txid
        # (otherwise our testing framework must be broken!)
        tx.rehash()
        assert(tx.malfixsha256 != tx.calc_sha256(with_witness=True))

        # Construct a segwit-signaling block that includes the transaction.
        block = self.build_next_block(version=(VB_TOP_BITS | (1 << VB_WITNESS_BIT)))
        self.update_witness_block_with_transactions(block, [tx])
        # Sending witness data before activation is not allowed (anti-spam
        # rule).
        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=True, accepted=False, reason=b'bad-txnmrklroot')
        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=False, accepted=True)

        #wait_until(lambda: 'reject' in self.test_node.last_message and self.test_node.last_message["reject"].reason ==  b"unexpected-witness")

        # But it should not be permanently marked bad...
        # Resend without witness information.
        self.test_node.send_message(msg_block(block))
        self.test_node.sync_with_ping()
        assert_equal(self.nodes[0].getbestblockhash(), block.hash)

        # Update our utxo list; we spent the first entry.
        self.utxo.pop(0)
        self.utxo.append(UTXO(tx.malfixsha256, 0, tx.vout[0].nValue))

    @subtest
    def test_block_relay(self):
        """Test that block requests do not carry MSG_WITNESS_FLAG.

        This is true regardless of segwit activation.
        Also test that we don't ask for blocks from unupgraded peers."""

        blocktype = 2

        # test_node has set NODE_WITNESS, so all getdata requests should be for
        # witness blocks.
        # Test announcing a block via inv results in a getdata, and that
        # announcing a version 4 or random VB block with a header results in a getdata
        block1 = self.build_next_block()
        block1.solve(self.signblockprivkeys)

        self.test_node.announce_block_and_wait_for_getdata(block1, use_header=False)
        assert(self.test_node.last_message["getdata"].inv[0].type == blocktype)
        test_witness_block(self.nodes[0].rpc, self.test_node, block1, with_witness=True, accepted=True)

        block2 = self.build_next_block(version=4)
        block2.solve(self.signblockprivkeys)

        self.test_node.announce_block_and_wait_for_getdata(block2, use_header=True)
        assert(self.test_node.last_message["getdata"].inv[0].type == blocktype)
        test_witness_block(self.nodes[0].rpc, self.test_node, block2, with_witness=True, accepted=True)

        block3 = self.build_next_block(version=(VB_TOP_BITS | (1 << 15)))
        block3.solve(self.signblockprivkeys)
        self.test_node.announce_block_and_wait_for_getdata(block3, use_header=True)
        assert(self.test_node.last_message["getdata"].inv[0].type == blocktype)
        test_witness_block(self.nodes[0].rpc, self.test_node, block3, with_witness=True, accepted=True)

        # Check that we can getdata for witness blocks or regular blocks,
        # and the right thing happens.
        # Before activation, we should be able to request old blocks with
        # or without witness, and they should be the same.
        chain_height = self.nodes[0].getblockcount()
        # Pick 10 random blocks on main chain, and verify that getdata's
        # for MSG_BLOCK, MSG_WITNESS_BLOCK, and rpc getblock() are equal.
        all_heights = list(range(chain_height + 1))
        random.shuffle(all_heights)
        all_heights = all_heights[0:10]
        for height in all_heights:
            block_hash = self.nodes[0].getblockhash(height)
            rpc_block = self.nodes[0].getblock(block_hash, False)
            block_hash = int(block_hash, 16)
            block = self.test_node.request_block(block_hash, 2)
            wit_block = self.test_node.request_block(block_hash, 2)
            assert_equal(block.serialize(with_witness=True), wit_block.serialize(with_witness=True))
            assert_equal(block.serialize(), hex_str_to_bytes(rpc_block))


    @subtest
    def test_v0_outputs_are_spendable(self):
        """Test that v0 outputs are spendable before segwit activation.

        ~6 months after segwit activation, the SCRIPT_VERIFY_WITNESS flag was
        backdated so that it applies to all blocks, going back to the genesis
        block.

        Consequently, version 0 witness outputs are never spendable without
        witness, and so can't be spent before segwit activation (the point at which
        blocks are permitted to contain witnesses)."""

        # node2 doesn't need to be connected for this test.
        # (If it's connected, node0 may propogate an invalid block to it over
        # compact blocks and the nodes would have inconsistent tips.)
        disconnect_nodes(self.nodes[0], 2)

        # Create two outputs, a p2wsh and p2sh-p2wsh
        witness_program = CScript([OP_TRUE])
        witness_hash = sha256(witness_program)
        script_pubkey = CScript([OP_0, witness_hash])

        p2sh_pubkey = hash160(script_pubkey)
        p2sh_script_pubkey = CScript([OP_HASH160, p2sh_pubkey, OP_EQUAL])

        value = self.utxo[0].nValue // 3

        tx = CTransaction()
        tx.vin = [CTxIn(COutPoint(self.utxo[0].sha256, self.utxo[0].n), b'')]
        tx.vout = [CTxOut(value, script_pubkey), CTxOut(value, p2sh_script_pubkey)]
        tx.vout.append(CTxOut(value, CScript([OP_TRUE])))
        tx.rehash()
        txid = tx.malfixsha256

        # Add it to a block
        block = self.build_next_block()
        self.update_witness_block_with_transactions(block, [tx])
        # Now send the block without witness. It should be accepted
        test_witness_block(self.nodes[0], self.test_node, block, with_witness=True, accepted=False, reason=b'bad-txnmrklroot')
        test_witness_block(self.nodes[0], self.test_node, block, with_witness=False, accepted=True)

        # Now try to spend the outputs. This should fail since SCRIPT_VERIFY_WITNESS is always enabled.
        p2wsh_tx = CTransaction()
        p2wsh_tx.vin = [CTxIn(COutPoint(txid, 0), b'')]
        p2wsh_tx.vout = [CTxOut(value, CScript([OP_TRUE]))]
        p2wsh_tx.wit.vtxinwit.append(CTxInWitness())
        p2wsh_tx.wit.vtxinwit[0].scriptWitness.stack = [CScript([OP_TRUE])]
        p2wsh_tx.rehash()

        block = self.build_next_block()
        #block with witness commitment
        self.update_witness_block_with_transactions(block, [p2wsh_tx], with_witness=True)

        # When the block is serialized without witness, validation fails because the transaction is
        # invalid 
        # Note: The reject reason for this failure could be
        # 'block-validation-failed' (if script check threads > 1) or
        # 'non-mandatory-script-verify-flag (Witness program was passed an
        # empty witness)' (otherwise).
        # TODO: support multiple acceptable reject reasons.
        test_witness_block(self.nodes[0], self.test_node, block, with_witness=True, accepted=False)
        test_witness_block(self.nodes[0], self.test_node, block, with_witness=False, accepted=True)

        p2sh_p2wsh_tx = CTransaction()
        p2sh_p2wsh_tx.vin = [CTxIn(COutPoint(txid, 1), CScript([script_pubkey]))]
        p2sh_p2wsh_tx.vout = [CTxOut(value, CScript([OP_TRUE]))]
        p2sh_p2wsh_tx.wit.vtxinwit.append(CTxInWitness())
        p2sh_p2wsh_tx.wit.vtxinwit[0].scriptWitness.stack = [CScript([OP_TRUE])]
        p2sh_p2wsh_tx.rehash()

        block = self.build_next_block()
        self.update_witness_block_with_transactions(block, [p2sh_p2wsh_tx], with_witness=True)
        test_witness_block(self.nodes[0], self.test_node, block, with_witness=True, accepted=False)
        test_witness_block(self.nodes[0], self.test_node, block, with_witness=False, accepted=True)

        connect_nodes(self.nodes[0], 2)

        self.utxo.pop(0)
        self.utxo.append(UTXO(txid, 2, value))


    @subtest
    def test_getblocktemplate_before_lockin(self):
        # Node0 is segwit aware, node2 is not.
        for node in [self.nodes[0], self.nodes[2]]:
            gbt_results = node.getblocktemplate()
            block_version = gbt_results['version']
            # If we're not indicating segwit support, we will still be
            # signalling for segwit activation.
            assert_equal(block_version & (1 << VB_WITNESS_BIT), 0)
            # If we don't specify the segwit rule, then we won't get a default
            # commitment.
            assert('default_witness_commitment' not in gbt_results)

        # Workaround:
        # Can either change the tip, or change the mempool and wait 5 seconds
        # to trigger a recomputation of getblocktemplate.
        txid = int(self.nodes[0].sendtoaddress(self.nodes[0].getnewaddress(), 1), 16)
        # Using mocktime lets us avoid sleep()
        sync_mempools(self.nodes)
        self.nodes[0].setmocktime(int(time.time()) + 10)
        self.nodes[2].setmocktime(int(time.time()) + 10)

        for node in [self.nodes[0], self.nodes[2]]:
            gbt_results = node.getblocktemplate({"rules": ["segwit"]})
            block_version = gbt_results['version']
            # If this is a non-segwit node, we should still not get a witness
            # commitment, nor a version bit signalling segwit.
            assert_equal(block_version & (1 << VB_WITNESS_BIT), 0)
            assert('default_witness_commitment' not in gbt_results)

        # undo mocktime
        self.nodes[0].setmocktime(0)
        self.nodes[2].setmocktime(0)

    @subtest
    def test_witness_tx_relay_before_segwit_activation(self):

        # Generate a transaction that doesn't require a witness, but send it
        # with a witness.  Should be rejected for premature-witness, but should
        # not be added to recently rejected list.
        tx = CTransaction()
        tx.vin.append(CTxIn(COutPoint(self.utxo[0].sha256, self.utxo[0].n), b""))
        tx.vout.append(CTxOut(self.utxo[0].nValue - 1000, CScript([OP_TRUE, OP_DROP] * 15 + [OP_TRUE])))
        tx.wit.vtxinwit.append(CTxInWitness())
        tx.wit.vtxinwit[0].scriptWitness.stack = [b'a']
        tx.rehash()

        tx_hash = tx.malfixsha256
        tx_value = tx.vout[0].nValue

        # Verify that if a peer doesn't set nServices to include NODE_WITNESS,
        # the getdata is just for the non-witness portion.
        self.old_node.announce_tx_and_wait_for_getdata(tx)
        assert(self.old_node.last_message["getdata"].inv[0].type == 1)

        # Since we haven't delivered the tx yet, inv'ing the same tx from
        # a witness transaction ought not result in a getdata.
        self.test_node.announce_tx_and_wait_for_getdata(tx, timeout=2, success=False)

        # Delivering this transaction without witness should succeed
        assert_equal(len(self.nodes[0].getrawmempool()), 1)
        assert_equal(len(self.nodes[1].getrawmempool()), 1)

        # sent without witness
        test_transaction_acceptance(self.nodes[0].rpc, self.old_node, tx, with_witness=True, accepted=False)
        test_transaction_acceptance(self.nodes[0].rpc, self.test_node, tx, with_witness=True, accepted=False)

        test_transaction_acceptance(self.nodes[0].rpc, self.old_node, tx, with_witness=False, accepted=True)
        test_transaction_acceptance(self.nodes[0].rpc, self.test_node, tx, with_witness=False, accepted=True)

        # Cleanup: mine the first transaction and update utxo
        self.nodes[0].generate(1, self.signblockprivkeys)
        assert_equal(len(self.nodes[0].getrawmempool()), 0)

        self.utxo.pop(0)
        self.utxo.append(UTXO(tx_hash, 0, tx_value))

    @subtest
    def test_standardness_v0(self):
        """Test V0 txout standardness.

        V0 segwit outputs and inputs are always standard.
        V0 segwit inputs may only be mined after activation, but not before."""

        witness_program = CScript([OP_TRUE])
        witness_hash = sha256(witness_program)
        script_pubkey = CScript([OP_0, witness_hash])

        p2sh_pubkey = hash160(witness_program)
        p2sh_script_pubkey = CScript([OP_HASH160, p2sh_pubkey, OP_EQUAL])

        # First prepare a p2sh output (so that spending it will pass standardness)
        p2sh_tx = CTransaction()
        p2sh_tx.vin = [CTxIn(COutPoint(self.utxo[0].sha256, self.utxo[0].n), b"")]
        p2sh_tx.vout = [CTxOut(self.utxo[0].nValue - 1000, p2sh_script_pubkey)]
        p2sh_tx.rehash()

        # Mine it on test_node to create the confirmed output.
        test_transaction_acceptance(self.nodes[0].rpc, self.test_node, p2sh_tx, with_witness=False, accepted=True)

        self.nodes[0].generate(1, self.signblockprivkeys)
        sync_blocks(self.nodes)

        # Now test standardness of v0 P2WSH outputs.
        # Start by creating a transaction with two outputs.
        tx = CTransaction()
        tx.vin = [CTxIn(COutPoint(p2sh_tx.malfixsha256, 0), CScript([witness_program]))]
        tx.vout = [CTxOut(p2sh_tx.vout[0].nValue - 10000, script_pubkey)]
        tx.vout.append(CTxOut(8000, script_pubkey))  # Might burn this later
        tx.vin[0].nSequence = BIP125_SEQUENCE_NUMBER  # Just to have the option to bump this tx from the mempool
        tx.rehash()

        # This is always accepted, since the mempool policy is to consider segwit as always active
        # and thus allow segwit outputs
        # tapyrus witness is non std
        test_transaction_acceptance(self.nodes[1].rpc, self.std_node, tx, with_witness=False, accepted=False, reason=b"scriptpubkey")
        test_transaction_acceptance(self.nodes[0].rpc, self.test_node, tx, with_witness=False, accepted=True)

        # Now create something that looks like a P2PKH output. This won't be spendable.
        script_pubkey = CScript([OP_0, hash160(witness_hash)])
        tx2 = CTransaction()
        # tx was accepted, so we spend the second output.
        tx2.vin = [CTxIn(COutPoint(tx.malfixsha256, 1), b"")]
        tx2.vout = [CTxOut(7000, script_pubkey)]
        tx2.wit.vtxinwit.append(CTxInWitness())
        tx2.wit.vtxinwit[0].scriptWitness.stack = [witness_program]
        tx2.rehash()

        test_transaction_acceptance(self.nodes[1].rpc, self.std_node, tx2, with_witness=False, accepted=False, reason=b"scriptpubkey")
        test_transaction_acceptance(self.nodes[0].rpc, self.test_node, tx2, with_witness=False, accepted=True)
        # Now update self.utxo for later tests.
        tx3 = CTransaction()
        # tx and tx2 were both accepted.  Don't bother trying to reclaim the
        # P2PKH output; just send tx's first output back to an anyone-can-spend.
        #sync_mempools([self.nodes[0], self.nodes[1]])
        tx3.vin = [CTxIn(COutPoint(tx.malfixsha256, 0), b"")]
        tx3.vout = [CTxOut(tx.vout[0].nValue - 1000, CScript([OP_TRUE, OP_DROP] * 15 + [OP_TRUE]))]
        tx3.wit.vtxinwit.append(CTxInWitness())
        tx3.wit.vtxinwit[0].scriptWitness.stack = [witness_program]
        tx3.rehash()

        # Just check mempool acceptance, but don't add the transaction to the mempool, since witness is disallowed
        # in blocks and the tx is impossible to mine right now.
        assert_raises_rpc_error(-22, "TX decode failed", self.nodes[0].testmempoolaccept, [bytes_to_hex_str(tx3.serialize_with_witness(with_scriptsig=True))])
        assert_equal(self.nodes[0].testmempoolaccept([bytes_to_hex_str(tx3.serialize())]), [{'txid': tx3.hashMalFix, 'allowed': True}])
        # Create the same output as tx3, but by replacing tx
        tx3_out = tx3.vout[0]
        tx3 = tx
        tx3.vout = [tx3_out]
        tx3.rehash()
        assert_equal(self.nodes[0].testmempoolaccept([bytes_to_hex_str(tx3.serialize_with_witness(with_scriptsig=True))]), [{'txid': tx3.hashMalFix, 'allowed':  True}])
        test_transaction_acceptance(self.nodes[0].rpc, self.test_node, tx3, with_witness=False, accepted=True)
        self.nodes[0].generate(1, self.signblockprivkeys)
        sync_blocks(self.nodes)
        self.utxo.pop(0)
        self.utxo.append(UTXO(tx3.malfixsha256, 0, tx3.vout[0].nValue))
        assert_equal(len(self.nodes[1].getrawmempool()), 0)


    @subtest
    def test_p2sh_witness(self):
        """Test P2SH wrapped witness programs."""

        # Prepare the p2sh-wrapped witness output
        witness_program = CScript([OP_DROP, OP_TRUE])
        witness_hash = sha256(witness_program)
        p2wsh_pubkey = CScript([OP_0, witness_hash])
        p2sh_witness_hash = hash160(p2wsh_pubkey)
        script_pubkey = CScript([OP_HASH160, p2sh_witness_hash, OP_EQUAL])
        script_sig = CScript([p2wsh_pubkey])  # a push of the redeem script

        # Fund the P2SH output
        tx = CTransaction()
        tx.vin.append(CTxIn(COutPoint(self.utxo[0].sha256, self.utxo[0].n), b""))
        tx.vout.append(CTxOut(self.utxo[0].nValue - 1000, script_pubkey))
        tx.rehash()

        # Verify mempool acceptance and block validity
        test_transaction_acceptance(self.nodes[0].rpc, self.test_node, tx, with_witness=False, accepted=True)
        block = self.build_next_block()
        #with witness commitment
        self.update_witness_block_with_transactions(block, [tx], with_witness=True)
        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=True, accepted=False)
        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=False, accepted=True)
        sync_blocks(self.nodes)

        # Now test attempts to spend the output.
        spend_tx = CTransaction()
        spend_tx.vin.append(CTxIn(COutPoint(tx.malfixsha256, 0), script_sig))
        spend_tx.vout.append(CTxOut(tx.vout[0].nValue - 1000, CScript([OP_TRUE])))
        spend_tx.rehash()

        # This transaction should  be accepted into the mempool 
        test_transaction_acceptance(self.nodes[0].rpc, self.test_node, spend_tx, with_witness=True, accepted=True)

        block = self.build_next_block()
        self.update_witness_block_with_transactions(block, [spend_tx], with_witness=True)

        # If we're after activation, then sending this with witnesses should be valid.
        # This no longer works before activation, because SCRIPT_VERIFY_WITNESS
        # is always set.
        # TODO: rewrite this test to make clear that it only works after activation.
        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=True, accepted=False)
        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=False, accepted=True)

        # Update self.utxo
        self.utxo.pop(0)
        self.utxo.append(UTXO(spend_tx.malfixsha256, 0, spend_tx.vout[0].nValue))

    @subtest
    def test_witness_commitments(self):
        """Test witness commitments.

        This test can only be run after segwit has activated."""

        # First try a correct witness commitment.
        block = self.build_next_block()
        add_witness_commitment(block)
        block.solve(self.signblockprivkeys)

        # Test the test -- witness serialization should be different
        assert(msg_witness_block(block).serialize() != msg_block(block).serialize())

        # This empty block should be valid.
        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=True,accepted=False)

        #block without witness commitment
        block = self.build_next_block()
        block.hashMerkleRoot = block.calc_merkle_root()
        block.hashImMerkleRoot = block.calc_immutable_merkle_root()
        block.solve(self.signblockprivkeys)

        # Test the test -- witness serialization should be the same
        assert(msg_witness_block(block).serialize() == msg_block(block).serialize())

        # This empty block should be valid.
        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=True,accepted=True)

        # Try to tweak the nonce
        block_2 = self.build_next_block()
        add_witness_commitment(block_2, nonce=28)
        block_2.solve(self.signblockprivkeys)

        # The commitment should have changed!
        assert(block_2.vtx[0].vout[-1] != block.vtx[0].vout[-1])

        # This should also be valid.
        test_witness_block(self.nodes[0].rpc, self.test_node, block_2, with_witness=True,accepted=False)

        #same block without witness commitment
        block_2 = self.build_next_block()
        block_2.hashMerkleRoot = block_2.calc_merkle_root()
        block_2.hashImMerkleRoot = block_2.calc_immutable_merkle_root()
        block_2.solve(self.signblockprivkeys)

        # This should also be valid.
        test_witness_block(self.nodes[0].rpc, self.test_node, block_2, with_witness=True,accepted=True)

        # Now test commitments with actual transactions
        tx = CTransaction()
        tx.vin.append(CTxIn(COutPoint(self.utxo[0].sha256, self.utxo[0].n), b""))

        # Let's construct a witness program
        witness_program = CScript([OP_TRUE])
        witness_hash = sha256(witness_program)
        script_pubkey = CScript([OP_0, witness_hash])
        tx.vout.append(CTxOut(self.utxo[0].nValue - 1000, script_pubkey))
        tx.rehash()

        # tx2 will spend tx1, and send back to a regular anyone-can-spend address
        tx2 = CTransaction()
        tx2.vin.append(CTxIn(COutPoint(tx.malfixsha256, 0), b""))
        tx2.vout.append(CTxOut(tx.vout[0].nValue - 1000, witness_program))
        tx2.wit.vtxinwit.append(CTxInWitness())
        tx2.wit.vtxinwit[0].scriptWitness.stack = [witness_program]
        tx2.rehash()

        block_3 = self.build_next_block()
        self.update_witness_block_with_transactions(block_3, [tx, tx2], nonce=1)
        # Add an extra OP_RETURN output that matches the witness commitment template,
        # even though it has extra data after the incorrect commitment.
        # This block should fail.
        block_3.vtx[0].vout.append(CTxOut(0, CScript([OP_RETURN, WITNESS_COMMITMENT_HEADER + ser_uint256(2), 10])))
        block_3.vtx[0].rehash()
        block_3.hashMerkleRoot = block_3.calc_merkle_root()
        block_3.hashImMerkleRoot = block_3.calc_immutable_merkle_root()
        block_3.rehash()
        block_3.solve(self.signblockprivkeys)

        test_witness_block(self.nodes[0].rpc, self.test_node, block_3, with_witness=True,accepted=False)

        block_3 = self.build_next_block()
        block_3.vtx.extend([tx, tx2])
        block_3.hashMerkleRoot = block_3.calc_merkle_root()
        block_3.hashImMerkleRoot = block_3.calc_immutable_merkle_root()
        block_3.rehash()
        block_3.solve(self.signblockprivkeys)

        test_witness_block(self.nodes[0].rpc, self.test_node, block_3, with_witness=True,accepted=False)

        # tx2 will spend tx1, and send back to a regular anyone-can-spend address
        block_3.vtx[2] = CTransaction()
        block_3.vtx[2].vin.append(CTxIn(COutPoint(tx.malfixsha256, 0), b""))
        block_3.vtx[2].vout.append(CTxOut(tx.vout[0].nValue - 1000, witness_program))
        block_3.vtx[2].rehash()

        test_witness_block(self.nodes[0].rpc, self.test_node, block_3, with_witness=False,accepted=True)

        # Finally test that a block with no witness transactions can
        # omit the commitment.
        block_4 = self.build_next_block()
        tx3 = CTransaction()
        tx3.vin.append(CTxIn(COutPoint(tx2.malfixsha256, 0), b""))
        tx3.vout.append(CTxOut(tx.vout[0].nValue - 1000, witness_program))
        tx3.rehash()
        block_4.vtx.append(tx3)
        block_4.hashMerkleRoot = block_4.calc_merkle_root()
        block_4.hashImMerkleRoot = block_4.calc_immutable_merkle_root()
        block_4.solve(self.signblockprivkeys)
        block_4.rehash()
        test_witness_block(self.nodes[0].rpc, self.test_node, block_4, with_witness=True,accepted=True)

        # Update available utxo's for use in later test.
        self.utxo.pop(0)
        self.utxo.append(UTXO(tx3.malfixsha256, 0, tx3.vout[0].nValue))

    @subtest
    def test_block_malleability(self):

        # Make sure that a block that has too big a virtual size
        # because of a too-large coinbase witness is not permanently
        # marked bad.
        block = self.build_next_block()
        add_witness_commitment(block)
        block.solve(self.signblockprivkeys)

        block.vtx[0].wit.vtxinwit[0].scriptWitness.stack.append(b'a' * 5000000)
        assert(get_virtual_size(block) > MAX_BLOCK_BASE_SIZE)

        # We can't send over the p2p network, because this is too big to relay
        # TODO: repeat this test with a block that can be relayed
        assert_raises_rpc_error(-22, "Block does not start with a coinbase", self.nodes[0].submitblock, bytes_to_hex_str(block.serialize(with_witness=True)))

        assert(self.nodes[0].getbestblockhash() != block.hash)

        block.vtx[0].wit.vtxinwit[0].scriptWitness.stack.pop()
        assert(get_virtual_size(block) < MAX_BLOCK_BASE_SIZE)
        assert_raises_rpc_error(-22, "Block does not start with a coinbase", self.nodes[0].submitblock, bytes_to_hex_str(block.serialize(with_witness=True)))

        #same block without witness commitment
        block = self.build_next_block()
        block.rehash()
        block.solve(self.signblockprivkeys)

        # accepted without witness commitment
        self.nodes[0].submitblock(bytes_to_hex_str(block.serialize()))

        assert(self.nodes[0].getbestblockhash() == block.hash)

        # Now make sure that malleating the witness reserved value doesn't
        # result in a block permanently marked bad.
        block = self.build_next_block()
        block.vtx[0].wit.vtxinwit.append(CTxInWitness())
        add_witness_commitment(block)
        block.solve(self.signblockprivkeys)

        # Change the nonce -- should not cause the block to be permanently
        # failed
        block.vtx[0].wit.vtxinwit[0].scriptWitness.stack = [ser_uint256(1)]
        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=True, accepted=False)

        # Changing the witness reserved value doesn't change the block hash
        block.vtx[0].wit.vtxinwit[0].scriptWitness.stack = [ser_uint256(0)]
        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=True, accepted=False)
        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=False, accepted=True)

        #accepted without witness commitment
        block = self.build_next_block()
        block.solve(self.signblockprivkeys)
        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=True,accepted=True)


    @subtest
    def test_witness_block_size(self):
        # TODO: Test that non-witness carrying blocks can't exceed 1MB
        # Skipping this test for now; this is covered in p2p-fullblocktest.py

        # Test that witness-bearing blocks are limited at ceil(base + wit/4) <= 1MB.
        block = self.build_next_block()

        assert(len(self.utxo) > 0)

        # Create a P2WSH transaction.
        # The witness program will be a bunch of OP_2DROP's, followed by OP_TRUE.
        # This should give us plenty of room to tweak the spending tx's
        # virtual size.
        NUM_DROPS = 200  # 201 max ops per script!
        NUM_OUTPUTS = 50

        witness_program = CScript([OP_2DROP] * NUM_DROPS + [OP_TRUE])
        witness_hash = uint256_from_str(sha256(witness_program))
        script_pubkey = CScript([OP_0, ser_uint256(witness_hash)])

        prevout = COutPoint(self.utxo[0].sha256, self.utxo[0].n)
        value = self.utxo[0].nValue

        parent_tx = CTransaction()
        parent_tx.vin.append(CTxIn(prevout, b""))
        child_value = int(value / NUM_OUTPUTS)
        for i in range(NUM_OUTPUTS):
            parent_tx.vout.append(CTxOut(child_value, script_pubkey))
        parent_tx.vout[0].nValue -= 50000
        assert(parent_tx.vout[0].nValue > 0)
        parent_tx.rehash()

        child_tx = CTransaction()
        for i in range(NUM_OUTPUTS):
            child_tx.vin.append(CTxIn(COutPoint(parent_tx.malfixsha256, i), b""))
        child_tx.vout = [CTxOut(value - 100000, CScript([OP_TRUE]))]
        for i in range(NUM_OUTPUTS):
            child_tx.wit.vtxinwit.append(CTxInWitness())
            child_tx.wit.vtxinwit[-1].scriptWitness.stack = [b'a' * 195] * (2 * NUM_DROPS) + [witness_program]
        child_tx.rehash()
        self.update_witness_block_with_transactions(block, [parent_tx, child_tx])

        block.solve(self.signblockprivkeys)
        prooflen = len(ser_string_vector(block.proof)) - len(ser_compact_size(len(block.proof)))
        vsize = get_virtual_size(block)
        additional_bytes = (MAX_BLOCK_BASE_SIZE - vsize) * 4
        i = 0
        while additional_bytes > 0:
            # Add some more bytes to each input until we hit MAX_BLOCK_BASE_SIZE+1
            extra_bytes = min(additional_bytes + 1, 55)
            block.vtx[-1].wit.vtxinwit[int(i / (2 * NUM_DROPS))].scriptWitness.stack[i % (2 * NUM_DROPS)] = b'a' * (195 + extra_bytes)
            additional_bytes -= extra_bytes
            i += 1

        block.vtx[0].vout.pop()  # Remove old commitment
        add_witness_commitment(block)
        block.solve(self.signblockprivkeys)
        i = 0
        while(prooflen != len(ser_string_vector(block.proof)) - len(ser_compact_size(len(block.proof))) and i < 10):
            block.solve(self.signblockprivkeys)
            i += 1
        vsize = get_virtual_size(block)
        assert_equal(vsize, MAX_BLOCK_BASE_SIZE + 1)
        # Make sure that our test case would exceed the old max-network-message
        # limit
        assert(len(block.serialize(with_witness=True)) > 2 * 1024 * 1024)

        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=True, accepted=False)
        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=False, accepted=True)

        # Update available utxo's
        self.utxo.pop(0)
        self.utxo.append(UTXO(block.vtx[-1].malfixsha256, 0, block.vtx[-1].vout[0].nValue))

    @subtest
    def test_submit_block(self):
        """Test that submitblock adds the nonce automatically when possible."""
        block = self.build_next_block()

        # Try using a custom nonce and then don't supply it.
        # This shouldn't possibly work.
        add_witness_commitment(block, nonce=1)
        block.vtx[0].wit = CTxWitness()  # drop the nonce
        block.solve(self.signblockprivkeys)
        self.nodes[0].submitblock(bytes_to_hex_str(block.serialize(with_witness=True)))
        assert(self.nodes[0].getbestblockhash() == block.hash)

        # This time, add a tx with non-empty witness, but don't supply
        # the commitment.
        block_2 = self.build_next_block()

        add_witness_commitment(block_2)

        block_2.solve(self.signblockprivkeys)

        # Drop commitment and nonce -- submitblock should not fill in.
        block_2.vtx[0].vout.pop()
        block_2.vtx[0].wit = CTxWitness()

        self.nodes[0].submitblock(bytes_to_hex_str(block_2.serialize(with_witness=True)))
        # Tip should not advance!
        assert(self.nodes[0].getbestblockhash() != block_2.hash)

    @subtest
    def test_extra_witness_data(self):
        """Test extra witness data in a transaction."""

        block = self.build_next_block()

        witness_program = CScript([OP_DROP, OP_TRUE])
        witness_hash = sha256(witness_program)
        script_pubkey = CScript([OP_0, witness_hash])

        # First try extra witness data on a tx that doesn't require a witness
        tx = CTransaction()
        tx.vin.append(CTxIn(COutPoint(self.utxo[0].sha256, self.utxo[0].n), b""))
        tx.vout.append(CTxOut(self.utxo[0].nValue - 2000, script_pubkey))
        tx.vout.append(CTxOut(1000, CScript([OP_TRUE])))  # non-witness output
        tx.wit.vtxinwit.append(CTxInWitness())
        tx.wit.vtxinwit[0].scriptWitness.stack = [CScript([])]
        tx.rehash()
        self.update_witness_block_with_transactions(block, [tx], with_witness=True)

        # Extra witness data should not be allowed.
        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=True, accepted=False)

        # Try extra signature data.  Ok if we're not spending a witness output.
        block.vtx[1].wit.vtxinwit = []
        block.vtx[1].vin[0].scriptSig = CScript([OP_0])
        block.vtx[1].rehash()
        add_witness_commitment(block)
        block.solve(self.signblockprivkeys)

        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=True, accepted=False)

        #without witness commitment
        block = self.build_next_block()
        self.update_witness_block_with_transactions(block, [tx], with_witness=False)
        block.solve(self.signblockprivkeys)
        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=False, accepted=True)

        # Now try extra witness/signature data on an input that DOES require a
        # witness
        tx2 = CTransaction()
        tx2.vin.append(CTxIn(COutPoint(tx.malfixsha256, 0), b""))  # witness output
        tx2.vin.append(CTxIn(COutPoint(tx.malfixsha256, 1), b""))  # non-witness
        tx2.vout.append(CTxOut(tx.vout[0].nValue, CScript([OP_TRUE])))
        tx2.wit.vtxinwit.extend([CTxInWitness(), CTxInWitness()])
        tx2.wit.vtxinwit[0].scriptWitness.stack = [CScript([CScriptNum(1)]), CScript([CScriptNum(1)]), witness_program]
        tx2.wit.vtxinwit[1].scriptWitness.stack = [CScript([OP_TRUE])]

        block = self.build_next_block()
        self.update_witness_block_with_transactions(block, [tx2], with_witness=True)

        # This has extra witness data, so it should fail.
        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=True, accepted=False)

        # Now get rid of the extra witness, but add extra scriptSig data
        tx2.vin[0].scriptSig = CScript([OP_TRUE])
        tx2.vin[1].scriptSig = CScript([OP_TRUE])
        tx2.wit.vtxinwit[0].scriptWitness.stack.pop(0)
        tx2.wit.vtxinwit[1].scriptWitness.stack = []
        tx2.rehash()
        add_witness_commitment(block)
        block.solve(self.signblockprivkeys)

        # This has extra signature data for a witness input, so it should fail.
        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=True, accepted=False)

        # Now get rid of the extra scriptsig on the witness input, and verify
        # success (even with extra scriptsig data in the non-witness input)
        tx2.vin[0].scriptSig = b""
        tx2.rehash()
        add_witness_commitment(block)
        block.solve(self.signblockprivkeys)

        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=True, accepted=False)

        #without witness commitment
        block = self.build_next_block()
        self.update_witness_block_with_transactions(block, [tx2], with_witness=False)
        block.hashMerkleRoot = block.calc_merkle_root()
        block.hashImMerkleRoot = block.calc_immutable_merkle_root()
        block.rehash()
        block.solve(self.signblockprivkeys)
        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=False, accepted=True)
        # Update utxo for later tests
        self.utxo.pop(0)
        self.utxo.append(UTXO(tx2.malfixsha256, 0, tx2.vout[0].nValue))

    @subtest
    def test_max_witness_push_length(self):
        """Test that witness stack can only allow up to 520 byte pushes."""

        block = self.build_next_block()

        witness_program = CScript([OP_DROP, OP_TRUE])
        witness_hash = sha256(witness_program)
        script_pubkey = CScript([OP_0, witness_hash])

        tx = CTransaction()
        tx.vin.append(CTxIn(COutPoint(self.utxo[0].sha256, self.utxo[0].n), b""))
        tx.vout.append(CTxOut(self.utxo[0].nValue - 1000, script_pubkey))
        tx.rehash()

        tx2 = CTransaction()
        tx2.vin.append(CTxIn(COutPoint(tx.malfixsha256, 0), b""))
        tx2.vout.append(CTxOut(tx.vout[0].nValue - 1000, CScript([OP_TRUE])))
        tx2.wit.vtxinwit.append(CTxInWitness())
        # First try a 521-byte stack element
        tx2.wit.vtxinwit[0].scriptWitness.stack = [b'a' * (MAX_SCRIPT_ELEMENT_SIZE + 1), witness_program]
        tx2.rehash()

        self.update_witness_block_with_transactions(block, [tx, tx2], with_witness=True)
        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=True, accepted=False)
        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=False, accepted=True)

        # Update the utxo for later tests
        self.utxo.pop()
        self.utxo.append(UTXO(tx2.malfixsha256, 0, tx2.vout[0].nValue))

    @subtest
    def test_max_witness_program_length(self):
        """Test that witness outputs greater than 10kB can't be spent."""

        MAX_PROGRAM_LENGTH = 10000

        # This program is 19 max pushes (9937 bytes), then 64 more opcode-bytes.
        long_witness_program = CScript([b'a' * 520] * 19 + [OP_DROP] * 63 + [OP_TRUE])
        assert(len(long_witness_program) == MAX_PROGRAM_LENGTH + 1)
        long_witness_hash = sha256(long_witness_program)
        long_script_pubkey = CScript([OP_0, long_witness_hash])

        block = self.build_next_block()

        tx = CTransaction()
        tx.vin.append(CTxIn(COutPoint(self.utxo[0].sha256, self.utxo[0].n), b""))
        tx.vout.append(CTxOut(self.utxo[0].nValue - 1000, long_script_pubkey))
        tx.rehash()

        tx2 = CTransaction()
        tx2.vin.append(CTxIn(COutPoint(tx.malfixsha256, 0), b""))
        tx2.vout.append(CTxOut(tx.vout[0].nValue - 1000, CScript([OP_TRUE])))
        tx2.wit.vtxinwit.append(CTxInWitness())
        tx2.wit.vtxinwit[0].scriptWitness.stack = [b'a'] * 44 + [long_witness_program]
        tx2.rehash()

        self.update_witness_block_with_transactions(block, [tx, tx2])

        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=True, accepted=False)

        # Try again with one less byte in the witness program
        witness_program = CScript([b'a' * 520] * 19 + [OP_DROP] * 62 + [OP_TRUE])
        assert(len(witness_program) == MAX_PROGRAM_LENGTH)
        witness_hash = sha256(witness_program)
        script_pubkey = CScript([OP_0, witness_hash])

        tx.vout[0] = CTxOut(tx.vout[0].nValue, script_pubkey)
        tx.rehash()
        tx2.vin[0].prevout.hash = tx.malfixsha256
        tx2.wit.vtxinwit[0].scriptWitness.stack = [b'a'] * 43 + [witness_program]
        tx2.rehash()
        block.vtx = [block.vtx[0]]
        self.update_witness_block_with_transactions(block, [tx, tx2])
        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=True, accepted=False)
        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=False, accepted=True)

        self.utxo.pop()
        self.utxo.append(UTXO(tx2.malfixsha256, 0, tx2.vout[0].nValue))

    @subtest
    def test_witness_input_length(self):
        """Test that vin length must match vtxinwit length."""

        witness_program = CScript([OP_DROP, OP_TRUE])
        witness_hash = sha256(witness_program)
        script_pubkey = CScript([OP_0, witness_hash])

        # Create a transaction that splits our utxo into many outputs
        tx = CTransaction()
        tx.vin.append(CTxIn(COutPoint(self.utxo[0].sha256, self.utxo[0].n), b""))
        value = self.utxo[0].nValue
        for i in range(10):
            tx.vout.append(CTxOut(int(value / 10), script_pubkey))
        tx.vout[0].nValue -= 1000
        assert(tx.vout[0].nValue >= 0)

        block = self.build_next_block()
        self.update_witness_block_with_transactions(block, [tx])
        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=True, accepted=False)
        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=False, accepted=True)

        # Try various ways to spend tx that should all break.
        # This "broken" transaction serializer will not normalize
        # the length of vtxinwit.
        class BrokenCTransaction(CTransaction):
            def serialize_with_witness(self):
                flags = 0
                if not self.wit.is_null():
                    flags |= 1
                r = b""
                r += struct.pack("<i", self.nVersion)
                if flags:
                    dummy = []
                    r += ser_vector(dummy)
                    r += struct.pack("<B", flags)
                r += ser_vector(self.vin)
                r += ser_vector(self.vout)
                if flags & 1:
                    r += self.wit.serialize()
                r += struct.pack("<I", self.nLockTime)
                return r

        tx2 = BrokenCTransaction()
        for i in range(10):
            tx2.vin.append(CTxIn(COutPoint(tx.malfixsha256, i), b""))
        tx2.vout.append(CTxOut(value - 3000, CScript([OP_TRUE])))

        # First try using a too long vtxinwit
        for i in range(11):
            tx2.wit.vtxinwit.append(CTxInWitness())
            tx2.wit.vtxinwit[i].scriptWitness.stack = [b'a', witness_program]

        block = self.build_next_block()
        self.update_witness_block_with_transactions(block, [tx2])
        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=True,accepted=False)
        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=False,accepted=True)

        self.utxo.pop()
        self.utxo.append(UTXO(tx2.malfixsha256, 0, tx2.vout[0].nValue))

    @subtest
    def test_tx_relay_after_segwit_activation(self):
        """Test transaction relay after segwit activation.

        After segwit activates, verify that mempool:
        - rejects transactions with unnecessary/extra witnesses
        - accepts transactions with valid witnesses
        and that witness transactions are relayed to non-upgraded peers."""

        # Generate a transaction that doesn't require a witness, but send it
        # with a witness.  Should be rejected because we can't use a witness
        # when spending a non-witness output.
        tx = CTransaction()
        tx.vin.append(CTxIn(COutPoint(self.utxo[0].sha256, self.utxo[0].n), b""))
        tx.vout.append(CTxOut(self.utxo[0].nValue - 1000, CScript([OP_TRUE, OP_DROP] * 15 + [OP_TRUE])))
        tx.wit.vtxinwit.append(CTxInWitness())
        tx.wit.vtxinwit[0].scriptWitness.stack = [b'a']
        tx.rehash()

        tx_hash = tx.malfixsha256

        # Verify that unnecessary witnesses are rejected.
        self.test_node.announce_tx_and_wait_for_getdata(tx)
        assert_equal(len(self.nodes[0].getrawmempool()), 0)
        test_transaction_acceptance(self.nodes[0].rpc, self.test_node, tx, with_witness=True, accepted=False)

        # Verify that removing the witness succeeds.
        test_transaction_acceptance(self.nodes[0].rpc, self.test_node, tx, with_witness=False, accepted=True)

        # Now try to add extra witness data to a valid witness tx.
        witness_program = CScript([OP_TRUE])
        witness_hash = sha256(witness_program)
        script_pubkey = CScript([OP_0, witness_hash])
        tx2 = CTransaction()
        tx2.vin.append(CTxIn(COutPoint(tx_hash, 0), b""))
        tx2.vout.append(CTxOut(tx.vout[0].nValue - 1000, script_pubkey))
        tx2.rehash()

        tx3 = CTransaction()
        tx3.vin.append(CTxIn(COutPoint(tx2.malfixsha256, 0), b""))
        tx3.wit.vtxinwit.append(CTxInWitness())

        # Add too-large for IsStandard witness and check that it does not enter reject filter
        p2sh_program = CScript([OP_TRUE])
        p2sh_pubkey = hash160(p2sh_program)
        witness_program2 = CScript([b'a' * 400000])
        tx3.vout.append(CTxOut(tx2.vout[0].nValue - 1000, CScript([OP_HASH160, p2sh_pubkey, OP_EQUAL])))
        tx3.wit.vtxinwit[0].scriptWitness.stack = [witness_program2]
        tx3.rehash()

        # Node will not be blinded to the transaction
        self.std_node.announce_tx_and_wait_for_getdata(tx3)
        test_transaction_acceptance(self.nodes[1].rpc, self.std_node, tx3, with_witness=True, accepted=False)
        #self.std_node.announce_tx_and_wait_for_getdata(tx3)
        test_transaction_acceptance(self.nodes[1].rpc, self.std_node, tx3, with_witness=False, accepted=False)

        # Remove witness stuffing, instead add extra witness push on stack
        tx3.vout[0] = CTxOut(tx2.vout[0].nValue - 1000, CScript([OP_TRUE, OP_DROP] * 15 + [OP_TRUE]))
        tx3.wit.vtxinwit[0].scriptWitness.stack = [CScript([CScriptNum(1)]), witness_program]
        tx3.rehash()

        test_transaction_acceptance(self.nodes[0].rpc, self.test_node, tx2, with_witness=False, accepted=True)

        test_transaction_acceptance(self.nodes[0].rpc, self.test_node, tx3, with_witness=True, accepted=False)

        # Get rid of the extra witness, and verify acceptance.
        tx3.wit.vtxinwit[0].scriptWitness.stack = [witness_program]
        # Also check that old_node gets a tx announcement, even though this is
        # a witness transaction.
        self.old_node.wait_for_inv([CInv(1, tx2.malfixsha256)])  # wait until tx2 was inv'ed
        self.nodes[0].generate(1, self.signblockprivkeys)
        assert_equal(len(self.nodes[0].getrawmempool()), 0)

        test_transaction_acceptance(self.nodes[0].rpc, self.test_node, tx3, with_witness=True, accepted=False)
        test_transaction_acceptance(self.nodes[0].rpc, self.test_node, tx3, with_witness=False, accepted=True)
        #self.old_node.wait_for_inv([CInv(1, tx3.malfixsha256)])

        block = self.build_next_block()
        self.update_witness_block_with_transactions(block, [tx3])
        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=True,accepted=False)
        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=False, accepted=True)

        # Test that getrawtransaction returns correct witness information
        # hash, size, vsize

        raw_tx = self.nodes[0].getrawtransaction(tx3.hashMalFix, 1)
        assert_equal(int(raw_tx["hash"], 16), tx3.calc_sha256(True))
        assert_equal(raw_tx["size"], len(tx3.serialize_without_witness()))
        weight = 4 * len(tx3.serialize_without_witness())
        vsize = math.ceil(weight / 4)
        assert_equal(raw_tx["vsize"], vsize)
        assert_equal(raw_tx["weight"], weight)
        assert("txinwitness" not in raw_tx["vin"][0].keys())
        assert(vsize == raw_tx["size"])

        # Cleanup: mine the transactions and update utxo for next test
        self.nodes[0].generate(1, self.signblockprivkeys)
        assert_equal(len(self.nodes[0].getrawmempool()), 0)

        self.utxo.pop(0)
        self.utxo.append(UTXO(tx3.malfixsha256, 0, tx3.vout[0].nValue))

    @subtest
    def test_segwit_versions(self):
        """Test validity of future segwit version transactions.

        Future segwit version transactions are non-standard, but valid in blocks.
        Can run this before and after segwit activation."""

        NUM_SEGWIT_VERSIONS = 17  # will test OP_0, OP1, ..., OP_16
        if len(self.utxo) < NUM_SEGWIT_VERSIONS:
            tx = CTransaction()
            tx.vin.append(CTxIn(COutPoint(self.utxo[0].sha256, self.utxo[0].n), b""))
            split_value = (self.utxo[0].nValue - 4000) // NUM_SEGWIT_VERSIONS
            for i in range(NUM_SEGWIT_VERSIONS):
                tx.vout.append(CTxOut(split_value, CScript([OP_TRUE])))
            tx.rehash()
            block = self.build_next_block()
            self.update_witness_block_with_transactions(block, [tx])
            test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=True, accepted=False)
            test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=False, accepted=True)
            self.utxo.pop(0)
            for i in range(NUM_SEGWIT_VERSIONS):
                self.utxo.append(UTXO(tx.malfixsha256, i, split_value))

        sync_blocks(self.nodes)
        temp_utxo = []
        tx = CTransaction()
        witness_program = CScript([OP_TRUE])
        witness_hash = sha256(witness_program)
        assert_equal(len(self.nodes[1].getrawmempool()), 0)
        for version in list(range(OP_1, OP_16 + 1)) + [OP_0]:
            # First try to spend to a future version segwit script_pubkey.
            script_pubkey = CScript([CScriptOp(version), witness_hash])
            tx.vin = [CTxIn(COutPoint(self.utxo[0].sha256, self.utxo[0].n), b"")]
            tx.vout = [CTxOut(self.utxo[0].nValue - 1000, script_pubkey)]
            tx.rehash()
            test_transaction_acceptance(self.nodes[1].rpc, self.std_node, tx, with_witness=True, accepted=False)
            test_transaction_acceptance(self.nodes[0].rpc, self.test_node, tx, with_witness=False, accepted=True)
            self.utxo.pop(0)
            temp_utxo.append(UTXO(tx.malfixsha256, 0, tx.vout[0].nValue))

        self.nodes[0].generate(1, self.signblockprivkeys)  # Mine all the transactions
        sync_blocks(self.nodes)
        assert(len(self.nodes[0].getrawmempool()) == 0)

        # Finally, verify that version 0 -> version 1 transactions
        # are non-standard
        script_pubkey = CScript([CScriptOp(OP_1), witness_hash])
        tx2 = CTransaction()
        tx2.vin = [CTxIn(COutPoint(tx.malfixsha256, 0), b"")]
        tx2.vout = [CTxOut(tx.vout[0].nValue - 1000, script_pubkey)]
        tx2.wit.vtxinwit.append(CTxInWitness())
        tx2.wit.vtxinwit[0].scriptWitness.stack = [witness_program]
        tx2.rehash()
        # Gets accepted to test_node, because standardness of outputs isn't
        # checked with fRequireStandard
        test_transaction_acceptance(self.nodes[0].rpc, self.test_node, tx2, with_witness=True, accepted=False)
        test_transaction_acceptance(self.nodes[0].rpc, self.test_node, tx2, with_witness=False, accepted=True)

        test_transaction_acceptance(self.nodes[1].rpc, self.std_node, tx2, with_witness=True, accepted=False)
        test_transaction_acceptance(self.nodes[1].rpc, self.std_node, tx2, with_witness=False, accepted=False)
        temp_utxo.pop()  # last entry in temp_utxo was the output we just spent
        #temp_utxo.append(UTXO(tx2.malfixsha256, 0, tx2.vout[0].nValue))

        # Spend everything in temp_utxo back to an OP_TRUE output.
        tx3 = CTransaction()
        total_value = 0
        for i in temp_utxo:
            tx3.vin.append(CTxIn(COutPoint(i.sha256, i.n), b""))
            tx3.wit.vtxinwit.append(CTxInWitness())
            total_value += i.nValue
        tx3.wit.vtxinwit[-1].scriptWitness.stack = [witness_program]
        tx3.vout.append(CTxOut(total_value - 1000, CScript([OP_TRUE])))
        tx3.rehash()
        # Spending a higher version witness output is not allowed by policy,
        # even with fRequireStandard=false.
        test_transaction_acceptance(self.nodes[0].rpc, self.test_node, tx3, with_witness=True, accepted=False)
        test_transaction_acceptance(self.nodes[0].rpc, self.test_node, tx3, with_witness=False, accepted=True)

        test_transaction_acceptance(self.nodes[1].rpc, self.std_node, tx3, with_witness=True, accepted=False)
        test_transaction_acceptance(self.nodes[1].rpc, self.std_node, tx3, with_witness=False, accepted=False)
        self.test_node.sync_with_ping()
        #with mininode_lock:
        #    assert(b"reserved for soft-fork upgrades" in self.test_node.last_message["reject"].reason)

        # Building a block with the transaction must be valid, however.
        block = self.build_next_block()
        self.update_witness_block_with_transactions(block, [tx2, tx3], with_witness=True)
        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=True, accepted=False)
        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=False, accepted=True)
        sync_blocks(self.nodes)

        # Add utxo to our list
        self.utxo.append(UTXO(tx3.malfixsha256, 0, tx3.vout[0].nValue))

    @subtest
    def test_premature_coinbase_witness_spend(self):

        block = self.build_next_block()
        # Change the output of the block to be a witness output.
        witness_program = CScript([OP_TRUE])
        witness_hash = sha256(witness_program)
        script_pubkey = CScript([OP_0, witness_hash])
        block.vtx[0].vout[0].scriptPubKey = script_pubkey
        # This next line will rehash the coinbase and update the merkle
        # root, and solve.
        self.update_witness_block_with_transactions(block, [])
        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=True,accepted=False)
        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=False,accepted=True)

        spend_tx = CTransaction()
        spend_tx.vin = [CTxIn(COutPoint(block.vtx[0].malfixsha256, 0), b"")]
        spend_tx.vout = [CTxOut(block.vtx[0].vout[0].nValue, witness_program)]
        spend_tx.wit.vtxinwit.append(CTxInWitness())
        spend_tx.wit.vtxinwit[0].scriptWitness.stack = [witness_program]
        spend_tx.rehash()

        # Now test a premature spend.
        self.nodes[0].generate(98, self.signblockprivkeys)
        sync_blocks(self.nodes)
        block2 = self.build_next_block()
        self.update_witness_block_with_transactions(block2, [spend_tx])
        test_witness_block(self.nodes[0].rpc, self.test_node, block2, with_witness=True,accepted=False)

        # Advancing one more block should allow the spend.
        self.nodes[0].generate(1, self.signblockprivkeys)
        block2 = self.build_next_block()
        self.update_witness_block_with_transactions(block2, [spend_tx])
        test_witness_block(self.nodes[0].rpc, self.test_node, block2, with_witness=True,accepted=False)
        test_witness_block(self.nodes[0].rpc, self.test_node, block2, with_witness=False,accepted=True)
        sync_blocks(self.nodes)

    @subtest
    def test_uncompressed_pubkey(self):
        """Test uncompressed pubkey validity in segwit transactions.

        Uncompressed pubkeys are no longer supported in default relay policy,
        but (for now) are still valid in blocks."""

        # Segwit transactions using uncompressed pubkeys are not accepted
        # under default policy, but should still pass consensus.
        key = CECKey()
        key.set_secretbytes(b"9")
        key.set_compressed(False)
        pubkey = CPubKey(key.get_pubkey())
        assert_equal(len(pubkey), 65)  # This should be an uncompressed pubkey

        utxo = self.utxo.pop(0)

        # Test 1: P2WPKH
        # First create a P2WPKH output that uses an uncompressed pubkey
        pubkeyhash = hash160(pubkey)
        script_pkh = CScript([OP_0, pubkeyhash])
        tx = CTransaction()
        tx.vin.append(CTxIn(COutPoint(utxo.sha256, utxo.n), b""))
        tx.vout.append(CTxOut(utxo.nValue - 1000, script_pkh))
        tx.rehash()

        # Confirm it in a block.
        block = self.build_next_block()
        self.update_witness_block_with_transactions(block, [tx])
        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=True,accepted=False)
        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=False,accepted=True)

        # Now try to spend it. Send it to a P2WSH output, which we'll
        # use in the next test.
        witness_program = CScript([pubkey, CScriptOp(OP_CHECKSIG)])
        witness_hash = sha256(witness_program)
        script_wsh = CScript([OP_0, witness_hash])

        tx2 = CTransaction()
        tx2.vin.append(CTxIn(COutPoint(tx.malfixsha256, 0), b""))
        tx2.vout.append(CTxOut(tx.vout[0].nValue - 1000, script_wsh))
        script = get_p2pkh_script(pubkeyhash)
        sig_hash = SegwitVersion1SignatureHash(script, tx2, 0, SIGHASH_ALL, tx.vout[0].nValue)
        signature = key.sign(sig_hash) + b'\x01'  # 0x1 is SIGHASH_ALL
        tx2.wit.vtxinwit.append(CTxInWitness())
        tx2.wit.vtxinwit[0].scriptWitness.stack = [signature, pubkey]
        tx2.rehash()

        # Should pass policy test.
        test_transaction_acceptance(self.nodes[0].rpc, self.test_node, tx2, with_witness=True, accepted=False)
        test_transaction_acceptance(self.nodes[0].rpc, self.test_node, tx2, with_witness=False, accepted=True)

        test_transaction_acceptance(self.nodes[1].rpc, self.std_node, tx2, with_witness=True, accepted=False)
        test_transaction_acceptance(self.nodes[1].rpc, self.std_node, tx2, with_witness=False, accepted=False, reason=b'scriptpubkey')

        # and passes consensus.
        block = self.build_next_block()
        self.update_witness_block_with_transactions(block, [tx2])
        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=True,accepted=False)
        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=False,accepted=True)

        # Test 2: P2WSH
        # Try to spend the P2WSH output created in last test.
        # Send it to a P2SH(P2WSH) output, which we'll use in the next test.
        p2sh_witness_hash = hash160(script_wsh)
        script_p2sh = CScript([OP_HASH160, p2sh_witness_hash, OP_EQUAL])
        script_sig = CScript([script_wsh])

        tx3 = CTransaction()
        tx3.vin.append(CTxIn(COutPoint(tx2.malfixsha256, 0), b""))
        tx3.vout.append(CTxOut(tx2.vout[0].nValue - 1000, script_p2sh))
        tx3.wit.vtxinwit.append(CTxInWitness())
        sign_p2pk_witness_input(witness_program, tx3, 0, SIGHASH_ALL, tx2.vout[0].nValue, key)

        # Should pass policy test.
        test_transaction_acceptance(self.nodes[0].rpc, self.test_node, tx3, with_witness=True, accepted=False)
        test_transaction_acceptance(self.nodes[0].rpc, self.test_node, tx3, with_witness=False, accepted=True)

        test_transaction_acceptance(self.nodes[1].rpc, self.std_node, tx3, with_witness=True, accepted=False)
        test_transaction_acceptance(self.nodes[1].rpc, self.std_node, tx3, with_witness=False, accepted=True)

        # and passes consensus.
        block = self.build_next_block()
        self.update_witness_block_with_transactions(block, [tx3])
        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=True,accepted=False)
        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=False,accepted=True)

        # Test 3: P2SH(P2WSH)
        # Try to spend the P2SH output created in the last test.
        # Send it to a P2PKH output, which we'll use in the next test.
        script_pubkey = get_p2pkh_script(pubkeyhash)
        tx4 = CTransaction()
        tx4.vin.append(CTxIn(COutPoint(tx3.malfixsha256, 0), script_sig))
        tx4.vout.append(CTxOut(tx3.vout[0].nValue - 1000, script_pubkey))
        tx4.wit.vtxinwit.append(CTxInWitness())
        sign_p2pk_witness_input(witness_program, tx4, 0, SIGHASH_ALL, tx3.vout[0].nValue, key)

        # Should pass policy test.
        test_transaction_acceptance(self.nodes[0].rpc, self.test_node, tx4, with_witness=True, accepted=False)
        test_transaction_acceptance(self.nodes[0].rpc, self.test_node, tx4, with_witness=False, accepted=True)

        test_transaction_acceptance(self.nodes[1].rpc, self.std_node, tx4, with_witness=True, accepted=False)
        test_transaction_acceptance(self.nodes[1].rpc, self.std_node, tx4, with_witness=False, accepted=True)

        block = self.build_next_block()
        self.update_witness_block_with_transactions(block, [tx4])
        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=True,accepted=False)
        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=False,accepted=True)

        # Test 4: Uncompressed pubkeys should still be valid in non-segwit
        # transactions.
        tx5 = CTransaction()
        tx5.vin.append(CTxIn(COutPoint(tx4.malfixsha256, 0), b""))
        tx5.vout.append(CTxOut(tx4.vout[0].nValue - 1000, CScript([OP_TRUE])))
        (sig_hash, err) = SignatureHash(script_pubkey, tx5, 0, SIGHASH_ALL)
        signature = key.sign(sig_hash) + b'\x01'  # 0x1 is SIGHASH_ALL
        tx5.vin[0].scriptSig = CScript([signature, pubkey])
        tx5.rehash()
        # Should pass policy and consensus.
        test_transaction_acceptance(self.nodes[0].rpc, self.test_node, tx5, with_witness=True, accepted=True)
        block = self.build_next_block()
        self.update_witness_block_with_transactions(block, [tx5])
        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=True,accepted=False)
        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=False,accepted=True)
        self.utxo.append(UTXO(tx5.malfixsha256, 0, tx5.vout[0].nValue))

    @subtest
    def test_signature_version_1(self):

        key = CECKey()
        key.set_secretbytes(b"9")
        pubkey = CPubKey(key.get_pubkey())

        witness_program = CScript([pubkey, CScriptOp(OP_CHECKSIG)])
        witness_hash = sha256(witness_program)
        script_pubkey = CScript([OP_0, witness_hash])

        # First create a witness output for use in the tests.
        tx = CTransaction()
        tx.vin.append(CTxIn(COutPoint(self.utxo[0].sha256, self.utxo[0].n), b""))
        tx.vout.append(CTxOut(self.utxo[0].nValue - 1000, script_pubkey))
        tx.rehash()

        test_transaction_acceptance(self.nodes[0].rpc, self.test_node, tx, with_witness=True, accepted=True)
        # Mine this transaction in preparation for following tests.
        block = self.build_next_block()
        self.update_witness_block_with_transactions(block, [tx])
        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=True, accepted=False)
        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=False, accepted=True)
        sync_blocks(self.nodes)
        self.utxo.pop(0)

        # Test each hashtype
        prev_utxo = UTXO(tx.malfixsha256, 0, tx.vout[0].nValue)
        for sigflag in [0, SIGHASH_ANYONECANPAY]:
            for hashtype in [SIGHASH_ALL, SIGHASH_NONE, SIGHASH_SINGLE]:
                hashtype |= sigflag
                block = self.build_next_block()
                tx = CTransaction()
                tx.vin.append(CTxIn(COutPoint(prev_utxo.sha256, prev_utxo.n), b""))
                tx.vout.append(CTxOut(prev_utxo.nValue - 1000, script_pubkey))
                tx.wit.vtxinwit.append(CTxInWitness())

                # Now try correct value
                sign_p2pk_witness_input(witness_program, tx, 0, hashtype, prev_utxo.nValue, key)
                block.vtx.pop()
                self.update_witness_block_with_transactions(block, [tx])
                test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=True, accepted=False)
                test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=False, accepted=False)

                block = self.build_next_block()
                self.update_witness_block_with_transactions(block, [tx], with_witness=False)
                test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=False, accepted=True)

                prev_utxo = UTXO(tx.malfixsha256, 0, tx.vout[0].nValue)

        # Test combinations of signature hashes.
        # Split the utxo into a lot of outputs.
        # Randomly choose up to 10 to spend, sign with different hashtypes, and
        # output to a random number of outputs.  Repeat NUM_SIGHASH_TESTS times.
        # Ensure that we've tested a situation where we use SIGHASH_SINGLE with
        # an input index > number of outputs.
        NUM_SIGHASH_TESTS = 500
        temp_utxos = []
        tx = CTransaction()
        tx.vin.append(CTxIn(COutPoint(prev_utxo.sha256, prev_utxo.n), b""))
        split_value = prev_utxo.nValue // NUM_SIGHASH_TESTS
        for i in range(NUM_SIGHASH_TESTS):
            tx.vout.append(CTxOut(split_value, script_pubkey))
        tx.wit.vtxinwit.append(CTxInWitness())
        sign_p2pk_witness_input(witness_program, tx, 0, SIGHASH_ALL, prev_utxo.nValue, key)
        for i in range(NUM_SIGHASH_TESTS):
            temp_utxos.append(UTXO(tx.malfixsha256, i, split_value))

        block = self.build_next_block()
        self.update_witness_block_with_transactions(block, [tx])
        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=True, accepted=False)
        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=False, accepted=True)

        block = self.build_next_block()
        used_sighash_single_out_of_bounds = False
        for i in range(NUM_SIGHASH_TESTS):
            # Ping regularly to keep the connection alive
            if (not i % 100):
                self.test_node.sync_with_ping()
            # Choose random number of inputs to use.
            num_inputs = random.randint(1, 10)
            # Create a slight bias for producing more utxos
            num_outputs = random.randint(1, 11)
            random.shuffle(temp_utxos)
            assert(len(temp_utxos) > num_inputs)
            tx = CTransaction()
            total_value = 0
            for i in range(num_inputs):
                tx.vin.append(CTxIn(COutPoint(temp_utxos[i].sha256, temp_utxos[i].n), b""))
                tx.wit.vtxinwit.append(CTxInWitness())
                total_value += temp_utxos[i].nValue
            split_value = total_value // num_outputs
            for i in range(num_outputs):
                tx.vout.append(CTxOut(split_value, script_pubkey))
            for i in range(num_inputs):
                # Now try to sign each input, using a random hashtype.
                anyonecanpay = 0
                if random.randint(0, 1):
                    anyonecanpay = SIGHASH_ANYONECANPAY
                hashtype = random.randint(1, 3) | anyonecanpay
                sign_p2pk_witness_input(witness_program, tx, i, hashtype, temp_utxos[i].nValue, key)
                if (hashtype == SIGHASH_SINGLE and i >= num_outputs):
                    used_sighash_single_out_of_bounds = True
            tx.rehash()
            for i in range(num_outputs):
                temp_utxos.append(UTXO(tx.malfixsha256, i, split_value))
            temp_utxos = temp_utxos[num_inputs:]

            block.vtx.append(tx)

            # Test the block periodically, if we're close to maxblocksize
            if (get_virtual_size(block) > MAX_BLOCK_BASE_SIZE - 1000):
                self.update_witness_block_with_transactions(block, [])
                test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=True, accepted=False)
                test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=False, accepted=True)
                block = self.build_next_block()

        if (not used_sighash_single_out_of_bounds):
            self.log.info("WARNING: this test run didn't attempt SIGHASH_SINGLE with out-of-bounds index value")
        # Test the transactions we've added to the block
        if (len(block.vtx) > 1):
            self.update_witness_block_with_transactions(block, [])
            test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=True, accepted=False)
            test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=False, accepted=True)

        # Now test witness version 0 P2PKH transactions
        pubkeyhash = hash160(pubkey)
        script_pkh = CScript([OP_0, pubkeyhash])
        tx = CTransaction()
        tx.vin.append(CTxIn(COutPoint(temp_utxos[0].sha256, temp_utxos[0].n), b""))
        tx.vout.append(CTxOut(temp_utxos[0].nValue, script_pkh))
        tx.wit.vtxinwit.append(CTxInWitness())
        sign_p2pk_witness_input(witness_program, tx, 0, SIGHASH_ALL, temp_utxos[0].nValue, key)
        tx2 = CTransaction()
        tx2.vin.append(CTxIn(COutPoint(tx.malfixsha256, 0), b""))
        tx2.vout.append(CTxOut(tx.vout[0].nValue, CScript([OP_TRUE])))

        script = get_p2pkh_script(pubkeyhash)
        sig_hash = SegwitVersion1SignatureHash(script, tx2, 0, SIGHASH_ALL, tx.vout[0].nValue)
        signature = key.sign(sig_hash) + b'\x01'  # 0x1 is SIGHASH_ALL

        # Check that we can have a scriptSig
        tx2.vin[0].scriptSig = CScript([signature, pubkey])
        block = self.build_next_block()
        self.update_witness_block_with_transactions(block, [tx, tx2])
        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=True, accepted=False)
        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=False, accepted=True)

        temp_utxos.pop(0)

        # Update self.utxos for later tests by creating two outputs
        # that consolidate all the coins in temp_utxos.
        output_value = sum(i.nValue for i in temp_utxos) // 2

        tx = CTransaction()
        index = 0
        # Just spend to our usual anyone-can-spend output
        tx.vout = [CTxOut(output_value, CScript([OP_TRUE]))] * 2
        for i in temp_utxos:
            # Use SIGHASH_ALL|SIGHASH_ANYONECANPAY so we can build up
            # the signatures as we go.
            tx.vin.append(CTxIn(COutPoint(i.sha256, i.n), b""))
            tx.wit.vtxinwit.append(CTxInWitness())
            sign_p2pk_witness_input(witness_program, tx, index, SIGHASH_ALL | SIGHASH_ANYONECANPAY, i.nValue, key)
            index += 1
        block = self.build_next_block()
        self.update_witness_block_with_transactions(block, [tx])
        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=True, accepted=False)
        test_witness_block(self.nodes[0].rpc, self.test_node, block, with_witness=False, accepted=True)

        for i in range(len(tx.vout)):
            self.utxo.append(UTXO(tx.malfixsha256, i, tx.vout[i].nValue))

    @subtest
    def test_non_standard_witness_blinding(self):
        """Test behavior of unnecessary witnesses in transactions does not blind the node for the transaction"""

        # Create a p2sh output -- this is so we can pass the standardness
        # rules (an anyone-can-spend OP_TRUE would be rejected, if not wrapped
        # in P2SH).
        p2sh_program = CScript([OP_TRUE])
        p2sh_pubkey = hash160(p2sh_program)
        script_pubkey = CScript([OP_HASH160, p2sh_pubkey, OP_EQUAL])

        # Now check that unnecessary witnesses can't be used to blind a node
        # to a transaction, eg by violating standardness checks.
        tx = CTransaction()
        tx.vin.append(CTxIn(COutPoint(self.utxo[0].sha256, self.utxo[0].n), b""))
        tx.vout.append(CTxOut(self.utxo[0].nValue - 1000, script_pubkey))
        tx.rehash()
        test_transaction_acceptance(self.nodes[0].rpc, self.test_node, tx, with_witness=False, accepted=True)
        self.nodes[0].generate(1, self.signblockprivkeys)
        sync_blocks(self.nodes)

        # We'll add an unnecessary witness to this transaction that would cause
        # it to be non-standard, to test that violating policy with a witness
        # doesn't blind a node to a transaction.  Transactions
        # rejected for having a witness shouldn't be added
        # to the rejection cache.
        tx2 = CTransaction()
        tx2.vin.append(CTxIn(COutPoint(tx.malfixsha256, 0), CScript([p2sh_program])))
        tx2.vout.append(CTxOut(tx.vout[0].nValue - 1000, script_pubkey))
        tx2.wit.vtxinwit.append(CTxInWitness())
        tx2.wit.vtxinwit[0].scriptWitness.stack = [b'a' * 400]
        tx2.rehash()
        # This will be rejected due to a policy check:
        # No witness is allowed, since it is not a witness program but a p2sh program
        test_transaction_acceptance(self.nodes[1].rpc, self.std_node, tx2, with_witness=True, accepted=False)

        # If we send without witness, it should be accepted.
        test_transaction_acceptance(self.nodes[1].rpc, self.std_node, tx2, with_witness=False, accepted=True)

        # Now create a new anyone-can-spend utxo for the next test.
        tx3 = CTransaction()
        tx3.vin.append(CTxIn(COutPoint(tx2.malfixsha256, 0), CScript([p2sh_program])))
        tx3.vout.append(CTxOut(tx2.vout[0].nValue - 1000, CScript([OP_TRUE, OP_DROP] * 15 + [OP_TRUE])))
        tx3.rehash()
        test_transaction_acceptance(self.nodes[0].rpc, self.test_node, tx2, with_witness=True, accepted=True)

        test_transaction_acceptance(self.nodes[0].rpc, self.test_node, tx3, with_witness=True, accepted=True)

        self.nodes[0].generate(1, self.signblockprivkeys)
        sync_blocks(self.nodes)

        # Update our utxo list; we spent the first entry.
        self.utxo.pop(0)
        self.utxo.append(UTXO(tx3.malfixsha256, 0, tx3.vout[0].nValue))

    @subtest
    def test_non_standard_witness(self):
        """Test detection of non-standard P2WSH witness"""
        pad = chr(1).encode('latin-1')

        # Create scripts for tests
        scripts = []
        scripts.append(CScript([OP_DROP] * 100))
        scripts.append(CScript([OP_DROP] * 99))
        scripts.append(CScript([pad * 59] * 59 + [OP_DROP] * 60))
        scripts.append(CScript([pad * 59] * 59 + [OP_DROP] * 61))

        p2wsh_scripts = []

        tx = CTransaction()
        tx.vin.append(CTxIn(COutPoint(self.utxo[0].sha256, self.utxo[0].n), b""))

        # For each script, generate a pair of P2WSH and P2SH-P2WSH output.
        outputvalue = (self.utxo[0].nValue - 1000) // (len(scripts) * 2)
        for i in scripts:
            p2wsh = CScript([OP_0, sha256(i)])
            p2sh = hash160(p2wsh)
            p2wsh_scripts.append(p2wsh)
            tx.vout.append(CTxOut(outputvalue, p2wsh))
            tx.vout.append(CTxOut(outputvalue, CScript([OP_HASH160, p2sh, OP_EQUAL])))
        tx.rehash()
        txid = tx.malfixsha256
        test_transaction_acceptance(self.nodes[0].rpc, self.test_node, tx, with_witness=True, accepted=True)

        self.nodes[0].generate(1, self.signblockprivkeys)
        sync_blocks(self.nodes)

        # Creating transactions for tests
        p2wsh_txs = []
        p2sh_txs = []
        for i in range(len(scripts)):
            p2wsh_tx = CTransaction()
            p2wsh_tx.vin.append(CTxIn(COutPoint(txid, i * 2)))
            p2wsh_tx.vout.append(CTxOut(outputvalue - 5000, CScript([OP_0, hash160(hex_str_to_bytes(""))])))
            p2wsh_tx.wit.vtxinwit.append(CTxInWitness())
            p2wsh_tx.rehash()
            p2wsh_txs.append(p2wsh_tx)
            p2sh_tx = CTransaction()
            p2sh_tx.vin.append(CTxIn(COutPoint(txid, i * 2 + 1), CScript([p2wsh_scripts[i]])))
            p2sh_tx.vout.append(CTxOut(outputvalue - 5000, CScript([OP_0, hash160(hex_str_to_bytes(""))])))
            p2sh_tx.wit.vtxinwit.append(CTxInWitness())
            p2sh_tx.rehash()
            p2sh_txs.append(p2sh_tx)

        # Testing native P2WSH
        # Witness stack size, excluding witnessScript, over 100 is non-standard
        p2wsh_txs[0].wit.vtxinwit[0].scriptWitness.stack = [pad] * 101 + [scripts[0]]
        test_transaction_acceptance(self.nodes[1].rpc, self.std_node, p2wsh_txs[0], with_witness=True, accepted=False)
        test_transaction_acceptance(self.nodes[1].rpc, self.std_node, p2wsh_txs[0], with_witness=False, accepted=False, reason=b'scriptpubkey')
        # Non-standard nodes should accept
        test_transaction_acceptance(self.nodes[0].rpc, self.test_node, p2wsh_txs[0], with_witness=True, accepted=False)
        test_transaction_acceptance(self.nodes[0].rpc, self.test_node, p2wsh_txs[0], with_witness=False, accepted=True)

        # Stack element size over 80 bytes is non-standard
        p2wsh_txs[1].wit.vtxinwit[0].scriptWitness.stack = [pad * 81] * 100 + [scripts[1]]
        test_transaction_acceptance(self.nodes[1].rpc, self.std_node, p2wsh_txs[1], with_witness=True, accepted=False)
        test_transaction_acceptance(self.nodes[1].rpc, self.std_node, p2wsh_txs[1], with_witness=False, accepted=False, reason=b'scriptpubkey')
        # Non-standard nodes should accept
        test_transaction_acceptance(self.nodes[0].rpc, self.test_node, p2wsh_txs[1], with_witness=False, accepted=True)

        # Standard nodes should accept if element size is not over 80 bytes
        p2wsh_txs[1].wit.vtxinwit[0].scriptWitness.stack = [pad * 80] * 100 + [scripts[1]]
        test_transaction_acceptance(self.nodes[1].rpc, self.std_node, p2wsh_txs[1], with_witness=False, accepted=False, reason=b'scriptpubkey')

        # witnessScript size at 3600 bytes is standard
        p2wsh_txs[2].wit.vtxinwit[0].scriptWitness.stack = [pad, pad, scripts[2]]
        test_transaction_acceptance(self.nodes[0].rpc, self.test_node, p2wsh_txs[2], with_witness=True, accepted=False)
        test_transaction_acceptance(self.nodes[0].rpc, self.test_node, p2wsh_txs[2], with_witness=False, accepted=True)

        test_transaction_acceptance(self.nodes[1].rpc, self.std_node, p2wsh_txs[2], with_witness=True, accepted=False)
        test_transaction_acceptance(self.nodes[1].rpc, self.std_node, p2wsh_txs[2], with_witness=False, accepted=False, reason=b'scriptpubkey')

        # witnessScript size at 3601 bytes is non-standard
        p2wsh_txs[3].wit.vtxinwit[0].scriptWitness.stack = [pad, pad, pad, scripts[3]]
        test_transaction_acceptance(self.nodes[1].rpc, self.std_node, p2wsh_txs[3], with_witness=True, accepted=False)
        test_transaction_acceptance(self.nodes[1].rpc, self.std_node, p2wsh_txs[3], with_witness=False, accepted=False, reason=b'scriptpubkey')
        # Non-standard nodes should accept
        test_transaction_acceptance(self.nodes[0].rpc, self.test_node, p2wsh_txs[3], with_witness=True, accepted=False)
        test_transaction_acceptance(self.nodes[0].rpc, self.test_node, p2wsh_txs[3], with_witness=False, accepted=True)

        # Repeating the same tests with P2SH-P2WSH
        p2sh_txs[0].wit.vtxinwit[0].scriptWitness.stack = [pad] * 101 + [scripts[0]]
        test_transaction_acceptance(self.nodes[1].rpc, self.std_node, p2sh_txs[0], with_witness=True, accepted=False)
        test_transaction_acceptance(self.nodes[1].rpc, self.std_node, p2sh_txs[0], with_witness=False, accepted=False, reason=b'scriptpubkey')

        test_transaction_acceptance(self.nodes[0].rpc, self.test_node, p2sh_txs[0], with_witness=True, accepted=False)
        test_transaction_acceptance(self.nodes[0].rpc, self.test_node, p2sh_txs[0], with_witness=False, accepted=True)

        p2sh_txs[1].wit.vtxinwit[0].scriptWitness.stack = [pad * 81] * 100 + [scripts[1]]
        test_transaction_acceptance(self.nodes[1].rpc, self.std_node, p2sh_txs[1], with_witness=True, accepted=False)
        test_transaction_acceptance(self.nodes[1].rpc, self.std_node, p2sh_txs[1], with_witness=False, accepted=False, reason=b'scriptpubkey')

        test_transaction_acceptance(self.nodes[0].rpc, self.test_node, p2sh_txs[1], with_witness=True, accepted=False)
        test_transaction_acceptance(self.nodes[0].rpc, self.test_node, p2sh_txs[1], with_witness=False, accepted=True)

        p2sh_txs[1].wit.vtxinwit[0].scriptWitness.stack = [pad * 80] * 100 + [scripts[1]]
        test_transaction_acceptance(self.nodes[1].rpc, self.std_node, p2sh_txs[1], with_witness=True, accepted=False)
        test_transaction_acceptance(self.nodes[1].rpc, self.std_node, p2sh_txs[1], with_witness=False, accepted=False)

        p2sh_txs[2].wit.vtxinwit[0].scriptWitness.stack = [pad, pad, scripts[2]]
        test_transaction_acceptance(self.nodes[0].rpc, self.test_node, p2sh_txs[2], with_witness=True, accepted=False)
        test_transaction_acceptance(self.nodes[0].rpc, self.test_node, p2sh_txs[2], with_witness=False, accepted=True)

        test_transaction_acceptance(self.nodes[1].rpc, self.std_node, p2sh_txs[2], with_witness=True, accepted=False)
        test_transaction_acceptance(self.nodes[1].rpc, self.std_node, p2sh_txs[2], with_witness=False, accepted=False, reason=b'scriptpubkey')

        p2sh_txs[3].wit.vtxinwit[0].scriptWitness.stack = [pad, pad, pad, scripts[3]]
        test_transaction_acceptance(self.nodes[1].rpc, self.std_node, p2sh_txs[3], with_witness=True, accepted=False)
        test_transaction_acceptance(self.nodes[1].rpc, self.std_node, p2sh_txs[3], with_witness=False, accepted=False, reason=b'scriptpubkey')

        test_transaction_acceptance(self.nodes[0].rpc, self.test_node, p2sh_txs[3], with_witness=True, accepted=False)
        test_transaction_acceptance(self.nodes[0].rpc, self.test_node, p2sh_txs[3], with_witness=False, accepted=True)

        self.nodes[0].generate(1, self.signblockprivkeys)  # Mine and clean up the mempool of non-standard node
        # Valid but non-standard transactions in a block should be accepted by standard node
        sync_blocks(self.nodes)
        assert_equal(len(self.nodes[0].getrawmempool()), 0)
        assert_equal(len(self.nodes[1].getrawmempool()), 0)

        self.utxo.pop(0)
        self.utxo.append(UTXO(p2wsh_txs[0].malfixsha256, 0, p2wsh_txs[0].vout[0].nValue))

    @subtest
    def test_upgrade_after_activation(self):
        """Test the behavior of starting up a segwit-aware node after the softfork has activated."""

        # Restart with the new binary
        self.stop_node(2)
        self.start_node(2, extra_args=[])
        connect_nodes(self.nodes[0], 2)

        sync_blocks(self.nodes)

        # Make sure this peer's blocks match those of node0.
        height = self.nodes[2].getblockcount()
        while height >= 0:
            block_hash = self.nodes[2].getblockhash(height)
            assert_equal(block_hash, self.nodes[0].getblockhash(height))
            assert_equal(self.nodes[0].getblock(block_hash), self.nodes[2].getblock(block_hash))
            height -= 1

    @subtest
    def test_witness_sigops(self):
        """Test sigop counting is correct inside witnesses."""

        # Keep this under MAX_OPS_PER_SCRIPT (201)
        witness_program = CScript([OP_TRUE, OP_IF, OP_TRUE, OP_ELSE] + [OP_CHECKMULTISIG] * 5 + [OP_CHECKSIG] * 193 + [OP_ENDIF])
        witness_hash = sha256(witness_program)
        script_pubkey = CScript([OP_0, witness_hash])

        sigops_per_script = 20 * 5 + 193 * 1
        # We'll produce 2 extra outputs, one with a program that would take us
        # over max sig ops, and one with a program that would exactly reach max
        # sig ops
        outputs = (MAX_SIGOP_COST // sigops_per_script) + 2
        extra_sigops_available = MAX_SIGOP_COST % sigops_per_script

        # We chose the number of checkmultisigs/checksigs to make this work:
        assert(extra_sigops_available < 100)  # steer clear of MAX_OPS_PER_SCRIPT

        # This script, when spent with the first
        # N(=MAX_SIGOP_COST//sigops_per_script) outputs of our transaction,
        # would push us just over the block sigop limit.
        witness_program_toomany = CScript([OP_TRUE, OP_IF, OP_TRUE, OP_ELSE] + [OP_CHECKSIG] * (extra_sigops_available + 1) + [OP_ENDIF])
        witness_hash_toomany = sha256(witness_program_toomany)
        script_pubkey_toomany = CScript([OP_0, witness_hash_toomany])

        # If we spend this script instead, we would exactly reach our sigop
        # limit (for witness sigops).
        witness_program_justright = CScript([OP_TRUE, OP_IF, OP_TRUE, OP_ELSE] + [OP_CHECKSIG] * (extra_sigops_available) + [OP_ENDIF])
        witness_hash_justright = sha256(witness_program_justright)
        script_pubkey_justright = CScript([OP_0, witness_hash_justright])

        # First split our available utxo into a bunch of outputs
        split_value = self.utxo[0].nValue // outputs
        tx = CTransaction()
        tx.vin.append(CTxIn(COutPoint(self.utxo[0].sha256, self.utxo[0].n), b""))
        for i in range(outputs):
            tx.vout.append(CTxOut(split_value, script_pubkey))
        tx.vout[-2].scriptPubKey = script_pubkey_toomany
        tx.vout[-1].scriptPubKey = script_pubkey_justright
        tx.rehash()

        block_1 = self.build_next_block()
        self.update_witness_block_with_transactions(block_1, [tx])
        test_witness_block(self.nodes[0].rpc, self.test_node, block_1, with_witness=True, accepted=False)
        test_witness_block(self.nodes[0].rpc, self.test_node, block_1, with_witness=False,accepted=True)

        tx2 = CTransaction()
        # If we try to spend the first n-1 outputs from tx, that should be
        # too many sigops.
        total_value = 0
        for i in range(outputs - 1):
            tx2.vin.append(CTxIn(COutPoint(tx.malfixsha256, i), b""))
            tx2.wit.vtxinwit.append(CTxInWitness())
            tx2.wit.vtxinwit[-1].scriptWitness.stack = [witness_program]
            total_value += tx.vout[i].nValue
        tx2.wit.vtxinwit[-1].scriptWitness.stack = [witness_program_toomany]
        tx2.vout.append(CTxOut(total_value, CScript([OP_TRUE])))
        tx2.rehash()

        block_2 = self.build_next_block()
        self.update_witness_block_with_transactions(block_2, [tx2])
        test_witness_block(self.nodes[0].rpc, self.test_node, block_2, with_witness=True,accepted=False)

        # Try dropping the last input in tx2, and add an output that has
        # too many sigops (contributing to legacy sigop count).
        checksig_count = (extra_sigops_available // 4) + 1
        script_pubkey_checksigs = CScript([OP_CHECKSIG] * checksig_count)
        tx2.vout.append(CTxOut(0, script_pubkey_checksigs))
        tx2.vin.pop()
        tx2.wit.vtxinwit.pop()
        tx2.vout[0].nValue -= tx.vout[-2].nValue
        tx2.rehash()
        block_3 = self.build_next_block()
        self.update_witness_block_with_transactions(block_3, [tx2])
        test_witness_block(self.nodes[0].rpc, self.test_node, block_3, with_witness=True,accepted=False)

        # If we drop the last checksig in this output, the tx should succeed.
        block_4 = self.build_next_block()
        tx2.vout[-1].scriptPubKey = CScript([OP_CHECKSIG] * (checksig_count - 1))
        tx2.rehash()
        self.update_witness_block_with_transactions(block_4, [tx2], with_witness=True)
        test_witness_block(self.nodes[0].rpc, self.test_node, block_4, with_witness=True,accepted=False)
        test_witness_block(self.nodes[0].rpc, self.test_node, block_4, with_witness=False,accepted=True)

        # Reset the tip back down for the next test
        sync_blocks(self.nodes)
        for x in self.nodes:
            x.invalidateblock(block_4.hash)

        # Try replacing the last input of tx2 to be spending the last
        # output of tx
        block_5 = self.build_next_block()
        tx2.vout.pop()
        tx2.vin.append(CTxIn(COutPoint(tx.malfixsha256, outputs - 1), b""))
        tx2.wit.vtxinwit.append(CTxInWitness())
        tx2.wit.vtxinwit[-1].scriptWitness.stack = [witness_program_justright]
        tx2.rehash()
        self.update_witness_block_with_transactions(block_5, [tx2])
        test_witness_block(self.nodes[0].rpc, self.test_node, block_5, with_witness=True,accepted=False)
        test_witness_block(self.nodes[0].rpc, self.test_node, block_5, with_witness=False,accepted=True)

        # TODO: test p2sh sigop counting

if __name__ == '__main__':
    SegWitTest().main()
