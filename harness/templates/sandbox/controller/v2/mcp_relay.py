#!/usr/bin/env python3
"""Unprivileged stdio bridge. The trusted controller owns the actual worker broker."""
import json
import os
import socket
import sys
import threading

def main():
    connection=socket.socket(socket.AF_UNIX)
    connection.connect(os.environ['DENG_MCP_SOCKET'])
    incoming=connection.makefile('rb')
    outgoing=connection.makefile('wb')
    outgoing.write((json.dumps({'token':os.environ['DENG_MCP_TOKEN']})+'\n').encode());outgoing.flush()
    if incoming.readline()!=b'OK\n': raise PermissionError('Broker authentication failed')
    def receive():
        while True:
            line=incoming.readline(16*1024*1024+1)
            if not line:return
            if len(line)>16*1024*1024:raise ValueError('Response bound exceeded')
            sys.stdout.buffer.write(line);sys.stdout.buffer.flush()
    thread=threading.Thread(target=receive,daemon=True);thread.start()
    while True:
        line=sys.stdin.buffer.readline(1024*1024+1)
        if not line:break
        if len(line)>1024*1024:raise ValueError('Request bound exceeded')
        outgoing.write(line);outgoing.flush()
    connection.shutdown(socket.SHUT_WR)
    thread.join(timeout=1)
    connection.close()

if __name__=='__main__':main()
