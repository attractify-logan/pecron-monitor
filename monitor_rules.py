"""Automation-rule policy for :class:`monitor.PecronMonitor`."""

import json
import logging
import os
import shlex
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

from constants import SENSOR_FIELDS
from helpers import _get_kv
from monitor_data import extract_power, extract_voltage


log = logging.getLogger("pecron")


class MonitorRulesMixin:
    def _rule_state_path(self) -> Path:
        """Resolve persisted rule-state path."""
        override = os.environ.get("PECRON_RULE_STATE_PATH")
        if override:
            return Path(override)
        configured = self.rule_state_config.get("path") or self.config.get("rule_state_path")
        if configured:
            return Path(configured).expanduser()
        return Path.home() / ".pecron-monitor-rules.json"

    _DEFAULT_STATE_VAR = "default"

    def _initial_rule_states(self) -> dict:
        """Configured initial state variables as a name->value dict.
        `initial_state` may be a plain string (the single `default` variable,
        legacy) or a dict of named variables."""
        initial = self.rule_state_config.get("initial_state")
        if isinstance(initial, dict):
            return {str(k): str(v) for k, v in initial.items()}
        if initial is None:
            return {self._DEFAULT_STATE_VAR: "default"}
        return {self._DEFAULT_STATE_VAR: str(initial)}

    def _load_rule_states(self) -> dict:
        """Load persisted state variables, falling back to the configured initial
        values. Reads both the multi-variable `{"states": {...}}` format and the
        legacy single-state `{"state": "..."}` format."""
        path = self._rule_state_path()
        fallback = self._initial_rule_states()
        if not path.exists():
            return fallback
        try:
            with path.open("r") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            log.warning("Could not load rule state from %s: %s", path, e)
            return fallback
        if not isinstance(data, dict):
            return fallback
        states = data.get("states")
        if isinstance(states, dict) and states:
            return {str(k): str(v) for k, v in states.items()}
        legacy = data.get("state")  # pre-multivar single-state format
        if legacy:
            return {self._DEFAULT_STATE_VAR: str(legacy)}
        return fallback

    def _save_rule_states(self) -> None:
        """Persist current state variables atomically."""
        path = self._rule_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".pecron-rules-", dir=str(path.parent))
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(
                    {"states": self.rule_states, "updated_at": datetime.utcnow().isoformat()},
                    f,
                )
                f.write("\n")
            os.replace(tmp_path, path)
        except Exception:
            try:
                tmp_path.unlink()
            except OSError:
                pass
            raise

    def _set_rule_states(self, updates) -> None:
        """Apply one or more name->value state changes and persist if anything
        changed. A bare string updates the single `default` variable (legacy)."""
        if not isinstance(updates, dict):
            updates = {self._DEFAULT_STATE_VAR: updates}
        changed = {}
        for name, value in updates.items():
            name, value = str(name), str(value)
            if self.rule_states.get(name) != value:
                self.rule_states[name] = value
                changed[name] = value
        if changed:
            self._save_rule_states()
            log.info("Rule state changed: %s", changed)

    def _rule_state_matches(self, condition: dict) -> bool:
        """Whether the current state variables satisfy a rule's state gate.
        `state`/`states` accept the legacy single-variable string/list, or a
        {variable: value} / {variable: [values]} dict for named variables (every
        named variable must match)."""
        if "state" in condition:
            spec = condition["state"]
            if isinstance(spec, dict):
                return all(self.rule_states.get(str(k)) == str(v) for k, v in spec.items())
            return self.rule_states.get(self._DEFAULT_STATE_VAR) == str(spec)
        states = condition.get("states")
        if states is None:
            return True
        if isinstance(states, dict):
            for name, allowed in states.items():
                allowed_set = (
                    {str(allowed)} if isinstance(allowed, str) else {str(a) for a in allowed}
                )
                if self.rule_states.get(str(name)) not in allowed_set:
                    return False
            return True
        if isinstance(states, str):
            return self.rule_states.get(self._DEFAULT_STATE_VAR) == states
        return self.rule_states.get(self._DEFAULT_STATE_VAR) in {str(s) for s in states}

    def _run_rule_command(
        self,
        command,
        *,
        rule: dict,
        action: dict,
        device_key: str,
        target_device_key: str,
        kv: dict,
        battery_pct: int,
    ) -> None:
        """Run a configured external command with rule context on stdin as JSON."""
        if isinstance(command, str):
            argv = shlex.split(command)
        else:
            argv = [str(part) for part in command]
        if not argv:
            raise ValueError("run_command cannot be empty")

        payload = {
            "rule": rule.get("name"),
            "state": self.rule_states.get(self._DEFAULT_STATE_VAR),
            "states": dict(self.rule_states),
            "device_key": device_key,
            "target_device_key": target_device_key,
            "battery_percent": battery_pct,
            "voltage": extract_voltage(kv),
            "data": kv,
        }
        timeout = float(action.get("timeout_seconds", 30))
        result = subprocess.run(
            argv,
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if result.stdout.strip():
            log.info("Rule '%s' command stdout: %s", rule.get("name"), result.stdout.strip())
        if result.stderr.strip():
            log.warning("Rule '%s' command stderr: %s", rule.get("name"), result.stderr.strip())
        if result.returncode != 0:
            raise RuntimeError(f"command exited with status {result.returncode}: {argv[0]}")

    # Trigger condition keys, ANDed together when more than one is present on a
    # rule (#56). `state`/`states` are separate preconditions handled by
    # _rule_state_matches and are intentionally not in this list.
    _TRIGGER_CONDITION_KEYS = (
        "init",
        "battery_below",
        "battery_above",
        "voltage_below",
        "voltage_above",
        "input_power_below",
        "input_power_above",
        "output_power_below",
        "output_power_above",
        "schedule",
        "schedule_between",
    )

    @staticmethod
    def _in_time_window(now_hm: str, start_hm: str, end_hm: str) -> bool:
        """True if now is within [start, end), supporting windows that wrap past
        midnight (e.g. 22:00-06:00). A zero-width window never matches."""

        def _mins(hm: str) -> int:
            hh, mm = str(hm).split(":")
            return int(hh) * 60 + int(mm)

        try:
            now, start, end = _mins(now_hm), _mins(start_hm), _mins(end_hm)
        except (ValueError, AttributeError):
            log.warning("Invalid HH:MM in schedule_between: %r-%r", start_hm, end_hm)
            return False
        if start == end:
            return False
        if start < end:
            return start <= now < end
        return now >= start or now < end

    def _eval_condition_clause(
        self, key: str, condition: dict, kv: dict, battery_pct: int, init: bool
    ) -> bool:
        """Evaluate one trigger clause. Returns True only if satisfied; missing or
        unevaluable telemetry returns False so the (ANDed) rule does not fire."""
        if key == "init":
            # `init` is a startup trigger only when true. `init: false` must not
            # become an "always fires on normal eval" trigger (#56 review).
            return bool(condition.get("init")) and init
        if key == "battery_below":
            return battery_pct >= 0 and battery_pct <= condition["battery_below"]
        if key == "battery_above":
            return battery_pct >= 0 and battery_pct >= condition["battery_above"]
        if key == "voltage_below":
            v = extract_voltage(kv)
            return v is not None and v <= float(condition["voltage_below"])
        if key == "voltage_above":
            v = extract_voltage(kv)
            return v is not None and v >= float(condition["voltage_above"])
        if key == "input_power_below":
            p = extract_power(kv, "total_input_power", "ac_input_power", "dc_input_power")
            return p is not None and p <= condition["input_power_below"]
        if key == "input_power_above":
            p = extract_power(kv, "total_input_power", "ac_input_power", "dc_input_power")
            return p is not None and p >= condition["input_power_above"]
        if key == "output_power_below":
            p = extract_power(kv, "total_output_power", "ac_output_power", "dc_output_power")
            return p is not None and p <= condition["output_power_below"]
        if key == "output_power_above":
            p = extract_power(kv, "total_output_power", "ac_output_power", "dc_output_power")
            return p is not None and p >= condition["output_power_above"]
        if key == "schedule":
            return datetime.now().strftime("%H:%M") == condition["schedule"]
        if key == "schedule_between":
            window = condition["schedule_between"]
            if not isinstance(window, (list, tuple)) or len(window) != 2:
                log.warning("schedule_between must be [start, end] HH:MM, got %r", window)
                return False
            return self._in_time_window(datetime.now().strftime("%H:%M"), window[0], window[1])
        return False

    def _evaluate_rules(self, device_key: str, kv: dict, battery_pct: int, *, init: bool = False):
        """Evaluate automation rules against current state."""
        # Sanity check: prevent rule triggers on invalid non-init data.
        voltage = extract_voltage(kv) or 0
        if not init and battery_pct <= 0 and voltage == 0:
            log.debug(
                "Skipping rule evaluation for %s: invalid data (battery=%d%%, voltage=%.1fV)",
                device_key,
                battery_pct,
                voltage,
            )
            return

        for rule in self.rules:
            if rule.get("device_key") and rule["device_key"] != device_key:
                continue

            try:
                condition = rule.get("condition", {})
                action = rule.get("action", {})

                if not self._rule_state_matches(condition):
                    continue

                # Check condition. `state`/`states` are preconditions handled
                # above, not triggers by themselves. Every trigger key present is
                # ANDed (#56): e.g. voltage_below + output_power_below fires only
                # when both hold. A rule with no trigger key (state gate only)
                # never fires. A clause that can't be evaluated (missing
                # telemetry) returns False, so the rule does not fire.
                present = [k for k in self._TRIGGER_CONDITION_KEYS if k in condition]
                if not present:
                    continue
                if not all(
                    self._eval_condition_clause(k, condition, kv, battery_pct, init)
                    for k in present
                ):
                    continue

                # Check cooldown
                rule_id = rule.get("name", str(rule))
                cooldown = rule.get("cooldown_minutes", 5) * 60
                now_ts = time.time()
                last = self.last_alert.get(f"rule_{rule_id}", 0)
                if now_ts - last < cooldown:
                    continue
                self.last_alert[f"rule_{rule_id}"] = now_ts

                # Execute action
                target_dk = action.get("device_key", device_key)

                # Safety check: verify target device has the required controls
                target_device = None
                for dev in self.devices:
                    if dev.get("device_key") == target_dk:
                        target_device = dev
                        break

                if not target_device:
                    log.warning(
                        "Rule '%s': target device %s not found, skipping",
                        rule.get("name"),
                        target_dk,
                    )
                    continue

                target_controls = target_device.get("controls", {})

                if "set_ac" in action:
                    if "ac_switch_hm" in target_controls:
                        self.set_ac(target_dk, action["set_ac"])
                        log.info(
                            "Rule '%s': set AC=%s on %s",
                            rule.get("name"),
                            action["set_ac"],
                            target_dk,
                        )
                    else:
                        log.warning(
                            "Rule '%s': device %s does not have AC control, skipping action",
                            rule.get("name"),
                            target_dk,
                        )

                if "set_dc" in action:
                    if "dc_switch_hm" in target_controls:
                        self.set_dc(target_dk, action["set_dc"])
                        log.info(
                            "Rule '%s': set DC=%s on %s",
                            rule.get("name"),
                            action["set_dc"],
                            target_dk,
                        )
                    else:
                        log.warning(
                            "Rule '%s': device %s does not have DC control, skipping action",
                            rule.get("name"),
                            target_dk,
                        )

                if "set_ups" in action:
                    if "ups_status_hm" in target_controls:
                        self.set_ups(target_dk, action["set_ups"])
                        log.info(
                            "Rule '%s': set UPS=%s on %s",
                            rule.get("name"),
                            action["set_ups"],
                            target_dk,
                        )
                    else:
                        log.warning(
                            "Rule '%s': device %s does not have UPS control, skipping action",
                            rule.get("name"),
                            target_dk,
                        )

                if "set_state" in action:
                    self._set_rule_states(action["set_state"])
                    log.info("Rule '%s': set state=%s", rule.get("name"), action["set_state"])

                if "run_command" in action:
                    self._run_rule_command(
                        action["run_command"],
                        rule=rule,
                        action=action,
                        device_key=device_key,
                        target_device_key=target_dk,
                        kv=kv,
                        battery_pct=battery_pct,
                    )

            except Exception as e:
                log.error("Rule evaluation error: %s", e)

    def _run_init_rules(self) -> None:
        """Evaluate rules with condition.init once after devices are available."""
        if self._init_rules_ran:
            return
        self._init_rules_ran = True
        for device in self.devices:
            device_key = device["device_key"]
            kv = self.latest_data.get(device_key, {})
            battery_pct = int(_get_kv(kv, SENSOR_FIELDS["battery_percent"], -1))
            self._evaluate_rules(device_key, kv, battery_pct, init=True)
