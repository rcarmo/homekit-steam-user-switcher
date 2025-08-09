#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import logging
import signal
import socket
import uuid
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

from pyhap.accessory import Accessory
from pyhap.accessory_driver import AccessoryDriver
from pyhap.const import CATEGORY_TELEVISION
import vdf

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("homekit-steam-user-switcher")

PAIRING_CODE = "111-11-111"
PAIRING_PIN_BYTES = PAIRING_CODE.encode("utf-8")
# Persist pairing/state under XDG default state directory
# Defaults to ~/.local/state/homekit-steam-user-switcher unless XDG_STATE_HOME is set
PERSIST_DIR = Path(os.getenv("XDG_STATE_HOME", Path.home() / ".local/state")) / "homekit-steam-user-switcher"
# Default name (can also be set via env var HOMEKIT_TV_NAME)
DEFAULT_TV_NAME = os.getenv("HOMEKIT_TV_NAME", "Steam Switcher")
DEFAULT_MFR = os.getenv("HOMEKIT_TV_MFR", "HomeSteam")
# Use a stable model string (do not mirror the Name), so Home doesn't substitute Model for the default name
DEFAULT_MODEL = os.getenv("HOMEKIT_TV_MODEL", "Steam User Switcher")
DEFAULT_FW = os.getenv("HOMEKIT_TV_FW", "1.0")

# Paths compatible with Steam on Linux (same as in switch.py)
LOGIN_USERS = Path(Path.home() / ".local/share/Steam/config/loginusers.vdf")
REGISTRY = Path(Path.home() / ".steam/registry.vdf")

# Built at runtime from Steam accounts; shape: List[(id, label, slug)]
INPUTS: List[Tuple[int, str, str]] = []

# --- Handler hooks: customize these to integrate with external scripts/services ---
def on_power_changed(is_on: bool) -> None:
    """Called whenever the TV power state changes.

    When turning off, restart Steam.
    """
    logger.info("[handler] power=%s", "on" if is_on else "off")
    if not is_on:
        try:
            logger.info("Restarting Steam (killall steam)...")
            subprocess.run(["killall", "steam"], check=False)
        except Exception:
            logger.exception("Failed to restart Steam on power off")


def on_input_changed(identifier: int, label: str, slug: str) -> None:
    """Called whenever the active input changes.

    Here, each input corresponds to a Steam account's AccountName. We set the
    AutoLoginUser in Steam's registry.vdf to that account (no immediate restart).
    Restart will happen when the TV is turned off from HomeKit.
    """
    logger.info("[handler] input=%s id=%s slug=%s", label, identifier, slug)
    try:
        set_account(slug)
        logger.info("Set Steam AutoLoginUser to %s", slug)
    except Exception:
        logger.exception("Failed to set Steam AutoLoginUser to %s", slug)
# --- End handler hooks ---


def _ensure_char(service, char_name: str):
    """Ensure a characteristic exists on service; try to preload optional ones."""
    try:
        return service.get_characteristic(char_name)
    except Exception:
        try:
            service.add_preload_characteristic(char_name)
            return service.get_characteristic(char_name)
        except Exception:
            return None


def _slugify_label(label: str) -> str:
    return "".join(ch for ch in label.lower() if ch.isalnum())


def _guess_input_type(name: str) -> int:
    """Return HAP InputSourceType based on a human-friendly name.

    Mapping based on HAP spec:
    0 Other, 1 HomeScreen, 2 Tuner, 3 HDMI, 4 CompositeVideo, 5 SVideo,
    6 ComponentVideo, 7 DVI, 8 AirPlay, 9 USB, 10 Application.
    """
    s = name.lower()
    if "hdmi" in s:
        return 3
    if "airplay" in s or "cast" in s:
        return 8
    if s.startswith("app") or "app" in s:
        return 10
    if "usb" in s:
        return 9
    if "dvi" in s:
        return 7
    if "component" in s:
        return 6
    if "svideo" in s:
        return 5
    if "composite" in s or s in {"av", "video"}:
        return 4
    if s in {"tuner", "tv", "antenna"}:
        return 2
    if "home" in s:
        return 1
    return 0


def _default_serial() -> str:
    try:
        return f"TV-{uuid.getnode():012x}"
    except Exception:
        return "TV-000000000000"

# --- Steam helpers (adapted from switch.py) ---
def get_accounts():
    with open(LOGIN_USERS, 'r') as f:
        user_registry = vdf.load(f)
    return user_registry['users']


def get_account():
    with open(REGISTRY, 'r') as f:
        registry = vdf.load(f)
    return registry['Registry']['HKCU']['Software']['Valve']['Steam']['AutoLoginUser']


def set_account(account: str):
    with open(REGISTRY, 'r') as f:
        registry = vdf.load(f)
    registry['Registry']['HKCU']['Software']['Valve']['Steam']['AutoLoginUser'] = account
    with open(REGISTRY, 'w') as f:
        vdf.dump(registry, f, pretty=True)
# --- end Steam helpers ---


class TelevisionAccessory(Accessory):
    category = CATEGORY_TELEVISION

    # Accept the driver explicitly and pass it to the base Accessory
    def __init__(self, driver: AccessoryDriver, name: str, input_items: List[Tuple[int, str, str]], initial_identifier: Optional[int] = None):
        super().__init__(driver, name)
        self.inputs = input_items  # list of (identifier, label, slug)
        self.id_to_label = {i: label for i, label, _ in input_items}
        self.id_to_slug = {i: slug for i, _, slug in input_items}
        self.active_identifier = initial_identifier if initial_identifier is not None else (input_items[0][0] if input_items else 0)
        self.is_active = 0
        self._power_restore_handle = None  # asyncio TimerHandle for auto-restore

        # Populate AccessoryInformation so Home suggests a better default name
        serial = os.getenv("HOMEKIT_TV_SN", _default_serial())
        try:
            # Preferred API in HAP-python
            self.set_info_service(
                manufacturer=DEFAULT_MFR,
                model=DEFAULT_MODEL,
                serial_number=serial,
                firmware_revision=DEFAULT_FW,
            )
            # Ensure AccessoryInformation.Name matches the accessory display name
            info = self.get_service("AccessoryInformation")
            if info:
                _ensure_char(info, "Name") and info.configure_char("Name", value=name)
        except Exception:
            # Fallback: set characteristics directly
            info = self.get_service("AccessoryInformation")
            if info:
                _ensure_char(info, "Manufacturer") and info.configure_char("Manufacturer", value=DEFAULT_MFR)
                _ensure_char(info, "Model") and info.configure_char("Model", value=DEFAULT_MODEL)
                _ensure_char(info, "SerialNumber") and info.configure_char("SerialNumber", value=serial)
                _ensure_char(info, "FirmwareRevision") and info.configure_char("FirmwareRevision", value=DEFAULT_FW)
            # Also set the AccessoryInformation Name to match the display name
            if info:
                _ensure_char(info, "Name") and info.configure_char("Name", value=name)

        # Create Television service
        self.tv_service = self.add_preload_service(
            "Television",
            chars=["Active", "ActiveIdentifier", "ConfiguredName", "SleepDiscoveryMode", "Name"],
        )
        # Basic Television characteristics
        self.tv_service.configure_char("Active", setter_callback=self.set_active)
        self.tv_service.configure_char("ActiveIdentifier", setter_callback=self.set_active_identifier)
        # Set both ConfiguredName and Name to ensure default presentation in Home
        self.tv_service.configure_char("ConfiguredName", value=name)
        self.tv_service.configure_char("Name", value=name)
        self.tv_service.configure_char("SleepDiscoveryMode", value=1)  # Always discovered

        # No RemoteKey handling needed

        # Mark Television as primary so HomeKit treats it as the main service
        try:
            # Newer HAP-python
            self.tv_service.is_primary_service = True
        except Exception:
            # Fallbacks for older versions
            try:
                self.tv_service.is_primary = True
            except Exception:
                pass

        # Add input sources and link them
        self.input_services = []
        for identifier, label, slug in self.inputs:
            # Preload all characteristics needed by InputSource to avoid "Characteristic not found"
            input_service = self.add_preload_service(
                "InputSource",
                chars=[
                    "Identifier",
                    "ConfiguredName",
                    "Name",
                    "IsConfigured",
                    "CurrentVisibilityState",
                    "TargetVisibilityState",
                    "InputSourceType",
                ],
            )
            input_service.configure_char("Identifier", value=int(identifier))
            input_service.configure_char("IsConfigured", value=1)  # Configured
            input_service.configure_char("CurrentVisibilityState", value=0)  # Shown
            # Set ConfiguredName first, then Name (some clients use one or the other)
            input_service.configure_char("ConfiguredName", value=label)
            input_service.configure_char("Name", value=label)
            # TargetVisibilityState is optional; guard it
            if _ensure_char(input_service, "TargetVisibilityState"):
                input_service.configure_char("TargetVisibilityState", value=0)
            input_service.configure_char("InputSourceType", value=_guess_input_type(label))

            # Give each InputSource a stable subtype so HomeKit can persist names across restarts
            try:
                input_service.subtype = slug
            except Exception:
                pass

            # Link input source to TV service per HAP spec
            self.tv_service.add_linked_service(input_service)
            self.input_services.append(input_service)
        # Default values (set before advertising to HomeKit)
        self.tv_service.get_characteristic("Active").set_value(self.is_active)
        self.tv_service.get_characteristic("ActiveIdentifier").set_value(self.active_identifier)

    def _restore_power(self) -> None:
        """Flip power back on after a delay."""
        self._power_restore_handle = None
        self.is_active = 1
        # Update characteristic so HomeKit reflects ON
        try:
            self.tv_service.get_characteristic("Active").set_value(1)
        except Exception:
            logger.exception("Failed to set Active=1 during auto-restore")
        logger.info("Auto-restored power to On after delay")
        on_power_changed(True)

    # Callbacks
    def set_active(self, value):
        # Cancel any pending auto-restore when turning on
        if value == 1 and self._power_restore_handle:
            try:
                self._power_restore_handle.cancel()
            except Exception:
                pass
            self._power_restore_handle = None
        self.is_active = value
        logger.info("Power %s", "On" if value == 1 else "Off")
        on_power_changed(value == 1)
        # If turned off, schedule auto-restore in 2 seconds
        if value == 0:
            try:
                if self._power_restore_handle:
                    self._power_restore_handle.cancel()
                self._power_restore_handle = self.driver.loop.call_later(2.0, self._restore_power)
                logger.debug("Scheduled auto-restore in 2s")
            except Exception:
                logger.exception("Failed to schedule auto-restore")

    def set_active_identifier(self, value):
        self.active_identifier = value
        label = self.id_to_label.get(value, "Unknown")
        slug = self.id_to_slug.get(value, "-")
        logger.info("Input selected: %s (%s, slug=%s)", value, label, slug)
        # Invoke handler
        on_input_changed(value, label, slug)

    # Remote key handling removed

    # Speaker-related methods removed: no TelevisionSpeaker service


def _detect_lan_ip(fallback: str = "127.0.0.1") -> str:
    """Detect primary LAN IPv4 by opening a UDP socket to a well-known address."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return fallback


def run(name: str, port: int, input_items: List[Tuple[int, str, str]], persist_dir: Path, address: str = "0.0.0.0", debug: bool = False, initial_identifier: Optional[int] = None):
    persist_dir.mkdir(parents=True, exist_ok=True)
    if debug:
        logging.getLogger("pyhap").setLevel(logging.DEBUG)
        logging.getLogger("HAP-python").setLevel(logging.DEBUG)
        logging.getLogger("zeroconf").setLevel(logging.INFO)
        logging.getLogger("asyncio").setLevel(logging.DEBUG)
        logger.setLevel(logging.DEBUG)
    driver = AccessoryDriver(
        port=port,
        persist_file=str(persist_dir / "switcher.state"),
        address=address,
        pincode=PAIRING_PIN_BYTES,
    )

    # Pass the driver into the accessory
    tv = TelevisionAccessory(driver, name=name, input_items=input_items, initial_identifier=initial_identifier)
    driver.add_accessory(tv)

    def signal_handler(sig, frame):
        driver.signal_handler(sig, frame)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    logger.info("Pairing code: %s", PAIRING_CODE)
    logger.info(
        "Starting HomeKit TV on port %s with inputs: %s",
        port,
        ", ".join(label for _, label, _ in input_items),
    )
    driver.start()


def parse_args(argv: Optional[List[str]] = None):
    parser = argparse.ArgumentParser(description="HomeKit Steam User Switcher")
    parser.add_argument("--name", default=DEFAULT_TV_NAME, help="Accessory name")
    parser.add_argument("--port", type=int, default=51826, help="TCP port for HAP server")
    parser.add_argument(
        "--bind",
        default="auto",
        help='Bind/advertise address: "auto" (default) detects your LAN IPv4, or provide an explicit IPv4',
    )
    # Keeping --inputs for compatibility, but by default inputs are Steam users.
    parser.add_argument(
        "--inputs",
        default="",
        help="Comma-separated list of input labels (overrides Steam users; slugs auto-generated)",
    )
    parser.add_argument("--persist", default=str(PERSIST_DIR), help="Persistence directory for pairing state")
    parser.add_argument("--debug", action="store_true", help="Enable verbose logging (pyhap, asyncio)")
    return parser.parse_args(argv)


if __name__ == "__main__":
    cli = parse_args()
    initial_identifier: Optional[int] = None
    if cli.inputs:
        # Override from CLI; enumerate starting at 1 and slugify labels
        labels = [s.strip() for s in cli.inputs.split(",") if s.strip()]
        resolved_items = [
            (idx, label, _slugify_label(label)) for idx, label in enumerate(labels, start=1)
        ]
    else:
        # Build inputs from Steam login users
        try:
            accounts = get_accounts()
            # Each entry is a dict with AccountName and PersonaName
            items: List[Tuple[int, str, str]] = []
            for idx, account in enumerate(accounts.values(), start=1):
                acc_name = account.get('AccountName')
                persona = account.get('PersonaName')
                if not acc_name:
                    # Skip invalid entries
                    continue
                label = persona or acc_name
                slug = acc_name  # we need AccountName to write AutoLoginUser
                items.append((idx, label, slug))
            if not items:
                raise RuntimeError("No Steam accounts found in loginusers.vdf")

            # Try to match current AutoLoginUser to set initial active input
            try:
                current = get_account()
                for i, _, slug in items:
                    if slug == current:
                        initial_identifier = i
                        break
            except Exception:
                pass

            resolved_items = items
        except Exception as e:
            logger.warning("Failed to read Steam users (%s); falling back to defaults", e)
            # Fallback to generic inputs
            resolved_items = [
                (1, "Steam User 1", "user1"),
                (2, "Steam User 2", "user2"),
            ]

    # Resolve bind/advertised address
    bind_addr = cli.bind
    if bind_addr in ("auto", "", "0.0.0.0"):
        bind_addr = _detect_lan_ip()
    if bind_addr.startswith("127."):
        logger.warning("Resolved bind address is loopback (%s); HomeKit will not reach it from your phone.", bind_addr)
    logger.info("Binding and advertising on %s:%s", bind_addr, cli.port)

    run(cli.name, cli.port, resolved_items, Path(cli.persist), address=bind_addr, debug=cli.debug, initial_identifier=initial_identifier)
