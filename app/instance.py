# -*- coding: utf-8 -*-
"""单实例：从「显示应用」再点一次时激活已有窗口，而不是再开一个藏在微信后面的无边框窗。"""
from PySide6.QtNetwork import QLocalServer, QLocalSocket

SOCK = "jev-chat-ubuntu-raise"


def ping_existing() -> bool:
    client = QLocalSocket()
    client.connectToServer(SOCK)
    if not client.waitForConnected(200):
        return False
    client.write(b"raise\n")
    client.waitForBytesWritten(200)
    client.disconnectFromServer()
    return True


def listen(on_raise):
    QLocalServer.removeServer(SOCK)
    server = QLocalServer()
    if not server.listen(SOCK):
        return server

    def incoming():
        sock = server.nextPendingConnection()
        if sock is None:
            return

        def read():
            sock.readAll()
            on_raise()

        sock.readyRead.connect(read)
        if sock.bytesAvailable():
            read()

    server.newConnection.connect(incoming)
    return server
