"""Getting bytes to a printer: over the network or to a USB printer installed
in Windows. Also finds printers on your network and lists Windows printers.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import shutil
import socket
import subprocess
import sys

import aiohttp

log = logging.getLogger("printers")


def lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


# ------------------------------------------------------------------ network

CHUNK = 1024                      # bytes per piece when sending
CHUNK_PAUSE = 0.03                # seconds between pieces: about 30 KB/s, gentle on small buffers


async def send_network(host: str, port: int, data: bytes, paced: bool = True):
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=5)
    try:
        if not paced:
            writer.write(data)
            await writer.drain()
        for i in range(0, len(data), CHUNK) if paced else ():
            writer.write(data[i:i + CHUNK])
            await writer.drain()
            await asyncio.sleep(CHUNK_PAUSE)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def _open(host, port, timeout):
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        w.close()
        return True
    except Exception:
        return False


async def _hostname(host):
    loop = asyncio.get_running_loop()
    try:
        name = await asyncio.wait_for(loop.run_in_executor(None, socket.gethostbyaddr, host), 1.5)
        return name[0].split(".")[0]
    except Exception:
        return ""


async def _web_title(http, host):
    """Many printers have a small web page; its title usually names the model."""
    try:
        async with http.get(f"http://{host}/", timeout=aiohttp.ClientTimeout(total=2),
                            allow_redirects=True, ssl=False) as r:
            text = await r.text(errors="replace")
        m = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
        return " ".join(m.group(1).split())[:60] if m else ""
    except Exception:
        return ""


async def scan_network(http, subnet: str | None = None, port: int = 9100, timeout: float = 0.7):
    """Finds every device on the local /24 that accepts print jobs on port 9100."""
    if subnet:
        net = ipaddress.ip_network(subnet, strict=False)
    else:
        net = ipaddress.ip_network(f"{lan_ip()}/24", strict=False)
    if net.num_addresses > 1024:
        raise ValueError("network too large to scan, use a /24 like 192.168.1.0/24")
    hosts = [str(h) for h in net.hosts()]
    sem = asyncio.Semaphore(128)

    async def probe(h):
        async with sem:
            return h if await _open(h, port, timeout) else None

    found = [h for h in await asyncio.gather(*(probe(h) for h in hosts)) if h]
    found.sort(key=lambda h: tuple(int(x) for x in h.split(".")))
    names = await asyncio.gather(*(_hostname(h) for h in found))
    titles = await asyncio.gather(*(_web_title(http, h) for h in found))
    return {"subnet": str(net), "printers": [{"host": h, "port": port, "hostname": n, "title": t}
                                             for h, n, t in zip(found, names, titles)]}


# ------------------------------------------------------------------ USB / installed printers

def _win32print():
    try:
        import win32print            # from the pywin32 package
        return win32print
    except ImportError:
        return None


def list_local_printers() -> dict:
    """Printers installed on this computer (Windows), or CUPS queues elsewhere."""
    wp = _win32print()
    if wp:
        flags = wp.PRINTER_ENUM_LOCAL | wp.PRINTER_ENUM_CONNECTIONS
        names = sorted({p[2] for p in wp.EnumPrinters(flags, None, 1)})
        try:
            default = wp.GetDefaultPrinter()
        except Exception:
            default = ""
        return {"printers": names, "default": default}
    if sys.platform == "win32":
        raise RuntimeError("pywin32 is missing - run: py -m pip install -r requirements.txt")
    if shutil.which("lpstat"):
        out = subprocess.run(["lpstat", "-e"], capture_output=True, text=True, timeout=5).stdout
        return {"printers": sorted(l.strip() for l in out.splitlines() if l.strip()), "default": ""}
    raise RuntimeError("no printer system found on this computer")


def _send_local_blocking(name: str, data: bytes):
    wp = _win32print()
    if wp:
        h = wp.OpenPrinter(name)
        try:
            wp.StartDocPrinter(h, 1, ("Print Desk", None, "RAW"))   # RAW = bytes go straight to the printer
            try:
                wp.StartPagePrinter(h)
                wp.WritePrinter(h, data)
                wp.EndPagePrinter(h)
            finally:
                wp.EndDocPrinter(h)
        finally:
            wp.ClosePrinter(h)
        return
    if sys.platform == "win32":
        raise RuntimeError("pywin32 is missing - run: py -m pip install -r requirements.txt")
    r = subprocess.run(["lp", "-d", name, "-o", "raw"], input=data, capture_output=True, timeout=15)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="replace").strip() or "lp failed")


async def send_local(name: str, data: bytes):
    if not name:
        raise ValueError("no USB printer chosen")
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _send_local_blocking, name, data)


class PrinterLock:
    """Only one Print Desk program talks to the printer at a time.

    The running Print Desk and a test command started next to it would otherwise
    send at the same moment, and the printer mixes the two slips into gibberish.
    A small lock file next to Print Desk does the taking turns; a lock left behind
    by a crashed program expires after a minute."""

    def __init__(self, path, stale: float = 60):
        self.path = path
        self.stale = stale

    async def __aenter__(self):
        import os
        import time
        waited = 0.0
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, f"{os.getpid()} {time.time()}".encode())
                os.close(fd)
                return self
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.path) > self.stale:
                        os.remove(self.path)              # left behind by a program that crashed
                        continue
                except OSError:
                    continue
            except OSError:
                return self                               # can't write the folder: carry on without
            if waited > 120:
                return self
            await asyncio.sleep(0.2)
            waited += 0.2

    async def __aexit__(self, *exc):
        import os
        try:
            os.remove(self.path)
        except OSError:
            pass
