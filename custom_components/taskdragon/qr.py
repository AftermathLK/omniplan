"""Minimal QR Code (Model 2) encoder for the printable object/task labels.

Byte mode, error correction level M, versions 1–40, standard masking. It
follows ISO/IEC 18004 (and the structure of Project Nayuki's reference
implementation) so no third-party library is needed inside Home Assistant.
The result is a list of rows of booleans (True = dark); the card draws it.
"""

from __future__ import annotations

# Error-correction codewords per block and number of blocks, indexed [level][version].
# Levels: 0 = L, 1 = M, 2 = Q, 3 = H.
_ECC_PER_BLOCK = (
    (-1, 7, 10, 15, 20, 26, 18, 20, 24, 30, 18, 20, 24, 26, 30, 22, 24, 28, 30, 28, 28, 28, 28, 30, 30, 26, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
    (-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26, 26, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28),
    (-1, 13, 22, 18, 26, 18, 24, 18, 22, 20, 24, 28, 26, 24, 20, 30, 24, 28, 28, 26, 30, 28, 30, 30, 30, 30, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
    (-1, 17, 28, 22, 16, 22, 28, 26, 26, 24, 28, 24, 28, 22, 24, 24, 30, 28, 28, 26, 28, 30, 24, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
)
_NUM_BLOCKS = (
    (-1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4, 4, 4, 4, 4, 6, 6, 6, 6, 7, 8, 8, 9, 9, 10, 12, 12, 12, 13, 14, 15, 16, 17, 18, 19, 19, 20, 21, 22, 24, 25),
    (-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16, 17, 17, 18, 20, 21, 23, 25, 26, 28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49),
    (-1, 1, 1, 2, 2, 4, 4, 6, 6, 8, 8, 8, 10, 12, 16, 12, 17, 16, 18, 21, 20, 23, 23, 25, 27, 29, 34, 34, 35, 38, 40, 43, 45, 48, 51, 53, 56, 59, 62, 65, 68),
    (-1, 1, 1, 2, 4, 4, 4, 5, 6, 8, 8, 11, 11, 16, 16, 18, 16, 19, 21, 25, 25, 25, 34, 30, 32, 35, 37, 40, 42, 45, 48, 51, 54, 57, 60, 63, 66, 70, 74, 77, 81),
)
_FORMAT_BITS = (1, 0, 3, 2)  # L, M, Q, H
LEVEL_M = 1


def _raw_modules(version: int) -> int:
    result = (16 * version + 128) * version + 64
    if version >= 2:
        align = version // 7 + 2
        result -= (25 * align - 10) * align - 55
        if version >= 7:
            result -= 36
    return result


def _data_codewords(version: int, level: int) -> int:
    return _raw_modules(version) // 8 - _ECC_PER_BLOCK[level][version] * _NUM_BLOCKS[level][version]


def _gf_multiply(x: int, y: int) -> int:
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree: int) -> list[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_multiply(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_multiply(root, 0x02)
    return result


def _rs_remainder(data: list[int], divisor: list[int]) -> list[int]:
    result = [0] * len(divisor)
    for byte in data:
        factor = byte ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_multiply(coef, factor)
    return result


class _Symbol:
    def __init__(self, version: int, level: int) -> None:
        self.version = version
        self.level = level
        self.size = version * 4 + 17
        self.modules = [[False] * self.size for _ in range(self.size)]
        self.function = [[False] * self.size for _ in range(self.size)]

    def set_function(self, x: int, y: int, dark: bool) -> None:
        self.modules[y][x] = dark
        self.function[y][x] = True

    def alignment_positions(self) -> list[int]:
        if self.version == 1:
            return []
        count = self.version // 7 + 2
        step = (self.version * 8 + count * 3 + 5) // (count * 4 - 4) * 2
        return list(reversed([self.size - 7 - i * step for i in range(count - 1)] + [6]))

    def draw_function_patterns(self) -> None:
        for i in range(self.size):
            self.set_function(6, i, i % 2 == 0)
            self.set_function(i, 6, i % 2 == 0)
        for x, y in ((3, 3), (self.size - 4, 3), (3, self.size - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    xx, yy = x + dx, y + dy
                    if 0 <= xx < self.size and 0 <= yy < self.size:
                        self.set_function(xx, yy, max(abs(dx), abs(dy)) not in (2, 4))
        positions = self.alignment_positions()
        last = len(positions) - 1
        for i, px in enumerate(positions):
            for j, py in enumerate(positions):
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.set_function(px + dx, py + dy, max(abs(dx), abs(dy)) != 1)
        self.draw_format_bits(0)
        self.draw_version()

    def draw_format_bits(self, mask: int) -> None:
        data = _FORMAT_BITS[self.level] << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412

        def bit(i: int) -> bool:
            return (bits >> i) & 1 != 0

        for i in range(6):
            self.set_function(8, i, bit(i))
        self.set_function(8, 7, bit(6))
        self.set_function(8, 8, bit(7))
        self.set_function(7, 8, bit(8))
        for i in range(9, 15):
            self.set_function(14 - i, 8, bit(i))
        for i in range(8):
            self.set_function(self.size - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self.set_function(8, self.size - 15 + i, bit(i))
        self.set_function(8, self.size - 8, True)

    def draw_version(self) -> None:
        if self.version < 7:
            return
        rem = self.version
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        bits = self.version << 12 | rem
        for i in range(18):
            dark = (bits >> i) & 1 != 0
            a, b = self.size - 11 + i % 3, i // 3
            self.set_function(a, b, dark)
            self.set_function(b, a, dark)

    def draw_codewords(self, data: list[int]) -> None:
        index = 0
        right = self.size - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(self.size):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = self.size - 1 - vert if upward else vert
                    if not self.function[y][x] and index < len(data) * 8:
                        self.modules[y][x] = (data[index >> 3] >> (7 - (index & 7))) & 1 != 0
                        index += 1
            right -= 2

    def apply_mask(self, mask: int) -> None:
        tests = (
            lambda x, y: (x + y) % 2 == 0,
            lambda x, y: y % 2 == 0,
            lambda x, y: x % 3 == 0,
            lambda x, y: (x + y) % 3 == 0,
            lambda x, y: (x // 3 + y // 2) % 2 == 0,
            lambda x, y: x * y % 2 + x * y % 3 == 0,
            lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
            lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
        )
        test = tests[mask]
        for y in range(self.size):
            for x in range(self.size):
                if not self.function[y][x] and test(x, y):
                    self.modules[y][x] = not self.modules[y][x]

    def penalty(self) -> int:
        size, modules = self.size, self.modules
        score = 0
        lines = [row for row in modules] + [[modules[y][x] for y in range(size)] for x in range(size)]
        finder_a = [True, False, True, True, True, False, True, False, False, False, False]
        finder_b = list(reversed(finder_a))
        for line in lines:
            run, previous = 0, None
            for module in line:
                if module == previous:
                    run += 1
                    if run == 5:
                        score += 3
                    elif run > 5:
                        score += 1
                else:
                    run, previous = 1, module
            for i in range(len(line) - 10):
                window = line[i : i + 11]
                if window == finder_a or window == finder_b:
                    score += 40
        for y in range(size - 1):
            for x in range(size - 1):
                color = modules[y][x]
                if color == modules[y][x + 1] == modules[y + 1][x] == modules[y + 1][x + 1]:
                    score += 3
        dark = sum(sum(row) for row in modules)
        total = size * size
        score += (abs(dark * 20 - total * 10) + total - 1) // total * 10 - 10 if dark * 20 != total * 10 else 0
        return score


def _encode_data(payload: bytes, version: int, level: int) -> list[int]:
    capacity = _data_codewords(version, level) * 8
    bits: list[int] = []

    def append(value: int, length: int) -> None:
        bits.extend((value >> i) & 1 for i in reversed(range(length)))

    append(0b0100, 4)
    append(len(payload), 8 if version <= 9 else 16)
    for byte in payload:
        append(byte, 8)
    append(0, min(4, capacity - len(bits)))
    append(0, -len(bits) % 8)
    pad = 0xEC
    while len(bits) < capacity:
        append(pad, 8)
        pad ^= 0xEC ^ 0x11
    return [int("".join(map(str, bits[i : i + 8])), 2) for i in range(0, len(bits), 8)]


def _interleave(data: list[int], version: int, level: int) -> list[int]:
    blocks_count = _NUM_BLOCKS[level][version]
    ecc_len = _ECC_PER_BLOCK[level][version]
    raw = _raw_modules(version) // 8
    short_count = blocks_count - raw % blocks_count
    short_len = raw // blocks_count
    divisor = _rs_divisor(ecc_len)
    blocks: list[list[int]] = []
    k = 0
    for i in range(blocks_count):
        chunk = data[k : k + short_len - ecc_len + (0 if i < short_count else 1)]
        k += len(chunk)
        ecc = _rs_remainder(chunk, divisor)
        if i < short_count:
            chunk = chunk + [0]
        blocks.append(chunk + ecc)
    result = []
    for i in range(len(blocks[0])):
        for j, block in enumerate(blocks):
            if i != short_len - ecc_len or j >= short_count:
                result.append(block[i])
    return result


def encode(text: str, level: int = LEVEL_M) -> list[list[bool]]:
    """Encode ``text`` (UTF-8) and return the module matrix (True = dark)."""
    payload = text.encode("utf-8")
    for version in range(1, 41):
        header = 4 + (8 if version <= 9 else 16)
        if header + len(payload) * 8 <= _data_codewords(version, level) * 8:
            break
    else:
        raise ValueError("text is too long for a QR code")
    codewords = _interleave(_encode_data(payload, version, level), version, level)
    best: tuple[int, list[list[bool]]] | None = None
    for mask in range(8):
        symbol = _Symbol(version, level)
        symbol.draw_function_patterns()
        symbol.draw_codewords(codewords)
        symbol.apply_mask(mask)
        symbol.draw_format_bits(mask)
        score = symbol.penalty()
        if best is None or score < best[0]:
            best = (score, [row[:] for row in symbol.modules])
    assert best is not None
    return best[1]


def encode_rows(text: str) -> list[str]:
    """The matrix as strings of 0/1, convenient for JSON."""
    return ["".join("1" if module else "0" for module in row) for row in encode(text)]
