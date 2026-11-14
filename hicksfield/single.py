"""Single instance: a second launch focuses the running app.

A lock file decides who owns the app. The owner also listens on a local
socket; a second launch sends "raise" through it and exits. A crashed
run leaves its socket behind, so the owner removes any stale socket
before listening.
"""

from PySide6.QtCore import QLockFile, QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from shiboken6 import isValid as shiboken6_is_valid

SERVER_NAME = "hicksfield-single-instance"


class SingleInstance:
    def __init__(self, lock_path, on_raise):
        self._lock_path = str(lock_path)
        self._on_raise = on_raise
        self._lock = None
        self._server = None

    def try_acquire(self):
        """True when this process owns the app. False when another does
        (after asking the other one to raise its window)."""
        lock = QLockFile(self._lock_path)
        if lock.tryLock(0):
            self._lock = lock
            QLocalServer.removeServer(SERVER_NAME)
            server = QLocalServer()
            if server.listen(SERVER_NAME):
                server.newConnection.connect(self._handle_connection)
            self._server = server
            return True
        self._ask_other_to_raise()
        return False

    def release(self):
        """Give up the instance: disconnect, close the socket, unlock.

        The server's signal connection holds this object alive, so
        relying on garbage collection leaves the server running past
        the end of the app (and past the QApplication, which crashes
        Qt on the way out). Call this on a clean exit.
        """
        if self._server is not None:
            try:
                self._server.newConnection.disconnect(
                    self._handle_connection)
            except (RuntimeError, TypeError):
                pass
            try:
                self._server.close()
            except RuntimeError:
                pass
            self._server = None
        if self._lock is not None:
            self._lock.unlock()
            self._lock = None

    def _ask_other_to_raise(self):
        socket = QLocalSocket()
        socket.connectToServer(SERVER_NAME)
        if socket.waitForConnected(1000):
            socket.write(b"raise")
            socket.flush()
            socket.waitForBytesWritten(1000)
        socket.close()

    def _handle_connection(self):
        socket = self._server.nextPendingConnection()
        if socket is None:
            return
        socket.readyRead.connect(
            lambda: self._read_raise(socket))
        QTimer.singleShot(1000, lambda: self._read_raise(socket))

    def _read_raise(self, socket):
        if not shiboken6_is_valid(socket):
            return
        if socket.state() != QLocalSocket.ConnectedState:
            return
        try:
            data = bytes(socket.readAll().data())
        except (AttributeError, RuntimeError):
            data = b""
        if b"raise" in data and self._on_raise is not None:
            self._on_raise()
        try:
            socket.close()
        except RuntimeError:
            pass
