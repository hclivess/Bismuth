"""Python implementation of an Ascii85 encoding

See https://en.wikipedia.org/wiki/Ascii85
Uses classic Ascii85 Algorithm, but a custom alphabet to be used as part of a bismuth bis:// url or json string.
(no / \ " ')

Inspiration from PyZMQ, BSD Licence

Data length to encode must be a multiple of 4, padding with non significant char of #0 has to be added if needed.

"""

import struct

# Custom base alphabet
Z85CHARS = b"0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ.-:+=^!;*?&<>()[]{}@%$#"

Z85MAP = dict([(c, idx) for idx, c in enumerate(Z85CHARS)])

_85s = [85 ** i for i in range(5)][::-1]


def encode(rawbytes):
    """encode raw bytes into Z85"""
    # Accepts only byte arrays bounded to 4 bytes

    if len(rawbytes) % 4:
        raise ValueError("length must be multiple of 4, not %i" % len(rawbytes))

    nvalues = len(rawbytes) / 4

    values = struct.unpack('>%dI' % nvalues, rawbytes)
    encoded = []
    for v in values:
        for offset in _85s:
            encoded.append(Z85CHARS[(v // offset) % 85])

    return bytes(encoded)


def decode(z85bytes):
    """decode Z85 bytes to raw bytes, accepts ASCII string"""
    if isinstance(z85bytes, str):
        try:
            z85bytes = z85bytes.encode('ascii')
        except UnicodeEncodeError:
            raise ValueError('string argument should contain only ASCII characters')

    if len(z85bytes) % 5:
        raise ValueError("Z85 length must be multiple of 5, not %i" % len(z85bytes))

    nvalues = len(z85bytes) / 5
    values = []
    for i in range(0, len(z85bytes), 5):
        value = 0
        for j, offset in enumerate(_85s):
            value += Z85MAP[z85bytes[i + j]] * offset
        values.append(value)
    return struct.pack('>%dI' % nvalues, *values)

def fill(string):
    while len(string) % 4 != 0:
        string = string + "0"
    return string




if __name__ == "__main__":
    string = "111111111111111111111111111111111111111"
    string2 = "/////"

    def test(string):
        print (fill(string))
        print (encode(fill(string).encode("utf-8")).decode("utf-8"))

    test(string)
    test(string2)


