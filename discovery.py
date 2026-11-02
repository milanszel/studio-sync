"""Local LAN host discovery. Replies name the host only; the room code is never broadcast."""

import json
import socket
import threading
import time

DISCOVERY_PORT = 42612
MAGIC = "studio-sync-5"


def local_addresses():
    """IPv4 addresses of this machine's network interfaces (best effort, stdlib only)."""
    found = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    except OSError:
        pass
    # The address used for the default route, even when the hostname doesn't resolve.
    for probe in ("192.0.2.1", "10.255.255.255"):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            try:
                s.connect((probe, 9))
                found.add(s.getsockname()[0])
            except OSError:
                pass
    return sorted(a for a in found if not a.startswith(("127.", "169.254.", "0.")))


def broadcast_targets():
    """Directed broadcasts for each local subnet (assumed /24), plus the general one.
    Macs with WiFi and Ethernet only send 255.255.255.255 out of one of them."""
    targets = ["255.255.255.255", "127.0.0.1"]
    for address in local_addresses():
        parts = address.split(".")
        if len(parts) == 4:
            targets.append(".".join(parts[:3] + ["255"]))
    return list(dict.fromkeys(targets))


class HostDiscovery:
    def __init__(self, name=None):
        self.name = str(name or socket.gethostname())[:40]
        self.done = threading.Event()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("", DISCOVERY_PORT))
        self.sock.settimeout(0.2)
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while not self.done.is_set():
            try:
                raw, addr = self.sock.recvfrom(1024)
                p = json.loads(raw)
                if p == {"discover": MAGIC}:
                    self.sock.sendto(json.dumps(dict(service=MAGIC, name=self.name)).encode(), addr)
            except (socket.timeout, ValueError, TypeError, ConnectionResetError):
                continue
            except OSError:
                return

    def close(self):
        self.done.set()
        self.sock.close()
        self.thread.join(timeout=0.3)


def find_hosts(timeout=0.8):
    found = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.settimeout(0.1)
        payload = json.dumps({"discover": MAGIC}).encode()
        sent = False
        for addr in broadcast_targets():
            try:
                s.sendto(payload, (addr, DISCOVERY_PORT))
                sent = True
            except OSError:
                continue
        if not sent:
            raise OSError("No network interface available for discovery")
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                raw, addr = s.recvfrom(1024)
                p = json.loads(raw)
                if p.get("service") == MAGIC:
                    found[addr[0]] = dict(ip=addr[0], name=str(p.get("name", addr[0]))[:80])
            except (socket.timeout, ValueError, TypeError, AttributeError, ConnectionResetError):
                continue
    # Prefer a LAN address over loopback for the same machine.
    if len(found) > 1:
        found.pop("127.0.0.1", None)
    return list(found.values())
