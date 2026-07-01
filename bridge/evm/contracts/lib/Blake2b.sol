// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title Blake2b
/// @notice Unkeyed BLAKE2b-256, byte-identical to Python `hashlib.blake2b(digest_size=32)`.
/// @dev Built on the EIP-152 compression precompile at address 0x09 (one F-compression per call).
///      THE TRAP IS ENDIANNESS: the precompile's h/m/t words are little-endian uint64, whereas
///      `bytes32`/`abi.encodePacked` are big-endian. The state `h` is kept in logical (big-endian
///      integer) form and byte-swapped on the way in (`_putLE64`) and out (`_swap64`). The message
///      block is copied raw — BLAKE2b's m[i] is already `load64_le(block[8i..])`, exactly the bytes.
library Blake2b {
    // The 8 standard BLAKE2b IV constants (logical uint64 values).
    uint64 private constant IV0 = 0x6a09e667f3bcc908;
    uint64 private constant IV1 = 0xbb67ae8584caa73b;
    uint64 private constant IV2 = 0x3c6ef372fe94f82b;
    uint64 private constant IV3 = 0xa54ff53a5f1d36f1;
    uint64 private constant IV4 = 0x510e527fade682d1;
    uint64 private constant IV5 = 0x9b05688c2b3e6c1f;
    uint64 private constant IV6 = 0x1f83d9abfb41bd6b;
    uint64 private constant IV7 = 0x5be0cd19137e2179;

    // Param block for unkeyed 256-bit digest: digest_len=0x20, key_len=0, fanout=1, depth=1.
    uint64 private constant PARAM0 = 0x01010020;

    uint256 private constant BLOCK_BYTES = 128;

    /// @notice Unkeyed BLAKE2b-256 of `input`.
    /// @return out The 32-byte digest, byte-identical to `hashlib.blake2b(digest_size=32)`.
    function hash256(bytes memory input) internal view returns (bytes32 out) {
        // (1) h = IV; h[0] ^= param block.
        uint64[8] memory h;
        h[0] = IV0 ^ PARAM0;
        h[1] = IV1;
        h[2] = IV2;
        h[3] = IV3;
        h[4] = IV4;
        h[5] = IV5;
        h[6] = IV6;
        h[7] = IV7;

        // (2) Process 128-byte blocks with a running byte counter. Every block but the last:
        //     t += 128, f = 0. The last block: t = total length, f = 1, zero-padded.
        //     Empty input falls straight through to a single final block (offset 0, len 0, t 0, f 1).
        uint256 len = input.length;
        uint256 offset = 0;
        uint256 counter = 0;
        while (len - offset > BLOCK_BYTES) {
            counter += BLOCK_BYTES;
            _compress(h, input, offset, BLOCK_BYTES, counter, 0);
            offset += BLOCK_BYTES;
        }
        _compress(h, input, offset, len - offset, len, 1);

        // (3) Digest = first 32 bytes of h serialized little-endian (byte-swap h[0..3]).
        out = bytes32(
            (uint256(_swap64(h[0])) << 192) |
            (uint256(_swap64(h[1])) << 128) |
            (uint256(_swap64(h[2])) << 64) |
            uint256(_swap64(h[3]))
        );
    }

    /// @dev One EIP-152 F-compression. Builds the EXACT 213-byte precompile input and folds the
    ///      64-byte result back into `h` (mutated in place).
    ///      input = be32(rounds=12) || h(64 LE) || m(128 LE) || t(16 LE) || f(1)
    function _compress(
        uint64[8] memory h,
        bytes memory data,
        uint256 dataOffset,
        uint256 blockLen,
        uint256 t,
        uint8 f
    ) private view {
        bytes memory inp = new bytes(213);
        uint256 d;
        assembly {
            d := add(inp, 32)
        }

        // rounds = 12 as a big-endian uint32 in [0..4); also zeros [4..32).
        assembly {
            mstore(d, shl(224, 12))
        }

        // h little-endian in [4..68).
        for (uint256 i = 0; i < 8; i++) {
            _putLE64(d + 4 + i * 8, h[i]);
        }

        // m in [68..196): zero the whole 128-byte region (calldatacopy past calldatasize == zeros),
        // then copy the raw message bytes for this block over the top (rest stays zero-padded).
        assembly {
            calldatacopy(add(d, 68), calldatasize(), 128)
        }
        if (blockLen > 0) {
            assembly {
                mcopy(add(d, 68), add(add(data, 32), dataOffset), blockLen)
            }
        }

        // t in [196..212): t0 = low 64 bits, t1 = next 64 bits, each little-endian.
        _putLE64(d + 196, uint64(t));
        _putLE64(d + 204, uint64(t >> 64));

        // f in [212].
        assembly {
            mstore8(add(d, 212), f)
        }

        // ONE compression: staticcall 0x09 with the 213-byte input, 64-byte output (overwrites inp).
        assembly {
            if iszero(staticcall(gas(), 0x09, d, 213, d, 64)) {
                revert(0, 0)
            }
        }

        // Read the new state: 8 little-endian uint64 words -> logical h.
        for (uint256 i = 0; i < 8; i++) {
            uint256 p = d + i * 8;
            uint64 le;
            assembly {
                le := shr(192, mload(p))
            }
            h[i] = _swap64(le);
        }
    }

    /// @dev Write `v` as 8 little-endian bytes (LSB first) at memory pointer `ptr`.
    function _putLE64(uint256 ptr, uint64 v) private pure {
        assembly {
            mstore8(ptr, and(v, 0xff))
            mstore8(add(ptr, 1), and(shr(8, v), 0xff))
            mstore8(add(ptr, 2), and(shr(16, v), 0xff))
            mstore8(add(ptr, 3), and(shr(24, v), 0xff))
            mstore8(add(ptr, 4), and(shr(32, v), 0xff))
            mstore8(add(ptr, 5), and(shr(40, v), 0xff))
            mstore8(add(ptr, 6), and(shr(48, v), 0xff))
            mstore8(add(ptr, 7), and(shr(56, v), 0xff))
        }
    }

    /// @dev Reverse the 8 bytes of a uint64 (logical <-> little-endian).
    function _swap64(uint64 x) private pure returns (uint64) {
        x = ((x & 0x00FF00FF00FF00FF) << 8) | ((x & 0xFF00FF00FF00FF00) >> 8);
        x = ((x & 0x0000FFFF0000FFFF) << 16) | ((x & 0xFFFF0000FFFF0000) >> 16);
        x = ((x & 0x00000000FFFFFFFF) << 32) | ((x & 0xFFFFFFFF00000000) >> 32);
        return x;
    }
}
