from whatsappy.tokens import str2tok, tok2str
from whatsappy.node import Node
from whatsappy.exceptions import StreamError

ENCRYPTED_IN = 0x8
ENCRYPTED_OUT = 0x1


class MessageIncomplete(Exception):
    """
    Indicates that the receiver needs more data before continuing.
    """
    pass


class EndOfStream(Exception):
    """
    Indicates an end of stream error.
    """
    pass


class Reader(object):
    """
    """

    def __init__(self):
        self.buf = bytes()
        self.offset = 0
        self.decrypt = None

    def data(self, buf):
        self.buf += buf

    def _consume(self, size):
        if size > len(self.buf):
            raise StreamError("Not enough bytes available")

        self.offset += size

        data = self.buf[:size]
        self.buf = self.buf[size:]
        return data

    def _peek(self, bytes):
        return self.buf[:bytes]

    def read(self):
        if len(self.buf) <= 2:
            raise MessageIncomplete()

        # Read stanza, but don't consume yet
        buf = self.peek_int24()

        flags = ((buf >> 16) & 0xF0) >> 4
        length = (buf & 0xFFFF) | (((buf >> 16) & 0x0F) << 16)

        if length > len(self.buf):
            raise MessageIncomplete()

        # Process message. At this point, the message is complete, but the
        # first three bytes should be consumed.
        self.int24()

        if flags & ENCRYPTED_IN:
            return self._read_encrypted(length)
        else:
            plain = self.buf[:length]
            return self._read(), plain

    def _read_encrypted(self, length):
        message_buf = self._consume(length)
        message_buf = self.decrypt(message_buf)

        buf = self.buf
        offset = self.offset

        try:
            self.buf = message_buf
            self.offset = 0

            return self._read(), message_buf
        finally:
            self.buf = buf
            self.offset = offset

    def _read(self):
        length = self.list_start()
        token = self.peek_int8()

        if token == 0x01:
            self._consume(1)
            attributes = self.attributes(length)
            return Node("start", **attributes)
        elif token == 0x02:
            self._consume(1)
            raise EndOfStream()

        node = Node(self.string())
        node.attributes = self.attributes(length)

        if (length % 2) == 0:
            token = self.peek_int8()

            if token == 0xf8 or token == 0xf9:
                node.children = self.list()
            else:
                node.data = self.string()

        return node

    def peek_int8(self):
        return ord(self._peek(1))

    def peek_int16(self):
        s = self._peek(2)
        return ord(s[0]) << 8 | ord(s[1])

    def peek_int24(self):
        s = self._peek(3)
        return ord(s[0]) << 16 | ord(s[1]) << 8 | ord(s[2])

    def int8(self):
        return ord(self._consume(1))

    def int16(self):
        s = self._consume(2)
        return ord(s[0]) << 8 | ord(s[1])

    def int24(self):
        s = self._consume(3)
        return ord(s[0]) << 16 | ord(s[1]) << 8 | ord(s[2])

    def list(self):
        children = []
        for i in range(self.list_start()):
            children.append(self._read())
        return children

    def list_start(self):
        token = self.int8()

        if token == 0x00:
            return 0
        elif token == 0xF8:
            return self.int8()
        elif token == 0xF9:
            return self.int16()
        else:
            raise ValueError("Unknown list start token: %02x" % ord(token))

    def attributes(self, length):
        attributes = {}

        for _ in range((length - 1) / 2):
            name = self.string()
            value = self.string()
            attributes[name] = value
        return attributes

    def string(self):
        token = self.int8()

        if token == 0x00:
            return ""
        elif 0x02 < token < 0xf5:
            if token == 0xec:
                return tok2str(0xed + self.int8())
            else:
                return tok2str(token)
        elif token == 0xFA:
            user = self.string()
            server = self.string()
            return user + "@" + server
        elif token == 0xFC:
            return self._consume(self.int8())
        elif token == 0xFD:
            return self._consume(self.int24())
        elif token == 0xFE:
            return tok2str(0xF5 + self.int8())
        elif token == 0xFF:
            nibble = self.int8()
            ignore_last_nibble = nibble & 0x80
            size = nibble & 0x7f
            nibbles_count = size * 2 - (1 if ignore_last_nibble else 0)

            data = self._consume(size)
            output = ""

            for i in xrange(nibbles_count):
                shift = 4 * (1 - i % 2)
                decimal = (ord(data[i // 2]) & (15 << shift)) >> shift

                if decimal < 10:
                    output += str(decimal)
                else:
                    output += chr(decimal - 10 + 45)

            return output
        else:
            raise ValueError("Unknown string token: %02x" % ord(token))


class Writer(object):
    """
    """

    def __init__(self):
        self.encrypt = None

    def start_stream(self, domain, resource):
        attributes = {"to": domain, "resource": resource}

        # Version 1.5
        buf = "WA\x01\x05\x00\x00\x17"

        buf += self.list_start(len(attributes) * 2 + 1)
        buf += "\x01"
        buf += self.attributes(attributes)

        return buf

    def node(self, node, encrypt=None):
        if node is None:
            buf = plain = "\x00"
        else:
            buf = plain = self._node(node)

        if encrypt is None:
            encrypt = self.encrypt is not None

        if encrypt:
            buf = self.encrypt(buf)

            first = (8 << 4) | ((len(buf) & 16711680) >> 16)
            second = (len(buf) & 65280) >> 8
            third = len(buf) & 255

            header = self.int24((first << 16) | (second << 8) | third)
        else:
            header = self.int24(len(buf))

        return header + buf, plain

    def _node(self, node):
        length = 1
        if node.attributes:
            length += len(node.attributes) * 2
        if node.children:
            length += 1
        if node.data:
            length += 1

        buf = self.list_start(length)
        buf += self.string(node.name)
        buf += self.attributes(node.attributes)

        if node.data:
            buf += self.bytes(node.data)

        if node.children:
            buf += self.list_start(len(node.children))
            for child in node.children:
                buf += self._node(child)

        return buf

    def token(self, token):
        if token < 0xF5:
            return chr(token)
        elif token <= 0x1F4:
            return "\xFE" + chr(token - 0xF5)

    def int8(self, value):
        return chr(value & 0xFF)

    def int16(self, value):
        return chr((value & 0xFF00) >> 8) + \
            chr((value & 0x00FF) >> 0)

    def int24(self, value):
        return chr((value & 0xFF0000) >> 16) + \
            chr((value & 0x00FF00) >> 8) + \
            chr((value & 0x0000FF) >> 0)

    def jid(self, user, server):
        buf = "\xFA"
        buf += self.string(user) if user else "\x00"
        buf += self.string(server)
        return buf

    def bytes(self, string):
        if isinstance(string, unicode):
            string = string.encode("utf-8")
        if len(string) > 0xFF:
            leader = "\xFD" + self.int24(len(string))
        else:
            leader = "\xFC" + self.int8(len(string))
        return leader + string

    def string(self, string):
        token = str2tok(string)

        if token is not None:
            if token > 0xEB:
                return self.token(0xEC) + self.token(token - 0xED)
            else:
                return self.token(token)
        elif "@" in string:
            user, at, server = string.partition("@")
            return self.jid(user, server)
        else:
            return self.bytes(string)

    def attributes(self, attributes):
        buf = bytes()
        for key, value in attributes.iteritems():
            buf += self.string(key)
            buf += self.string(value)
        return buf

    def list_start(self, length):
        if length == 0:
            return "\x00"
        elif length <= 0xFF:
            return "\xF8" + chr(length)
        else:
            return "\xF9" + self.int16(length)
