Signed-Blocks Specification
===========================

Tapyrus uses Signed-Blocks as block creation algorithm. Signed Blocks is
based on Federated Blocksigning in a Strong Federation which is proposed
[Strong Federations: An Interoperable Blockchain Solution to Centralized Third Party Risks](https://arxiv.org/pdf/1612.05491v2.pdf).
In this algorithm, the blocks are poroposed in round-robin by a federation
member. If the blocks succeeds in collecting a minimum of 'threshold‘
signatures from other federation members, it is added as the next block.
The block can be verified by all public nodes.


Arguments for Signed-Blocks
===========================

Signed-Block needs some parameters. The first is public keys from each
federation member. And 2nd is threshold number for multi-signature.
Last is genesis block data which needs to be signed and the number of
signatures must exceed ‘threshold’ .

You can set public keys and threshold as tapyrusd arguments.

```
  -signblockpubkeys=<pubkeys>
       Sets the public keys for Signed Blocks multisig that combined as one
       string.

  -signblockthreshold=<n>
       Sets the number of public keys to be the threshold of multisig
```

To setting genesis block data, you need to create dat file at network
data directory. If you set `/var/lib/tapyrus` is data directory, you
should put on `/var/lib/tapyrus/genesis.dat`.

Block Structure Expansion for Signed-Blocks
===========================================

Tapyrus block header has proof field and bits and nonce are removed. So,
Tapyrus block header structure is like this.

 Name | Type | Size(Bytes) | Description
------|------|-------------|-------------
Version | int32_t | 4 | Block version number
hasPrevBlock | char\[32\] | 32 | 256-bit hash of the previous block header
hashMerkleRoot | char\[32\] | 32 | 256-bit hash based on all of the transactions in the block
hashImMerkleRoot | char\[32\] | 32 | MerkleRoot based on fixing malleability transaction hash. More details in doc/fix_transaction_malleability.md
Time | uint32_t | 4 | Current block timestamp as seconds since 1970-01-01T00:00 UTC
Proof | Proof | ? | Collection holds signatures for block hash which is consisted of block header without Proof.

And the proof structure is like this.

 Name | Type | Size(Bytes) | Description
------|------|-------------|-------------
Signatures count | [VarInt](https://en.bitcoin.it/wiki/Protocol_documentation#Variable_length_integer) | 1+ | Count of Signatures |
Signatures | Signature[] | ? | Array of Signature

Signature Structure

 Name | Type | Size(Bytes) | Description
------|------|-------------|-------------
Signature Length | [VarInt](https://en.bitcoin.it/wiki/Protocol_documentation#Variable_length_integer) | 1+ |
Signature | char\[\] | ? | ECDSA Signature for block header hash. Formatted as DER encoding.

Tapyrus Chain has Signed-Blocks Arguments
=========================================

Genesis Block coinbase tx's script sig has threshold and public keys.
The public key is the sum of all public keys are added up.

Example:

```
[02] [0276d7a1b83fd97b8751d2b82e1f7a89dcd23a5b3f5cc2798083ad6f775018102e]
```

`[02]` is threshold, the following part is public key that are added up all keys.

RPC collection for Signer Nodes
===============================

Tapyrus Core provides some RPCs for Signer Nodes. These are useful for
block signing process which are creating candidate blocks, testing blocks
which proposed by other member, signing blocks.

## getnewblock

```
getnewblock

Gets hex representation of a proposed, unmined new block

Arguments:
1. address         (string, required) The address to send fees and the newly generated coin.
2. required_age    (numeric, optional, default=0) How many seconds a transaction must have been in the mempool to be inluded in the block proposal. This may help with faster block convergence among functionaries using compact blocks.

Result
blockhex      (hex) The block hex

Examples:
> tapyrus-cli getnewblock
```


## testproposedblock

```
testproposedblock "blockhex" "[acceptnonstdtxn]"

Validate proposed block before signing

Arguments:
1. "blockhex"       (string, required) The hex-encoded block from getnewblockhex
2. "acceptnonstdtxn" (bool) flag indicating whether the block validation must accept non-standard transactions

Result
"valid"              (bool) true when the block is valid, JSON exception on failure

Examples:
> tapyrus-cli testproposedblock <blockhex> [acceptnonstdtxn]
```


## combineblocksigs

```
combineblocksigs "blockhex" [{"signature"},...]

Merges signatures on a block proposal

Arguments:
1. "blockhex"       (string, required) The hex-encoded block from getnewblockhex
2. "signatures"     (string) A json array of signatures
    [
      "signature"   (string) A signature (in the form of a hex-encoded scriptSig)
      ,...
    ]

Result
{
  "hex": "value",       (string) The signed block
  "complete": n           (numeric) if block is complete
  "warning": "message"  (string) diagnostic message stating why signatures were not added to a block
}

Examples:
> tapyrus-cli combineblocksigs <hex> ["signature1", "signature2", ...]
```

## submitblock


```
submitblock "hexdata"  ( "dummy" )

Attempts to submit new block to network.
See https://en.bitcoin.it/wiki/BIP_0022 for full specification.

Arguments
1. "hexdata"        (string, required) the hex-encoded block data to submit
2. "dummy"          (optional) dummy value, for compatibility with BIP22. This value is ignored.

Result:

Examples:
> tapyrus-cli submitblock "mydata"
> curl --user myusername --data-binary '{"jsonrpc": "1.0", "id":"curltest", "method": "submitblock", "params": ["mydata"] }' -H 'content-type: text/plain;' http://127.0.0.1:2377/
```