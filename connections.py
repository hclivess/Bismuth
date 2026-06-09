"""Length-prefixed JSON framing over a socket — the legacy Bismuth wire protocol.

Every legacy peer-to-peer message is a JSON value preceded by a fixed-width,
zero-padded ASCII length header (``SLEN`` = 10 bytes). :func:`send` serialises a
Python object to JSON, prepends the header, and writes both in one ``sendall``.
:func:`receive` reads the header, then the exact number of payload bytes, and
returns the parsed object — or the sentinel string ``"*"`` on a *logical*
timeout (no data arrived within ``timeout`` seconds, which is normal idle, not an
error).

Two implementations of :func:`receive` are defined depending on the platform:

  * On Linux, ``select.poll`` is used (the pre-computed ``*_FLAGS`` masks classify
    readiness vs. hangup/error).
  * Elsewhere, ``select.select`` is used.

Both block (``setblocking(1)``) and raise on EOF / socket error. This module is
imported very widely (the foundation of the legacy networking stack), so the
framing and error behaviour here are kept byte-for-byte stable.
"""
import select
import json
import platform

# Logical timeout
LTIMEOUT = 45
# Fixed header length
SLEN = 10


def send(sdef, data, slen: int = SLEN) -> None:
    """Send JSON data with fixed-length header."""
    sdef.setblocking(1)
    # Serialize data once and encode
    json_data = json.dumps(data).encode("utf-8")
    # Create header with length
    header = str(len(json_data)).encode("utf-8").zfill(slen)
    # Send header + data in one call
    sdef.sendall(header + json_data)


if "Linux" in platform.system():
    # Pre-compute flag combinations for faster checks
    READ_OR_ERROR = select.POLLIN | select.POLLPRI | select.POLLHUP | select.POLLERR | select.POLLNVAL
    ERROR_FLAGS = select.POLLHUP | select.POLLERR | select.POLLNVAL
    READ_FLAGS = select.POLLIN | select.POLLPRI


    def receive(sdef, slen: int = SLEN, timeout: int = LTIMEOUT):
        """Receive JSON data with fixed-length header using poll (Linux)."""
        poller = select.poll()
        poller.register(sdef, READ_OR_ERROR)

        try:
            sdef.setblocking(1)
            timeout_ms = timeout * 1000

            # Wait for header
            ready = poller.poll(timeout_ms)
            if not ready:
                return "*"  # Logical timeout

            fd, flag = ready[0]

            # Check for errors first (most likely to indicate connection issues)
            if flag & ERROR_FLAGS:
                if not (flag & READ_FLAGS):
                    raise RuntimeError("Socket POLLHUP")

            if flag & READ_FLAGS:
                data = sdef.recv(slen)
                if not data:
                    raise RuntimeError("Socket EOF")
                data_len = int(data)
            else:
                raise RuntimeError(f"Socket Unexpected Error: {flag}")

            # Pre-allocate bytearray for better performance
            result = bytearray(data_len)
            view = memoryview(result)
            bytes_recd = 0

            # Receive data in chunks
            while bytes_recd < data_len:
                ready = poller.poll(timeout_ms)
                if not ready:
                    raise RuntimeError("Socket Timeout2")

                fd, flag = ready[0]

                if flag & ERROR_FLAGS and not (flag & READ_FLAGS):
                    raise RuntimeError("Socket POLLHUP2")

                if flag & READ_FLAGS:
                    # Receive directly into the buffer
                    chunk_size = min(data_len - bytes_recd, 8192)  # Increased buffer size
                    bytes_read = sdef.recv_into(view[bytes_recd:bytes_recd + chunk_size], chunk_size)
                    if bytes_read == 0:
                        raise RuntimeError("Socket EOF2")
                    bytes_recd += bytes_read
                else:
                    raise RuntimeError(f"Socket Error {flag}")

            # Decode and parse JSON
            return json.loads(result.decode("utf-8"))

        except Exception as e:
            raise RuntimeError(f"Connections: {e}")
        finally:
            # Always unregister, even if an exception occurred
            try:
                poller.unregister(sdef)
            except:
                pass

else:
    def receive(sdef, slen: int = SLEN, timeout: int = LTIMEOUT):
        """Receive JSON data with fixed-length header using select (non-Linux)."""
        try:
            sdef.setblocking(1)

            # Wait for header
            ready = select.select([sdef], [], [sdef], timeout)
            if ready[2]:  # Error condition
                raise ConnectionError("Socket in error state")

            if ready[0]:
                header = sdef.recv(slen)
                if not header:
                    raise ConnectionError("Connection closed by remote host")

                try:
                    data_len = int(header)
                except ValueError:
                    raise ValueError(f"Invalid header received: {header!r}")

                if data_len < 0:
                    raise ValueError(f"Invalid data length: {data_len}")
                if data_len > 100 * 1024 * 1024:  # 100MB sanity check
                    raise ValueError(f"Data too large: {data_len} bytes")
            else:
                return "*"  # Logical timeout

            # Pre-allocate bytearray for better performance
            result = bytearray(data_len)
            view = memoryview(result)
            bytes_recd = 0

            # Receive data in chunks
            while bytes_recd < data_len:
                ready = select.select([sdef], [], [sdef], timeout)
                if ready[2]:  # Error condition
                    raise ConnectionError(f"Socket error while receiving (got {bytes_recd}/{data_len} bytes)")

                if ready[0]:
                    chunk_size = min(data_len - bytes_recd, 8192)
                    bytes_read = sdef.recv_into(view[bytes_recd:bytes_recd + chunk_size], chunk_size)
                    if bytes_read == 0:
                        raise ConnectionError(f"Connection broken (got {bytes_recd}/{data_len} bytes)")
                    bytes_recd += bytes_read
                else:
                    raise TimeoutError(f"Timeout receiving data (got {bytes_recd}/{data_len} bytes)")

            # Decode and parse JSON
            try:
                decoded = result.decode("utf-8")
                return json.loads(decoded)
            except UnicodeDecodeError as e:
                raise ValueError(f"Failed to decode received data as UTF-8: {e}")
            except json.JSONDecodeError as e:
                raise ValueError(f"Failed to parse JSON: {e}")

        except (ConnectionError, TimeoutError, ValueError):
            # Re-raise these as-is for better error specificity
            raise
        except OSError as e:
            # Socket-related OS errors
            raise ConnectionError(f"Socket error: {e}")
        except Exception as e:
            # Unexpected errors
            raise RuntimeError(f"Unexpected error in receive: {e}")