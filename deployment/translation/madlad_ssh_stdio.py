"""Фиксированная SSH-команда: только stdin/stdout ↔ MADLAD loopback HTTP."""
import socket
import sys
import threading


def main():
    # Никаких аргументов, команд из запроса, URL или произвольного адресата.
    with socket.create_connection(("127.0.0.1", 18099), timeout=10) as peer:
        peer.settimeout(600)

        def send():
            try:
                while block := sys.stdin.buffer.read1(65536):
                    peer.sendall(block)
                peer.shutdown(socket.SHUT_WR)
            except OSError:
                return

        threading.Thread(target=send, daemon=True).start()
        while block := peer.recv(65536):
            sys.stdout.buffer.write(block)
            sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
