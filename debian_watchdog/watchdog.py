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


DATA = Path("/data")
STATE = DATA / "state.json"
SECRET = Path("/run/secrets/mqtt.json")
SSH_KEY = Path("/run/secrets/cube_ssh_key")
SSH_KNOWN_HOSTS = Path("/run/secrets/cube_known_hosts")
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


def recover_ssh(action):
    command = ["ssh", "-i", str(SSH_KEY), "-o", "BatchMode=yes",
               "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes",
               "-o", "UserKnownHostsFile=" + str(SSH_KNOWN_HOSTS),
               "-o", "ConnectTimeout=5", "root@" + CUBE_IP]

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


def recover(action):
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
        return "ssh"

    if action == "bridge":
        adb("-s", SERIAL, "shell", "setprop", "ctl.restart", "mqtt_ha_bridge", timeout=10)
    elif action == "reboot":
        adb("-s", SERIAL, "reboot", timeout=10)
    else:
        raise ValueError(action)
    return "adb"


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    handlers = [logging.StreamHandler(),
                RotatingFileHandler(DATA / "events.jsonl", maxBytes=10_000_000, backupCount=5)]
    logging.basicConfig(level=logging.INFO, format="%(message)s", handlers=handlers)
    config = json.loads(SECRET.read_text())
    policy_config = {
        "stale_after": int(os.environ.get("STALE_AFTER", "600")),
        "bridge_grace": int(os.environ.get("BRIDGE_GRACE", "420")),
        "reboot_grace": int(os.environ.get("REBOOT_GRACE", "600")),
        "cooldown": int(os.environ.get("COOLDOWN", "3600")),
        "max_bridge_per_day": int(os.environ.get("MAX_BRIDGE_PER_DAY", "4")),
        "max_reboot_per_day": int(os.environ.get("MAX_REBOOT_PER_DAY", "2")),
    }
    saved = json.loads(STATE.read_text()) if STATE.exists() else {}
    policy = Policy.from_saved(policy_config, saved)
    lock = threading.Lock()
    started_at = time.time()
    broker_since = None
    last_saved_sample = 0
    topic = config.get("topic", "cubej/cubej1/power")
    event("agent_started", topic=topic, cube=CUBE_IP, thresholds=policy_config, phase=policy.phase)

    def on_connect(client, userdata, flags, reason_code, properties):
        nonlocal broker_since
        if reason_code.is_failure:
            event("mqtt_connect_rejected", reason=str(reason_code))
            return
        with lock:
            broker_since = time.time()
        result, _ = client.subscribe(topic, qos=0)
        event("mqtt_connected", subscribe_result=int(result), topic=topic)

    def on_disconnect(client, userdata, flags, reason_code, properties):
        nonlocal broker_since
        with lock:
            was_connected = broker_since is not None
            broker_since = None
        if was_connected:
            event("mqtt_disconnected", reason=str(reason_code))

    def on_message(client, userdata, message):
        nonlocal last_saved_sample
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
                if action:
                    policy.start_action(action, now)
                    save_state(policy)
                elif policy.phase != before:
                    save_state(policy)
            if reason and not action:
                event("recovery_deferred", reason=reason, until=policy.cooldown_until)
            if not action:
                continue
            event("recovery_requested", action=action, reason=reason)
            try:
                method = recover(action)
            except Exception as exc:
                with lock:
                    policy.action_failed(action, time.time())
                    save_state(policy)
                event("recovery_failed", action=action, error=str(exc)[:240])
            else:
                event("recovery_command_sent", action=action, method=method)
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
