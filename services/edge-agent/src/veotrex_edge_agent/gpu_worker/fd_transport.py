from __future__ import annotations

import array
import fcntl
import os
import socket
import stat
from collections.abc import Iterable

REQUIRED_SEALS = fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE


class FileDescriptorError(RuntimeError):
    """A bounded tensor file-descriptor transport failure."""


def create_sealed_memfd(data: bytes | bytearray | memoryview) -> int:
    fd = os.memfd_create("veotrex-tensor", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    try:
        view = memoryview(data).cast("B")
        os.ftruncate(fd, len(view))
        offset = 0
        while offset < len(view):
            written = os.write(fd, view[offset:])
            if written <= 0:
                raise FileDescriptorError("tensor_write_failed")
            offset += written
        os.lseek(fd, 0, os.SEEK_SET)
        fcntl.fcntl(fd, fcntl.F_ADD_SEALS, REQUIRED_SEALS)
        return fd
    except Exception:
        os.close(fd)
        raise


def validate_sealed_memfd(fd: int, expected_size: int) -> None:
    metadata = os.fstat(fd)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != expected_size:
        raise FileDescriptorError("invalid_tensor_size")
    seals = fcntl.fcntl(fd, fcntl.F_GET_SEALS)
    if seals & REQUIRED_SEALS != REQUIRED_SEALS:
        raise FileDescriptorError("mutable_tensor_rejected")


def send_packet(sock: socket.socket, data: bytes, fds: Iterable[int] = ()) -> None:
    descriptors = array.array("i", fds)
    if not descriptors:
        sock.sendall(data)
        return
    ancillary = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, descriptors)] if descriptors else []
    if sock.sendmsg([data], ancillary) != len(data):
        raise FileDescriptorError("short_packet_send")


def receive_packet(sock: socket.socket, maximum_bytes: int) -> tuple[bytes, list[int]]:
    item_size = array.array("i").itemsize
    data, ancillary, flags, _ = sock.recvmsg(maximum_bytes + 1, socket.CMSG_SPACE(item_size * 2))
    received: list[int] = []
    try:
        for level, kind, payload in ancillary:
            if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS:
                raise FileDescriptorError("malformed_ancillary_data")
            descriptors = array.array("i")
            descriptors.frombytes(payload[: len(payload) - (len(payload) % item_size)])
            received.extend(descriptors)
        if flags & socket.MSG_CTRUNC:
            raise FileDescriptorError("ancillary_data_truncated")
        return data, received
    except Exception:
        for fd in received:
            os.close(fd)
        raise
