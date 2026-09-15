#!/usr/bin/env python3
"""Observe one MQTT topic; recover Cube J1 only after sustained silence."""

import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import subprocess
import threading
import time
from datetime import datetime

import paho.mqtt.client as mqtt

from policy import Policy
from tapo_power import TapoPowerController


DATA = Path("/data")
STATE = DATA / "state.json"
SECRET = Path("/run/secrets/mqtt.json")
SSH_KEY = Path("/run/secrets/cube_ssh_key")
SSH_KNOWN_HOSTS = Path("/run/secrets/cube_known_hosts")
TAPO_SECRET = Path("/run/secrets/tapo.json")
CUBE_IP = os.environ.get("CUBE_IP", "192.168.3.33")
SERIAL = CUBE_IP + ":5555"


def event(name, **fields):
    record = {"time": datetime.now().astimezone().isoformat(timespec="seconds"),
              "event": name, **fields}
    logging.info(json.dumps(record, ensure_ascii=False, sort_keys=True))


def save_state(policy):
    tmp = STATE.with_suffix(".tmp")
    with tmp.open("w") as f:
        json.dump(policy.saved(), f, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE)


def adb(*args, timeout=15):
    result = subprocess.run(["adb", *args], capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip()[:240] or "adb failed")
    return result.stdout.strip()


def ssh_command():
    return ["ssh", "-i", str(SSH_KEY), "-o", "BatchMode=yes",
               "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes",
               "-o", "UserKnownHostsFile=" + str(SSH_KNOWN_HOSTS),
               "-o", "ConnectTimeout=5", "root@" + CUBE_IP]


def radio_search_active():
    """True only while Cube and its Wi-SUN module are still completing scans."""
    remote = ("p=$(pgrep -f '[m]qtt_bridge.py' | head -n 1); "
              "test -n \"$p\" && "
              "test $(( $(date +%s) - $(stat -c %Y /data/local/mqtt_bridge.log) )) -lt 300 && "
              "tail -n 50 /data/local/mqtt_bridge.log | "
              "grep -q 'SKSCAN completed (EVENT 22 received)' && "
              "tail -n 250 /data/local/mqtt_bridge.log | "
              "grep -q 'Wi-SUN join failed: SKSCAN: no candidate PAN found' && "
              "! tail -n 50 /data/local/mqtt_bridge.log | "
              "grep -Eq 'Meter connected at|Measurements:'")
    try:
        result = subprocess.run(ssh_command() + [remote], capture_output=True,
                                text=True, timeout=10)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def recover_ssh(action):
    command = ssh_command()

    def run(remote, timeout=12):
        result = subprocess.run(command + [remote], capture_output=True,
                                text=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout).strip()[:240] or "ssh failed")
        return result.stdout.strip()

    model = run("/system/bin/getprop ro.product.model")
    if model.lower() != "cubej":
        raise RuntimeError("unexpected SSH model: {}".format(model[:80]))
    if action == "bridge":
        run("/system/bin/setprop ctl.restart mqtt_ha_bridge")
    elif action == "reboot":
        run("/system/bin/sh -c 'sleep 2; /system/bin/reboot' </dev/null >/dev/null 2>&1 &")
    else:
        raise ValueError(action)


def recover(action, tapo):
    if action == "power_cycle":
        import asyncio
        result = asyncio.run(tapo.power_cycle())
        return "tapo_local", result
    try:
        connection = adb("connect", SERIAL, timeout=12)
        if "failed" in connection.lower() or "unable" in connection.lower():
            raise RuntimeError(connection[:240])
        model = adb("-s", SERIAL, "shell", "getprop", "ro.product.model", timeout=10)
        if model.lower() != "cubej":
            raise RuntimeError("unexpected ADB model: {}".format(model[:80]))
    except Exception as exc:
        event("adb_unavailable", error=str(exc)[:240], fallback="ssh")
        recover_ssh(action)
        return "ssh", None

    if action == "bridge":
        adb("-s", SERIAL, "shell", "setprop", "ctl.restart", "mqtt_ha_bridge", timeout=10)
    elif action == "reboot":
        adb("-s", SERIAL, "reboot", timeout=10)
    else:
        raise ValueError(action)
    return "adb", None


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    handlers = [logging.StreamHandler(),
                RotatingFileHandler(DATA / "events.jsonl", maxBytes=10_000_000, backupCount=5)]
    logging.basicConfig(level=logging.INFO, format="%(message)s", handlers=handlers)
    config = json.loads(SECRET.read_text())
    tapo_error = None
    try:
        tapo = TapoPowerController.from_path(TAPO_SECRET) if TAPO_SECRET.exists() \
            else TapoPowerController({})
    except Exception as exc:
        tapo = TapoPowerController({})
        tapo_error = "{}: {}".format(type(exc).__name__, str(exc)[:160])
    policy_config = {
        "stale_after": int(os.environ.get("STALE_AFTER", "600")),
        "bridge_grace": int(os.environ.get("BRIDGE_GRACE", "420")),
        "reboot_grace": int(os.environ.get("REBOOT_GRACE", "600")),
        "cooldown": int(os.environ.get("COOLDOWN", "3600")),
        "max_bridge_per_day": int(os.environ.get("MAX_BRIDGE_PER_DAY", "4")),
        "max_reboot_per_day": int(os.environ.get("MAX_REBOOT_PER_DAY", "2")),
        "power_cycle_grace": int(os.environ.get("POWER_CYCLE_GRACE", "900")),
        "max_power_cycle_per_day": int(os.environ.get("MAX_POWER_CYCLE_PER_DAY", "4")),
        "power_cycle_min_interval": int(os.environ.get("POWER_CYCLE_MIN_INTERVAL", "21600")),
        "power_cycle_enabled": tapo.armed,
    }
    saved = json.loads(STATE.read_text()) if STATE.exists() else {}
    if tapo.armed and not bool(saved.get("power_cycle_enabled", False)):
        # Enabling a new plug must start a fresh recovery ladder. A persisted
        # reboot_wait state must never cause an immediate first power cut.
        saved["phase"] = "normal"
        saved["phase_at"] = 0
    policy = Policy.from_saved(policy_config, saved)
    lock = threading.Lock()
    started_at = time.time()
    broker_since = None
    last_saved_sample = 0
    topic = config.get("topic", "cubej/cubej1/power")
    status_topic = config.get("status_topic", "cubej/cubej1/status")
    status_stale = int(os.environ.get("CUBE_STATUS_STALE", "180"))
    self_recovery_grace = int(os.environ.get("CUBE_SELF_RECOVERY_GRACE", "1800"))
    max_radio_deferrals = int(os.environ.get("MAX_RADIO_DEFERRALS", "2"))
    radio_status_stale = int(os.environ.get("RADIO_STATUS_STALE", "1200"))
    last_status_at = 0
    cube_status = {}
    event("agent_started", topic=topic, status_topic=status_topic, cube=CUBE_IP,
          thresholds=policy_config, phase=policy.phase,
          smart_plug_armed=tapo.armed, smart_plug_config_error=tapo_error)

    def on_connect(client, userdata, flags, reason_code, properties):
        nonlocal broker_since
        if reason_code.is_failure:
            event("mqtt_connect_rejected", reason=str(reason_code))
            return
        with lock:
            broker_since = time.time()
        result, _ = client.subscribe([(topic, 0), (status_topic, 0)])
        event("mqtt_connected", subscribe_result=int(result), topic=topic,
              status_topic=status_topic)

    def on_disconnect(client, userdata, flags, reason_code, properties):
        nonlocal broker_since
        with lock:
            was_connected = broker_since is not None
            broker_since = None
        if was_connected:
            event("mqtt_disconnected", reason=str(reason_code))

    def on_message(client, userdata, message):
        nonlocal last_saved_sample, last_status_at, cube_status
        if message.topic == status_topic:
            try:
                status = json.loads(message.payload)
                heartbeat_at = float(status.get("heartbeat_at", 0))
                now = time.time()
                if abs(now - heartbeat_at) > status_stale:
                    return
                with lock:
                    last_status_at = now
                    cube_status = status
            except (ValueError, TypeError, UnicodeDecodeError):
                return
            return
        if message.topic != topic:
            return
        if message.retain:
            return
        try:
            value = float(message.payload)
            if not math.isfinite(value):
                return
        except (ValueError, TypeError):
            return
        now = time.time()
        with lock:
            first = policy.last_message_at == 0
            recovered = policy.observe(now)
            if recovered or first or now - last_saved_sample > 60:
                save_state(policy)
                last_saved_sample = now
        if recovered:
            event("readings_restored", power_w=value)
        elif first:
            event("first_live_reading", power_w=value)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="cube-j1-watchdog")
    if config.get("user"):
        client.username_pw_set(config["user"], config.get("password", ""))
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=2, max_delay=60)
    client.connect_async(config["host"], int(config.get("port", 1883)), keepalive=60)
    client.loop_start()
    try:
        while True:
            time.sleep(5)
            with lock:
                now = time.time()
                before = policy.phase
                action, reason = policy.decide(now, broker_since, started_at)
                if policy.phase != before:
                    save_state(policy)
            if reason and not action:
                event("recovery_deferred", reason=reason, until=policy.cooldown_until)
            if not action:
                continue
            with lock:
                status_age = time.time() - last_status_at if last_status_at else float("inf")
                status_state = str(cube_status.get("state", ""))
                radio_at = float(cube_status.get("radio_at", 0) or 0)
                anchor = max(policy.last_message_at, broker_since or 0, started_at)
                outage_age = time.time() - anchor
            cube_alive = status_age <= status_stale
            radio_age = time.time() - radio_at if radio_at else float("inf")
            cube_radio_active = cube_alive and radio_age <= radio_status_stale and status_state in {
                "wisun_initializing", "wisun_direct_join", "wisun_scan", "wisun_join",
                "wisun_backoff", "meter_timeout", "serial_open", "serial_wait"
            }
            if action == "bridge" and cube_alive and outage_age < self_recovery_grace:
                with lock:
                    current, _ = policy.decide(time.time(), broker_since, started_at)
                    if current == action:
                        policy.defer_for_cube_recovery(time.time())
                        save_state(policy)
                        event("recovery_deferred",
                              reason="Cube heartbeat is fresh; allowing local recovery",
                              action=action, cube_state=status_state,
                              status_age_s=int(status_age), until=policy.cooldown_until)
                continue
            radio_active = cube_radio_active
            if not radio_active and not cube_alive:
                radio_active = radio_search_active()
            if radio_active:
                with lock:
                    current, _ = policy.decide(time.time(), broker_since, started_at)
                    can_defer = policy.radio_deferrals < max_radio_deferrals
                    if current == action and can_defer:
                        policy.defer_for_radio_search(time.time())
                        save_state(policy)
                        event("recovery_deferred",
                              reason="Wi-SUN recovery is responsive; bounded deferral",
                              action=action, cube_state=status_state or "remote_log",
                              radio_age_s=int(radio_age) if math.isfinite(radio_age) else None,
                              deferrals=policy.radio_deferrals, until=policy.cooldown_until)
                        continue
            with lock:
                current, reason = policy.decide(time.time(), broker_since, started_at)
                if current != action:
                    continue
                policy.start_action(action, time.time())
                save_state(policy)
            event("recovery_requested", action=action, reason=reason)
            try:
                method, details = recover(action, tapo)
            except Exception as exc:
                with lock:
                    if getattr(exc, "relay_was_touched", False):
                        # Count an ambiguous relay attempt so it cannot repeat early.
                        policy.action_succeeded(action, time.time())
                    else:
                        policy.action_failed(action, time.time())
                    save_state(policy)
                event("recovery_failed", action=action, error=str(exc)[:240],
                      relay_was_touched=bool(getattr(exc, "relay_was_touched", False)))
            else:
                with lock:
                    policy.action_succeeded(action, time.time())
                    save_state(policy)
                safe_details = None
                if details:
                    safe_details = {key: value for key, value in details.items()
                                    if key not in {"device_id"}}
                event("recovery_command_sent", action=action, method=method,
                      details=safe_details)
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
