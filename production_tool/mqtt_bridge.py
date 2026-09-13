#!/usr/bin/python
# -*- coding: utf-8 -*-
"""
mqtt_bridge.py  -  Wi-SUN B-route -> ECHONET Lite -> Home Assistant MQTT
Python 2.7 stdlib only: termios, fcntl, select, socket, struct, json, os
"""

from __future__ import print_function

import os
import sys
import json
import time
import struct
import socket
import select
import binascii
import termios
import fcntl
import collections
import re
import threading

CONFIG_PATH = "/data/local/config.json"
LOG_PATH    = "/data/local/mqtt_bridge.log"

LED_R = "/sys/class/leds/red/brightness"
LED_G = "/sys/class/leds/green/brightness"
LED_B = "/sys/class/leds/blue/brightness"

def led_rgb(r, g, b):
    for path, val in ((LED_R, r), (LED_G, g), (LED_B, b)):
        try:
            with open(path, 'w') as f:
                f.write(str(val) + '\n')
        except Exception:
            pass

def led_read():
    result = []
    for path in (LED_R, LED_G, LED_B):
        try:
            with open(path) as f:
                result.append(int(f.read().strip()))
        except Exception:
            result.append(0)
    return tuple(result)

_log_file = None

def log(msg):
    # 日本時間 (JST: UTC+9) でタイムスタンプを生成
    jst_time = time.time() + 9 * 3600
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(jst_time))
    line = "[{}] {}\n".format(ts, msg)
    global _log_file
    if _log_file:
        try:
            _log_file.write(line)
            _log_file.flush()
        except Exception:
            pass
    else:
        sys.stderr.write(line)
        sys.stderr.flush()

def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)

# ---------------------------------------------------------------------------
# Serial port (termios, no pyserial)
# ---------------------------------------------------------------------------

def open_serial(port, baud=115200):
    fd = os.open(port, os.O_RDWR | os.O_NOCTTY)

    attrs = list(termios.tcgetattr(fd))
    iflag, oflag, cflag, lflag = attrs[0], attrs[1], attrs[2], attrs[3]

    # raw input
    iflag &= ~(termios.IGNBRK | termios.BRKINT | termios.PARMRK |
               termios.ISTRIP | termios.INLCR  | termios.IGNCR  |
               termios.ICRNL  | termios.IXON)
    oflag &= ~termios.OPOST
    cflag &= ~(termios.CSIZE | termios.PARENB)
    cflag |=  termios.CS8 | termios.CREAD | termios.CLOCAL
    lflag &= ~(termios.ECHO | termios.ECHONL | termios.ICANON |
               termios.ISIG | termios.IEXTEN)

    baud_map = {
        9600:   termios.B9600,
        19200:  termios.B19200,
        38400:  termios.B38400,
        57600:  termios.B57600,
        115200: termios.B115200,
    }
    baud_const = baud_map.get(baud, termios.B115200)

    cc = attrs[6]
    # attrs[6] must be returned in the same type tcgetattr gave us.
    # On this device Python 2.7 it is a list of 32 ints; tcsetattr rejects bytes.
    if isinstance(cc, list):
        cc_list = list(cc)
        cc_list[termios.VMIN]  = 1
        cc_list[termios.VTIME] = 0
        attrs[6] = cc_list
    else:
        # bytes/bytearray path
        cc_arr = bytearray(cc)
        cc_arr[termios.VMIN]  = 1
        cc_arr[termios.VTIME] = 0
        attrs[6] = bytes(cc_arr)

    attrs[0], attrs[1], attrs[2], attrs[3] = iflag, oflag, cflag, lflag
    attrs[4] = baud_const
    attrs[5] = baud_const

    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    termios.tcflush(fd, termios.TCIOFLUSH)
    return fd

def serial_write(fd, data):
    if isinstance(data, bytes):
        os.write(fd, data)
    else:
        os.write(fd, data.encode("ascii"))

def serial_readline(fd, timeout=10):
    """Read one CRLF-terminated line; return decoded str or None on timeout."""
    buf = b""
    deadline = time.time() + timeout
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        r, _, _ = select.select([fd], [], [], min(remaining, 0.5))
        if not r:
            continue
        ch = os.read(fd, 1)
        if not ch:
            continue
        buf += ch
        if buf.endswith(b"\r\n"):
            return buf[:-2].decode("ascii", errors="replace")
    return buf.decode("ascii", errors="replace") if buf else None

def _led_blink(stop_event, colors, interval=0.2):
    i = 0
    while not stop_event.is_set():
        led_rgb(*colors[i % len(colors)])
        i += 1
        stop_event.wait(interval)

def skcommand(fd, cmd, timeout=10):
    """Send one SKSTACK command; return list of response lines (up to OK/FAIL)."""
    orig_led = led_read()
    stop_event = threading.Event()
    t = threading.Thread(target=_led_blink,
                         args=(stop_event, [(0, 255, 0), (0, 0, 255)]))
    t.daemon = True
    t.start()

    # B-route credentials are echoed by the module; never persist them in logs.
    sensitive = cmd.startswith("SKSETPWD ") or cmd.startswith("SKSETRBID ")
    log("CMD > {}".format(cmd.split()[0] + " [redacted]" if sensitive else cmd))
    serial_write(fd, cmd + "\r\n")
    lines = []
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            line = serial_readline(fd, timeout=max(0.5, deadline - time.time()))
            if line is None:
                break
            line_str = line.strip()
            if line_str:
                log("CMD < {}".format(line_str.split()[0] + " [redacted]"
                                      if sensitive and line_str.startswith(cmd.split()[0] + " ")
                                      else line_str))
            lines.append(line)
            if line in ("OK", ) or line.startswith("FAIL"):
                break
    finally:
        stop_event.set()
        t.join(timeout=1)
        led_rgb(*orig_led)
    return lines

# ---------------------------------------------------------------------------
# Scan settings
# ---------------------------------------------------------------------------

SCAN_DURATION_BASE = 4
SCAN_RETRY_LIMIT = 7
NO_RESPONSE_REJOIN_COUNT = 6
DIRECT_JOIN_RETRY_INTERVAL = 1800
DIRECT_JOIN_MARKER = "/data/local/mqtt_direct_join.last"
_last_direct_join_at = 0

# ---------------------------------------------------------------------------
# SKSTACK-IP / Wi-SUN B-route connection
# ---------------------------------------------------------------------------

def skscan(fd, expected_pair_id, target_pan_id=None, target_channel=None, target_addr=None):
    """Return only PANs belonging to the configured B-route meter."""
    duration = SCAN_DURATION_BASE
    
    while duration <= SCAN_RETRY_LIMIT:
        # Clear stale lines from previous command/scan cycle.
        termios.tcflush(fd, termios.TCIFLUSH)

        # Wi-SUN active scan time: ~20ms * (2^duration + 1) * 32 channels
        # e.g. duration=4 -> ~11s, duration=5 -> ~21s, duration=6 -> ~42s
        expected_sec = int(0.02 * (2**duration + 1) * 32)
        timeout_sec  = expected_sec + 10

        cmd = "SKSCAN 2 FFFFFFFF {} 0".format(duration)
        log("SCAN > {} (waiting up to ~{}s for scan completion)".format(cmd, timeout_sec))
        # BP35C0 style scan command: <mode> <channel_mask> <duration> <side>
        serial_write(fd, cmd + "\r\n")

        pan_list  = []
        current   = {}
        deadline  = time.time() + timeout_sec
        scan_done = False

        while time.time() < deadline:
            line = serial_readline(fd, timeout=2)
            if line is None:
                continue
            line_clean = line.strip()
            if line_clean:
                # The raw scan can contain neighboring meters' identifiers.
                if line_clean.startswith(("Pan ID:", "Addr:", "PairID:")):
                    log("SCAN < {} [redacted]".format(line_clean.split(":", 1)[0] + ":"))
                else:
                    log("SCAN < {}".format(line_clean))

            if line_clean.startswith("EVENT 20"):
                if current:
                    pan_list.append(current)
                current = {}
            elif line_clean.startswith("EVENT 22"):
                if current:
                    pan_list.append(current)
                current = {}
                scan_done = True
                log("SKSCAN completed (EVENT 22 received)")
                break  # Exit loop once EVENT 22 received
            elif ":" in line_clean and not line_clean.startswith("EVENT"):
                key, _, val = line_clean.partition(":")
                current[key.strip()] = val.strip()

        if current and current not in pan_list:
            pan_list.append(current)

        if pan_list:
            normalized_list = []
            for i, p in enumerate(pan_list):
                # キーの表記揺れを吸収 (Pan ID / PanID / pan_id など)
                norm = {}
                for k, v in p.items():
                    kc = k.replace(" ", "").lower()
                    if kc in ("channel", "ch"):
                        norm["Channel"] = v
                    elif kc in ("panid", "pan"):
                        norm["Pan ID"] = v
                    elif kc in ("addr", "mac"):
                        norm["Addr"] = v
                    elif kc == "lqi":
                        norm["LQI"] = v
                    elif kc in ("pairid", "pairingid"):
                        norm["PairID"] = v
                    else:
                        norm[k] = v
                normalized_list.append(norm)

            # A scan may see other meters in an apartment building. Never
            # authenticate to an unrelated PAN or log its identifiers.
            eligible = []
            for pan in normalized_list:
                if pan.get("PairID", "").upper() != expected_pair_id.upper():
                    continue
                if target_pan_id and pan.get("Pan ID", "").upper() != str(target_pan_id).upper():
                    continue
                if target_addr and pan.get("Addr", "").upper() != str(target_addr).upper():
                    continue
                eligible.append(pan)
            log("SKSCAN received {} PAN(s), {} match the configured meter".format(
                len(normalized_list), len(eligible)))
            if not eligible:
                log("No authorized meter in scan results; retrying with longer duration")
                duration += 1
                continue

            for i, norm in enumerate(eligible):
                extra = " ".join(["{}={}".format(k, v) for k, v in sorted(norm.items())
                                 if k not in ("Channel", "Pan ID", "Addr", "LQI", "PairID")])
                log("  [{}] Channel={} Pan ID={} Addr={} LQI={}{}".format(
                    i + 1,
                    norm.get("Channel", "N/A"),
                    norm.get("Pan ID", "N/A"),
                    norm.get("Addr", "N/A"),
                    norm.get("LQI", "N/A"),
                    " " + extra if extra else ""
                ))

            # The channel can change without changing the meter's identity.
            for pan in eligible:
                pan["_is_target"] = True
            eligible.sort(key=lambda p: int(p.get("LQI", "0"), 16), reverse=True)
            return eligible

        if not scan_done:
            log("SKSCAN timed out without EVENT 22, retrying with longer duration")
        else:
            log("SKSCAN finished but no matching PAN found, retrying with longer duration")
        duration += 1

    return []

def skll64(fd, mac):
    """Convert MAC address to IPv6 link-local address.

    Reads lines until an IPv6-like substring (hex digits + colons) is found
    and validated. Returns the candidate string or None on timeout.
    """
    serial_write(fd, "SKLL64 {}\r\n".format(mac))
    deadline = time.time() + 10
    while time.time() < deadline:
        line = serial_readline(fd, timeout=2)
        if not line:
            continue
        # skip echoes and obvious non-data lines
        if line.startswith("SKLL64") or line.strip() == "":
            continue
        # extract only hex+colon runs (length threshold to avoid short noise)
        m = re.search(r'([0-9A-Fa-f:]{15,})', line)
        if not m:
            continue
        candidate = m.group(1)
        # validate with inet_pton if available
        try:
            socket.inet_pton(socket.AF_INET6, candidate)
            return candidate
        except Exception:
            # not valid IPv6; continue waiting for a proper response
            log("skll64: received candidate but validation failed: {}".format(candidate))
            continue
    return None

def wisun_connect(fd, br_id, br_pwd, target_pan_id=None, target_channel=None, target_addr=None):
    """Full SKSTACK-IP join sequence. Returns IPv6 address of meter."""
    global _last_direct_join_at
    log("Initializing Wi-SUN module...")
    skcommand(fd, "SKRESET", timeout=5)
    time.sleep(1)

    skcommand(fd, "SKSETPWD C {}".format(br_pwd), timeout=5)
    skcommand(fd, "SKSETRBID {}".format(br_id), timeout=5)

    # Force ASCII-hex ERXUDP payload format if supported (BP35A1/BP35C0 compatibility)
    skcommand(fd, "WOPT 1", timeout=2)

    # A previously verified meter can be tried without waiting for a beacon.
    # Rate-limit this path because repeated PANA attempts can burden the meter.
    now = time.time()
    try:
        last_direct_join_at = max(_last_direct_join_at, os.path.getmtime(DIRECT_JOIN_MARKER))
    except OSError:
        last_direct_join_at = _last_direct_join_at
    if (target_pan_id and target_channel and target_addr and
            now - last_direct_join_at >= DIRECT_JOIN_RETRY_INTERVAL):
        _last_direct_join_at = now
        try:
            with open(DIRECT_JOIN_MARKER, "w") as marker:
                marker.write(str(int(now)) + "\n")
        except IOError as exc:
            log("Could not persist direct join rate limit: {}".format(exc))
        log("Trying configured meter directly: Channel={} Pan ID={} Addr={}".format(
            target_channel, target_pan_id, target_addr))
        ipv6 = skll64(fd, target_addr)
        if ipv6:
            skcommand(fd, "SKSREG S2 {}".format(target_channel))
            skcommand(fd, "SKSREG S3 {}".format(target_pan_id))
            serial_write(fd, "SKJOIN {}\r\n".format(ipv6))
            deadline = time.time() + 45
            while time.time() < deadline:
                line = serial_readline(fd, timeout=2)
                if line is None:
                    continue
                if "EVENT 25" in line:
                    log("Direct SKJOIN succeeded: Channel={} Pan ID={}".format(
                        target_channel, target_pan_id))
                    return ipv6
                if "EVENT 24" in line:
                    log("Direct SKJOIN PANA connection failed (EVENT 24)")
                    break
            else:
                log("Direct SKJOIN timed out")
        else:
            log("Direct SKLL64 failed")
        log("Falling back to active scan")
        skcommand(fd, "SKRESET", timeout=5)
        time.sleep(1)
        skcommand(fd, "SKSETPWD C {}".format(br_pwd), timeout=5)
        skcommand(fd, "SKSETRBID {}".format(br_id), timeout=5)
        skcommand(fd, "WOPT 1", timeout=2)

    log("Starting Wi-SUN PAN Active Scan...")
    pan_candidates = skscan(fd, br_id[-8:], target_pan_id=target_pan_id,
                            target_channel=target_channel,
                            target_addr=target_addr)
    if not pan_candidates:
        raise RuntimeError("SKSCAN: no candidate PAN found")

    log("Testing connection to {} candidate PAN(s) sequentially...".format(len(pan_candidates)))

    for idx, pan in enumerate(pan_candidates):
        channel = pan.get("Channel")
        pan_id  = pan.get("Pan ID")
        mac     = pan.get("Addr")
        lqi     = pan.get("LQI", "N/A")
        is_tgt  = pan.get("_is_target", False)

        if not channel or not pan_id or not mac:
            log("Candidate [{}/{}] missing channel/pan_id/mac, skipping: {}".format(
                idx + 1, len(pan_candidates), pan))
            continue

        prefix = "[Target PAN]" if is_tgt else "[Fallback PAN]"
        log("--- Trying PAN [{}/{}]{}: Channel={} Pan ID={} Addr={} LQI={} ---".format(
            idx + 1, len(pan_candidates), " " + prefix if target_pan_id else "", channel, pan_id, mac, lqi
        ))

        ipv6 = skll64(fd, mac)
        if not ipv6:
            log("SKLL64 failed for PAN [{}/{}], skipping".format(idx + 1, len(pan_candidates)))
            continue

        skcommand(fd, "SKSREG S2 {}".format(channel))
        skcommand(fd, "SKSREG S3 {}".format(pan_id))

        log("SKJOIN {} (Channel={} Pan ID={})".format(ipv6, channel, pan_id))
        serial_write(fd, "SKJOIN {}\r\n".format(ipv6))

        orig_led = led_read()
        stop_event = threading.Event()
        t = threading.Thread(target=_led_blink,
                             args=(stop_event, [(0, 255, 0), (0, 0, 255)]))
        t.daemon = True
        t.start()

        join_success = False
        try:
            deadline = time.time() + 45
            while time.time() < deadline:
                line = serial_readline(fd, timeout=2)
                if line is None:
                    continue
                if "EVENT 25" in line:
                    log("==================================================")
                    log(">>> SKJOIN SUCCESS! Connected to PAN [{}/{}] <<<".format(idx + 1, len(pan_candidates)))
                    log(">>> Channel: {} | Pan ID: {} | Addr: {} | LQI: {} <<<".format(channel, pan_id, mac, lqi))
                    log(">>> Meter IPv6: {} <<<".format(ipv6))
                    log(">>> Hint: Set \"target_pan_id\": \"{}\" in config.json to connect directly in future. <<<".format(pan_id))
                    log("==================================================")
                    join_success = True
                    return ipv6
                if "EVENT 24" in line:
                    log("SKJOIN: PANA connection failed (EVENT 24) for PAN [{}/{}]: Channel={} Pan ID={}".format(
                        idx + 1, len(pan_candidates), channel, pan_id))
                    break
        finally:
            stop_event.set()
            t.join(timeout=1)
            led_rgb(*orig_led)

        if not join_success:
            if is_tgt:
                log("Target PAN [{}/{}] (Pan ID={}) failed. Automatically trying remaining detected PANs...".format(
                    idx + 1, len(pan_candidates), pan_id))
            else:
                log("PAN [{}/{}] connection failed, trying next candidate...".format(
                    idx + 1, len(pan_candidates)))
            time.sleep(1)

    raise RuntimeError("SKJOIN: All candidate PAN connections failed")

# ---------------------------------------------------------------------------
# ECHONET Lite frame builder / parser
# ---------------------------------------------------------------------------

EPCS = [0xD3, 0xE1, 0xE7, 0xE0, 0xE3, 0xE8]

def build_el_get(tid, epcs):
    frame = bytearray()
    frame += b"\x10\x81"                     # EHD1, EHD2
    frame += struct.pack(">H", tid & 0xFFFF) # TID
    frame += b"\x05\xFF\x01"                 # SEOJ: controller
    frame += b"\x02\x88\x01"                 # DEOJ: smart meter
    frame += b"\x62"                         # ESV: Get
    frame += struct.pack("B", len(epcs))     # OPC
    for epc in epcs:
        frame += struct.pack("BB", epc, 0)   # EPC, PDC=0
    return bytes(frame)

def parse_el_response(data):
    """Returns dict {epc_int: bytearray}."""
    if len(data) < 12:
        return {}
    esv = data[10] if isinstance(data[10], int) else ord(data[10])
    opc = data[11] if isinstance(data[11], int) else ord(data[11])
    # Accept Get_Res (0x72) or Get_SNA (0x52)
    if esv not in (0x72, 0x52):
        return {}
    result = {}
    pos = 12
    for _ in range(opc):
        if pos + 2 > len(data):
            break
        epc = data[pos] if isinstance(data[pos], int) else ord(data[pos])
        pdc = data[pos+1] if isinstance(data[pos+1], int) else ord(data[pos+1])
        pos += 2
        if pos + pdc > len(data):
            break
        result[epc] = bytearray(data[pos:pos+pdc])
        pos += pdc
    return result

def decode_measurements(props):
    result = {}

    # D3: coefficient (4-byte unsigned)
    if 0xD3 in props and len(props[0xD3]) >= 4:
        result["coefficient"] = struct.unpack(">I", bytes(props[0xD3][:4]))[0]

    # E1: unit exponent byte
    if 0xE1 in props and len(props[0xE1]) >= 1:
        unit_byte = props[0xE1][0]
        unit_map = {0x00: 1.0, 0x01: 0.1,  0x02: 0.01,   0x03: 0.001, 0x04: 0.0001,
                    0x0A: 10.0, 0x0B: 100.0, 0x0C: 1000.0, 0x0D: 10000.0}
        result["unit_kwh"] = unit_map.get(unit_byte, 1.0)

    # E7: instantaneous power W (4-byte signed)
    if 0xE7 in props and len(props[0xE7]) >= 4:
        result["power_w"] = struct.unpack(">i", bytes(props[0xE7][:4]))[0]

    # E0: cumulative forward kWh (4-byte unsigned × coeff × unit)
    if 0xE0 in props and len(props[0xE0]) >= 4:
        result["energy_forward_raw"] = struct.unpack(">I", bytes(props[0xE0][:4]))[0]

    # E3: cumulative reverse kWh (4-byte unsigned × coeff × unit)
    if 0xE3 in props and len(props[0xE3]) >= 4:
        result["energy_reverse_raw"] = struct.unpack(">I", bytes(props[0xE3][:4]))[0]

    # E8: instantaneous current R,T phase (2×signed short, 0.1A)
    if 0xE8 in props and len(props[0xE8]) >= 4:
        r, t = struct.unpack(">hh", bytes(props[0xE8][:4]))
        result["current_r_a"] = r / 10.0
        result["current_t_a"] = t / 10.0

    return result

def apply_energy_scale(measurements, coeff, unit_kwh):
    c = measurements.get("coefficient", coeff)
    u = measurements.get("unit_kwh", unit_kwh)
    if "energy_forward_raw" in measurements:
        measurements["energy_forward_kwh"] = measurements["energy_forward_raw"] * c * u
    if "energy_reverse_raw" in measurements:
        measurements["energy_reverse_kwh"] = measurements["energy_reverse_raw"] * c * u
    return measurements

# ---------------------------------------------------------------------------
# Send ECHONET Lite Get via SKSENDTO
# ---------------------------------------------------------------------------

def send_el_get(fd, ipv6, tid):
    frame = build_el_get(tid, EPCS)
    # SKSENDTO expects 4-hex-digit payload length and trailing CRLF after raw data.
    cmd = "SKSENDTO 1 {} 0E1A 1 0 {:04X} ".format(ipv6, len(frame))
    serial_write(fd, cmd)
    serial_write(fd, frame)
    serial_write(fd, b"\r\n")

def read_erxudp(fd, timeout=15):
    """Wait for ERXUDP and return payload as bytearray, or None."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = serial_readline(fd, timeout=max(0.5, deadline - time.time()))
        if line is None:
            continue
        if line.startswith("ERXUDP"):
            parts = line.split()
            # Tail fields are stable: ... <secured> <side> <datalen> <data>
            if len(parts) >= 10:
                hex_data = parts[-1].strip()
                if not hex_data.startswith("1081"):
                    continue
                try:
                    return bytearray(binascii.unhexlify(hex_data))
                except Exception as e:
                    log("ERXUDP hex decode error: {}".format(e))
    return None

# ---------------------------------------------------------------------------
# Minimal MQTT 3.1.1 client (raw socket, no paho)
# ---------------------------------------------------------------------------

def _encode_remaining(n):
    buf = b""
    while True:
        byte = n % 128
        n //= 128
        if n > 0:
            byte |= 0x80
        buf += struct.pack("B", byte)
        if n == 0:
            break
    return buf

def _encode_str(s):
    b = s.encode("utf-8")
    return struct.pack(">H", len(b)) + b

class MQTTClient(object):
    def __init__(self, host, port, client_id, username=None, password=None):
        self.host      = host
        self.port      = port
        self.client_id = client_id
        self.username  = username
        self.password  = password
        self.sock      = None
        self._out_queue = collections.deque()

    def connect(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(30)
        # Enable TCP keepalive where available
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            # platform-specific options
            for opt_name, opt_val in (('TCP_KEEPIDLE', 60), ('TCP_KEEPINTVL', 10), ('TCP_KEEPCNT', 3)):
                if hasattr(socket, opt_name):
                    try:
                        s.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt_name), opt_val)
                    except Exception:
                        pass
        except Exception:
            pass

        s.connect((self.host, self.port))

        flags = 0x02  # clean session
        if self.username: flags |= 0x80
        if self.password: flags |= 0x40

        var_hdr = (b"\x00\x04MQTT"
                   + b"\x04"
                   + struct.pack("B", flags)
                   + b"\x00\x3C")   # keep-alive 60s

        payload = _encode_str(self.client_id)
        if self.username: payload += _encode_str(self.username)
        if self.password: payload += _encode_str(self.password)

        remaining = var_hdr + payload
        pkt = b"\x10" + _encode_remaining(len(remaining)) + remaining
        s.sendall(pkt)

        # read CONNACK
        s.settimeout(10)
        ack = b""
        while len(ack) < 4:
            chunk = s.recv(4 - len(ack))
            if not chunk:
                break
            ack += chunk
        s.settimeout(None)

        if len(ack) < 4 or (ack[0] if isinstance(ack[0], int) else ord(ack[0])) != 0x20:
            raise RuntimeError("MQTT: bad CONNACK ({})".format(binascii.hexlify(ack)))
        rc = ack[3] if isinstance(ack[3], int) else ord(ack[3])
        if rc != 0:
            raise RuntimeError("MQTT: connection refused code {}".format(rc))

        self.sock = s
        log("MQTT connected to {}:{}".format(self.host, self.port))

        # flush any queued messages
        try:
            self._flush_queue()
        except Exception as e:
            log("MQTT flush queue error: {}".format(e))

    def _make_pkt(self, topic, payload, retain=False):
        if isinstance(payload, dict):
            payload = json.dumps(payload, separators=(",", ":"))
        topic_b = topic.encode("utf-8")
        payload_b = payload.encode("utf-8") if isinstance(payload, str) else payload
        fixed = 0x30 | (0x01 if retain else 0x00)
        var_hdr = struct.pack(">H", len(topic_b)) + topic_b
        remaining = var_hdr + payload_b
        return struct.pack("B", fixed) + _encode_remaining(len(remaining)) + remaining

    def publish(self, topic, payload, retain=False):
        pkt = self._make_pkt(topic, payload, retain)
        try:
            if not self.sock:
                raise RuntimeError("No MQTT socket")
            self.sock.sendall(pkt)
            return
        except Exception as e:
            log("MQTT publish error: {}".format(e))
            # try reconnect and resend
            try:
                self._reconnect()
            except Exception as e2:
                log("MQTT reconnect failed after publish error: {}".format(e2))
                # queue the message for later delivery
                try:
                    self._out_queue.append((topic, payload, retain))
                except Exception:
                    pass
                return

            try:
                self.sock.sendall(pkt)
                return
            except Exception as e3:
                log("MQTT publish retry failed: {}".format(e3))
                try:
                    self._out_queue.append((topic, payload, retain))
                except Exception:
                    pass

    def _flush_queue(self):
        while self._out_queue and self.sock:
            topic, payload, retain = self._out_queue[0]
            try:
                pkt = self._make_pkt(topic, payload, retain)
                self.sock.sendall(pkt)
                self._out_queue.popleft()
            except Exception as e:
                log("MQTT queued publish failed: {}".format(e))
                break

    def ping(self):
        try:
            self.sock.sendall(b"\xC0\x00")
        except Exception as e:
            log("MQTT ping error: {}".format(e))
            self._reconnect()
            return
        # wait for PINGRESP (should be 0xD0 0x00)
        try:
            r, _, _ = select.select([self.sock], [], [], 5)
            if r:
                resp = self.sock.recv(2)
                if not resp:
                    log("MQTT ping: no response (empty)")
                    self._reconnect()
                elif len(resp) < 2:
                    log("MQTT ping: incomplete response (len={})".format(len(resp)))
                    self._reconnect()
                else:
                    first_byte = resp[0] if isinstance(resp[0], int) else ord(resp[0])
                    if first_byte != 0xD0:
                        log("MQTT ping: unexpected response first_byte=0x{:02X}".format(first_byte))
                        self._reconnect()
            else:
                log("MQTT ping: timeout (no data within 5s)")
                self._reconnect()
        except Exception as e:
            log("MQTT ping recv error: {}".format(e))
            self._reconnect()

    def _reconnect(self):
        log("MQTT reconnecting …")
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        self.sock = None
        while True:
            try:
                self.connect()
                return
            except Exception as e:
                log("MQTT reconnect failed: {} - retry in 15s".format(e))
                time.sleep(15)

# ---------------------------------------------------------------------------
# Home Assistant MQTT auto-discovery
# ---------------------------------------------------------------------------

SENSOR_DEFS = [
    ("power",          "Instantaneous Power",  "W",   "power",   "measurement"),
    ("energy_forward", "Cumulative Energy Fwd", "kWh", "energy",  "total_increasing"),
    ("energy_reverse", "Cumulative Energy Rev", "kWh", "energy",  "total_increasing"),
    ("current_r",      "Current R Phase",       "A",   "current", "measurement"),
    ("current_t",      "Current T Phase",       "A",   "current", "measurement"),
]

def publish_ha_discovery(mqtt, device_id):
    device = {
        "identifiers": [device_id],
        "name":         "Cube J1 Smart Meter",
        "model":        "Cube J1",
        "manufacturer": "NextDrive",
    }
    base = "cubej/{}".format(device_id)
    for sid, name, unit, dev_class, state_class in SENSOR_DEFS:
        topic  = "homeassistant/sensor/{}/{}/config".format(device_id, sid)
        config = {
            "name":               name,
            "unique_id":          "{}_{}".format(device_id, sid),
            "state_topic":        "{}/{}".format(base, sid),
            "unit_of_measurement": unit,
            "device_class":       dev_class,
            "state_class":        state_class,
            "device":             device,
        }
        mqtt.publish(topic, config, retain=True)
        log("HA discovery: {}".format(topic))

def publish_measurements(mqtt, device_id, m):
    base = "cubej/{}".format(device_id)
    if "power_w" in m:
        mqtt.publish("{}/power".format(base), str(m["power_w"]))
    if "energy_forward_kwh" in m:
        mqtt.publish("{}/energy_forward".format(base), "{:.3f}".format(m["energy_forward_kwh"]))
    if "energy_reverse_kwh" in m:
        mqtt.publish("{}/energy_reverse".format(base), "{:.3f}".format(m["energy_reverse_kwh"]))
    if "current_r_a" in m:
        mqtt.publish("{}/current_r".format(base), "{:.1f}".format(m["current_r_a"]))
    if "current_t_a" in m:
        mqtt.publish("{}/current_t".format(base), "{:.1f}".format(m["current_t_a"]))

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global _log_file
    try:
        _log_file = open(LOG_PATH, "a")
        _log_file.write("\n\n" + "=" * 80 + "\n")
        _log_file.flush()
    except Exception:
        pass

    cfg           = load_config()
    br_id         = cfg["br_id"]
    br_pwd        = cfg["br_pwd"]
    ha_host       = cfg["mqtt_host"]
    ha_port       = int(cfg.get("mqtt_port", 1883))
    ha_user       = cfg.get("mqtt_user", "")
    ha_pass       = cfg.get("mqtt_pass", "")
    device_id     = cfg.get("device_id", "cubej1")
    serial_port   = cfg.get("serial_port", "/dev/ttyS1")
    poll_interval = int(cfg.get("poll_interval", 60))
    target_pan_id = cfg.get("target_pan_id") or cfg.get("pan_id")
    target_channel= cfg.get("target_channel") or cfg.get("channel")
    target_addr   = cfg.get("target_addr") or cfg.get("addr")

    log("================================================================================")
    log("=== [START / RESTART] mqtt_bridge started (device_id={}) ===".format(device_id))
    log("================================================================================")

    # Connect MQTT
    mqtt = MQTTClient(ha_host, ha_port, "cubej1_{}".format(device_id),
                      username=ha_user, password=ha_pass)
    while True:
        try:
            mqtt.connect()
            break
        except Exception as e:
            log("MQTT connect failed: {} - retry in 15s".format(e))
            time.sleep(15)

    publish_ha_discovery(mqtt, device_id)

    # Open serial port
    log("Opening serial {}".format(serial_port))
    fd = None
    while True:
        try:
            fd = open_serial(serial_port)
            break
        except Exception as e:
            log("Serial open failed: {} - retry in 10s".format(e))
            time.sleep(10)

    # Wi-SUN join
    ipv6 = None
    join_failures = 0
    while True:
        try:
            ipv6 = wisun_connect(fd, br_id, br_pwd,
                                 target_pan_id=target_pan_id,
                                 target_channel=target_channel,
                                 target_addr=target_addr)
            break
        except Exception as e:
            join_failures += 1
            # A missing meter can persist for hours. Avoid continuous radio
            # scanning and repeated module resets during a long outage.
            retry_after = min(60 * (2 ** min(join_failures - 1, 4)), 900)
            log("Wi-SUN join failed: {} - retry in {}s".format(e, retry_after))
            time.sleep(retry_after)

    log("Meter connected at {}".format(ipv6))

    tid       = 1
    coeff     = 1
    unit_kwh  = 1.0
    last_ping = time.time()
    consecutive_no_response = 0

    while True:
        try:
            orig_led = led_read()
            led_rgb(0, 0, 255)
            try:
                send_el_get(fd, ipv6, tid)
                tid = (tid + 1) & 0xFFFF
                data = read_erxudp(fd, timeout=15)
                if data:
                    consecutive_no_response = 0
                    props = parse_el_response(data)
                    m     = decode_measurements(props)
                    m     = apply_energy_scale(m, coeff, unit_kwh)
                    if "coefficient" in m:
                        coeff = m["coefficient"]
                    if "unit_kwh" in m:
                        unit_kwh = m["unit_kwh"]
                    log("Measurements: {}".format(
                        {k: v for k, v in m.items()
                         if k in ("power_w", "energy_forward_kwh", "energy_reverse_kwh",
                                   "current_r_a", "current_t_a")}))
                    publish_measurements(mqtt, device_id, m)
                else:
                    consecutive_no_response += 1
                    log("No ERXUDP response (timeout; consecutive={}/{})".format(
                        consecutive_no_response, NO_RESPONSE_REJOIN_COUNT))
                    if consecutive_no_response >= NO_RESPONSE_REJOIN_COUNT:
                        raise RuntimeError("{} consecutive meter response timeouts".format(
                            consecutive_no_response))
            finally:
                led_rgb(*orig_led)

            if time.time() - last_ping > 50:
                mqtt.ping()
                last_ping = time.time()

            time.sleep(poll_interval)

        except Exception as e:
            log("Main loop error: {} - reconnecting Wi-SUN in 30s".format(e))
            time.sleep(30)
            try:
                ipv6 = wisun_connect(fd, br_id, br_pwd,
                                     target_pan_id=target_pan_id,
                                     target_channel=target_channel,
                                     target_addr=target_addr)
                consecutive_no_response = 0
                log("Wi-SUN reconnected at {}".format(ipv6))
            except Exception as e2:
                log("Wi-SUN reconnect failed: {}".format(e2))


if __name__ == "__main__":
    main()
