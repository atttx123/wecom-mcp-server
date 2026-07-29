"""Pure-Python AES-256-CBC (FIPS-197) + PKCS7.

避免依赖需要原生编译的 cryptography/pycryptodome。
S-box 通过 GF(2^8) 求逆 + 仿射变换运行时生成，已用 NIST 测试向量验证。
"""


def _xtime(a: int) -> int:
    a <<= 1
    if a & 0x100:
        a ^= 0x11B
    return a & 0xFF


def _gmul(a: int, b: int) -> int:
    res = 0
    for _ in range(8):
        if b & 1:
            res ^= a
        a = _xtime(a)
        b >>= 1
    return res


def _gf_pow(a: int, e: int) -> int:
    res = 1
    while e:
        if e & 1:
            res = _gmul(res, a)
        a = _gmul(a, a)
        e >>= 1
    return res


def _gf_inverse(a: int) -> int:
    return 0 if a == 0 else _gf_pow(a, 254)


def _rotl8(x: int, n: int) -> int:
    return ((x << n) | (x >> (8 - n))) & 0xFF


# S-box / 逆 S-box (运行时生成)
_SBOX = tuple(
    (lambda a: a ^ _rotl8(a, 1) ^ _rotl8(a, 2) ^ _rotl8(a, 3) ^ _rotl8(a, 4) ^ 0x63)(_gf_inverse(i))
    for i in range(256)
)
_INV_SBOX = tuple(_SBOX.index(i) for i in range(256))
_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36, 0x6C, 0xD8, 0xAB, 0x4D)


def _key_expansion(key: bytes) -> list[list[int]]:
    """AES-256 key expansion -> 60 words."""
    Nk, Nr, Nb = 8, 14, 4
    w: list[list[int]] = [list(key[4 * i:4 * i + 4]) for i in range(Nk)]
    for i in range(Nk, Nb * (Nr + 1)):
        temp = list(w[i - 1])
        if i % Nk == 0:
            temp = [_SBOX[b] for b in temp[1:] + temp[:1]]
            temp[0] ^= _RCON[i // Nk - 1]
        elif i % Nk == 4:
            temp = [_SBOX[b] for b in temp]
        w.append([w[i - Nk][j] ^ temp[j] for j in range(4)])
    return w


def _add_round_key(state: list[int], w: list[list[int]], rnd: int) -> list[int]:
    out = state[:]
    col = rnd * 4
    for r in range(4):
        for c in range(4):
            out[r + 4 * c] ^= w[col + c][r]
    return out


def _sub_bytes(state: list[int]) -> list[int]:
    return [_SBOX[b] for b in state]


def _inv_sub_bytes(state: list[int]) -> list[int]:
    return [_INV_SBOX[b] for b in state]


def _shift_rows(state: list[int]) -> list[int]:
    s = state[:]
    s[1], s[5], s[9], s[13] = state[5], state[9], state[13], state[1]
    s[2], s[6], s[10], s[14] = state[10], state[14], state[2], state[6]
    s[3], s[7], s[11], s[15] = state[15], state[3], state[7], state[11]
    return s


def _inv_shift_rows(state: list[int]) -> list[int]:
    s = state[:]
    s[1], s[5], s[9], s[13] = state[13], state[1], state[5], state[9]
    s[2], s[6], s[10], s[14] = state[10], state[14], state[2], state[6]
    s[3], s[7], s[11], s[15] = state[7], state[11], state[15], state[3]
    return s


def _mix_columns(state: list[int]) -> list[int]:
    out = state[:]
    for c in range(4):
        i = 4 * c
        a0, a1, a2, a3 = state[i], state[i + 1], state[i + 2], state[i + 3]
        out[i] = _gmul(a0, 2) ^ _gmul(a1, 3) ^ a2 ^ a3
        out[i + 1] = a0 ^ _gmul(a1, 2) ^ _gmul(a2, 3) ^ a3
        out[i + 2] = a0 ^ a1 ^ _gmul(a2, 2) ^ _gmul(a3, 3)
        out[i + 3] = _gmul(a0, 3) ^ a1 ^ a2 ^ _gmul(a3, 2)
    return out


def _inv_mix_columns(state: list[int]) -> list[int]:
    out = state[:]
    for c in range(4):
        i = 4 * c
        a0, a1, a2, a3 = state[i], state[i + 1], state[i + 2], state[i + 3]
        out[i] = _gmul(a0, 14) ^ _gmul(a1, 11) ^ _gmul(a2, 13) ^ _gmul(a3, 9)
        out[i + 1] = _gmul(a0, 9) ^ _gmul(a1, 14) ^ _gmul(a2, 11) ^ _gmul(a3, 13)
        out[i + 2] = _gmul(a0, 13) ^ _gmul(a1, 9) ^ _gmul(a2, 14) ^ _gmul(a3, 11)
        out[i + 3] = _gmul(a0, 11) ^ _gmul(a1, 13) ^ _gmul(a2, 9) ^ _gmul(a3, 14)
    return out


def _encrypt_block(block: bytes, w: list[list[int]]) -> bytes:
    state = _add_round_key(list(block), w, 0)
    for rnd in range(1, 14):
        state = _add_round_key(_mix_columns(_shift_rows(_sub_bytes(state))), w, rnd)
    return bytes(_add_round_key(_shift_rows(_sub_bytes(state)), w, 14))


def _decrypt_block(block: bytes, w: list[list[int]]) -> bytes:
    state = _add_round_key(list(block), w, 14)
    for rnd in range(13, 0, -1):
        state = _inv_mix_columns(_add_round_key(_inv_sub_bytes(_inv_shift_rows(state)), w, rnd))
    return bytes(_add_round_key(_inv_sub_bytes(_inv_shift_rows(state)), w, 0))


def _pkcs7_pad(data: bytes) -> bytes:
    pad = 16 - (len(data) % 16)
    return data + bytes([pad]) * pad


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data or len(data) % 16 != 0:
        return data
    pad = data[-1]
    if 1 <= pad <= 16 and data[-pad:] == bytes([pad]) * pad:
        return data[:-pad]
    return data


def aes_256_cbc_encrypt(key: bytes, iv: bytes, plaintext: bytes) -> bytes:
    w = _key_expansion(key)
    data = _pkcs7_pad(plaintext)
    out, prev = bytearray(), iv
    for i in range(0, len(data), 16):
        block = bytes(a ^ b for a, b in zip(data[i:i + 16], prev))
        enc = _encrypt_block(block, w)
        out += enc
        prev = enc
    return bytes(out)


def aes_256_cbc_decrypt(key: bytes, iv: bytes, ciphertext: bytes) -> bytes:
    w = _key_expansion(key)
    out, prev = bytearray(), iv
    for i in range(0, len(ciphertext), 16):
        block = ciphertext[i:i + 16]
        dec = _decrypt_block(block, w)
        out += bytes(a ^ b for a, b in zip(dec, prev))
        prev = block
    return _pkcs7_unpad(bytes(out))
