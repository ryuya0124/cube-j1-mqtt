"""Small, persistent recovery state machine for Cube J1."""

from dataclasses import dataclass, field


DAY = 24 * 60 * 60


@dataclass
class Policy:
    stale_after: int = 600
    bridge_grace: int = 420
    reboot_grace: int = 600
    power_cycle_grace: int = 900
    cooldown: int = 3600
    max_bridge_per_day: int = 4
    max_reboot_per_day: int = 2
    max_power_cycle_per_day: int = 4
    power_cycle_min_interval: int = 21600
    power_cycle_enabled: bool = False
    phase: str = "normal"
    phase_at: float = 0
    cooldown_until: float = 0
    last_message_at: float = 0
    actions: list = field(default_factory=list)
    radio_deferrals: int = 0

    @classmethod
    def from_saved(cls, config, saved):
        obj = cls(**config)
        obj.phase = saved.get("phase", "normal")
        if obj.phase not in ("normal", "bridge_wait", "reboot_wait",
                             "power_cycle_wait", "cooldown"):
            obj.phase = "normal"
        obj.phase_at = float(saved.get("phase_at", 0))
        obj.cooldown_until = float(saved.get("cooldown_until", 0))
        obj.last_message_at = float(saved.get("last_message_at", 0))
        obj.actions = [a for a in saved.get("actions", [])
                       if a.get("type") in ("bridge", "reboot", "power_cycle")
                       and isinstance(a.get("at"), (int, float))]
        obj.radio_deferrals = int(saved.get("radio_deferrals", 0))
        return obj

    def saved(self):
        return {"phase": self.phase, "phase_at": self.phase_at,
                "cooldown_until": self.cooldown_until,
                "last_message_at": self.last_message_at, "actions": self.actions,
                "radio_deferrals": self.radio_deferrals,
                "power_cycle_enabled": self.power_cycle_enabled}

    def observe(self, now):
        previous = self.phase
        self.last_message_at = now
        self.phase = "normal"
        self.phase_at = now
        self.cooldown_until = 0
        self.radio_deferrals = 0
        return previous != "normal"

    def decide(self, now, broker_since, started_at):
        """Return (action, reason). Never act unless MQTT is currently connected."""
        if broker_since is None:
            return None, None
        self.actions = [a for a in self.actions if now - a["at"] < DAY]
        if self.phase == "cooldown":
            if now < self.cooldown_until:
                return None, None
            self.phase = "normal"
        if self.phase == "normal":
            anchor = max(self.last_message_at, broker_since, started_at)
            if now - anchor < self.stale_after:
                return None, None
            action, reason = "bridge", "no live power message for {}s".format(int(now - anchor))
        elif self.phase == "bridge_wait":
            if now - max(self.phase_at, broker_since) < self.bridge_grace:
                return None, None
            action, reason = "reboot", "no live power message after bridge restart"
        elif self.phase == "reboot_wait":
            if now - max(self.phase_at, broker_since) < self.reboot_grace:
                return None, None
            if self.power_cycle_enabled:
                action, reason = "power_cycle", "no live power message after Cube reboot"
            else:
                self.phase = "cooldown"
                self.cooldown_until = now + self.cooldown
                return None, "reboot grace expired; smart-plug recovery is not armed"
        elif self.phase == "power_cycle_wait":
            if now - max(self.phase_at, broker_since) >= self.power_cycle_grace:
                self.phase = "cooldown"
                self.cooldown_until = now + self.cooldown
                return None, "power-cycle grace expired; cooling down"
            return None, None
        else:
            return None, None

        caps = {
            "bridge": self.max_bridge_per_day,
            "reboot": self.max_reboot_per_day,
            "power_cycle": self.max_power_cycle_per_day,
        }
        cap = caps[action]
        recent = [a["at"] for a in self.actions if a["type"] == action]
        if len(recent) >= cap:
            self.phase = "cooldown"
            self.cooldown_until = max(now + self.cooldown, min(recent) + DAY)
            return None, "daily {} limit reached; cooling down".format(action)
        if (action == "power_cycle" and recent
                and now - max(recent) < self.power_cycle_min_interval):
            self.phase = "cooldown"
            self.cooldown_until = max(recent) + self.power_cycle_min_interval
            return None, "power-cycle minimum interval has not elapsed"
        return action, reason

    def start_action(self, action, now):
        phases = {
            "bridge": "bridge_wait",
            "reboot": "reboot_wait",
            "power_cycle": "power_cycle_wait",
        }
        self.phase = phases[action]
        self.phase_at = now

    def action_succeeded(self, action, now):
        """Only commands that reached Cube count against the daily limit."""
        self.actions.append({"type": action, "at": now})

    def action_failed(self, action, now):
        expected = {
            "bridge": "bridge_wait",
            "reboot": "reboot_wait",
            "power_cycle": "power_cycle_wait",
        }[action]
        if self.phase == expected:
            self.phase = "cooldown"
            self.cooldown_until = now + min(self.cooldown, 900)

    def defer_for_radio_search(self, now):
        """Let a responsive Wi-SUN scan continue without restarting Cube."""
        self.radio_deferrals += 1
        self.phase = "cooldown"
        self.phase_at = now
        self.cooldown_until = now + min(self.cooldown, 900)

    def defer_for_cube_recovery(self, now):
        """Give Cube's own supervisor one short window before intervening."""
        self.phase = "cooldown"
        self.phase_at = now
        self.cooldown_until = now + min(self.cooldown, 900)
