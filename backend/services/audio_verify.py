"""Audio pipeline verification — BT + mpv + optional test tone."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from backend.services.mpv import MpvClient, MpvError
from backend.services.bluez import device_connected, device_prop

BT_STATUS_FILE = Path("/var/lib/audio-player/bt_status.json")
TEST_WAV = Path("/usr/share/sounds/alsa/Front_Center.wav")
MPV_UNIT = "audio-player-mpv.service"
# mpv needs the BlueALSA sink open before it creates the IPC socket (~8 s on a
# Pi 3 A+); the old fixed 3-4 s sleeps gave up while it was still starting.
MPV_START_TIMEOUT = 15.0


def restart_mpv_service(mpv: MpvClient, timeout: float = MPV_START_TIMEOUT) -> bool:
    """Restart mpv and wait for its IPC socket. True once it answers.

    Callers must ensure Bluetooth is connected first — mpv_daemon.sh exits
    outright when no sink is up, so restarting without a link just burns the
    timeout.

    The restart BLOCKS deliberately. --no-block is right for the shell helpers,
    which run inside audio-player-bt-autoconnect.service and would deadlock on
    a job ordered after themselves; the backend has no such ordering, and there
    it only created a race where the still-running old mpv answered the first
    poll and the caller concluded the restart was done.
    """
    try:
        subprocess.run(
            ["systemctl", "restart", MPV_UNIT],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return mpv.wait_available(timeout)


def bt_connected(mac: str) -> bool:
    if not mac:
        return False
    return bool(device_connected(mac))


def _wait_for_playback(mpv: MpvClient, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not bool(mpv.get_property_safe("idle-active", True)) and not bool(
            mpv.get_property_safe("pause", True)
        ):
            return True
        time.sleep(0.2)
    return False


def _bt_name(mac: str) -> str:
    return device_prop(mac, "Name") or ""


def verify_audio(
    mpv: MpvClient,
    cfg_get,
    install_dir: Path,
    *,
    require_playback: bool = False,
    playback_timeout: float = 5.0,
    test_tone: bool = False,
) -> dict:
    failures: list[str] = []
    warnings: list[str] = []

    mac = str(cfg_get("bluetooth.primary_mac") or "").strip()
    bt_is_connected = bt_connected(mac)
    bt_name = _bt_name(mac) if bt_is_connected else ""

    codec = ""
    if BT_STATUS_FILE.exists():
        try:
            codec = json.loads(BT_STATUS_FILE.read_text()).get("codec") or ""
        except json.JSONDecodeError:
            pass

    if not mac:
        failures.append("primary_mac_empty")
    elif not bt_is_connected:
        failures.append("bluetooth_not_connected")

    service_active = subprocess.run(
        ["systemctl", "is-active", "audio-player-mpv"],
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    ).stdout.strip() == "active"
    if not service_active:
        failures.append("mpv_service_inactive")

    socket_ok = mpv.available()
    if not socket_ok:
        failures.append("mpv_socket_missing")

    audio_device = ""
    idle = True
    paused = True
    if socket_ok:
        audio_device = str(mpv.get_property_safe("audio-device") or "")
        idle = bool(mpv.get_property_safe("idle-active", True))
        paused = bool(mpv.get_property_safe("pause", True))
        if mac and audio_device and mac.upper() not in audio_device.upper():
            failures.append("audio_device_mismatch")

    if require_playback and not _wait_for_playback(mpv, timeout=playback_timeout):
        failures.append("playback_not_active")
        # Re-read idle so callers can tell "still opening the stream"
        # (idle=False) from "mpv gave up and fell back to idle" (idle=True).
        if socket_ok:
            idle = bool(mpv.get_property_safe("idle-active", True))

    test_tone_ok: bool | None = None
    if test_tone and not failures:
        test_tone_ok = False
        if TEST_WAV.is_file():
            prev_path = mpv.get_property_safe("path")
            prev_pos = mpv.get_property_safe("playback-time")
            was_paused = bool(mpv.get_property_safe("pause", True))
            try:
                mpv.load(str(TEST_WAV), "replace")
                mpv.pause(False)
                if _wait_for_playback(mpv, timeout=2.0):
                    test_tone_ok = True
                else:
                    failures.append("test_tone_no_playback")
            except MpvError:
                failures.append("test_tone_mpv_error")
            finally:
                if prev_path:
                    try:
                        mpv.load(prev_path, "replace")
                        if prev_pos:
                            mpv.command("seek", prev_pos, "absolute")
                        mpv.pause(was_paused)
                    except MpvError:
                        warnings.append("test_tone_restore_failed")
        else:
            warnings.append("test_wav_missing")

    ok = len(failures) == 0
    return {
        "ok": ok,
        "bluetooth": {
            "connected": bt_is_connected,
            "mac": mac,
            "name": bt_name,
            "codec": codec,
        },
        "mpv": {
            "service_active": service_active,
            "socket_ok": socket_ok,
            "audio_device": audio_device,
            "idle": idle,
            "paused": paused,
        },
        "failures": failures,
        "warnings": warnings,
        "test_tone_ok": test_tone_ok,
    }


def ensure_playback_ready(mpv: MpvClient, cfg_get, install_dir: Path) -> None:
    """Connect BT and align mpv before play — raises MpvError on failure."""
    mac = str(cfg_get("bluetooth.primary_mac") or "").strip()
    if not mac:
        raise MpvError("bluetooth.primary_mac not set — pair a speaker first")

    if not bt_connected(mac):
        script = install_dir / "scripts" / "bt_connect.sh"
        proc = subprocess.run([str(script), mac], capture_output=True, text=True, errors="replace", timeout=90, check=False)
        if not bt_connected(mac):
            raise MpvError(f"Bluetooth not connected ({mac}): {proc.stderr.strip() or 'connect failed'}")

    if not mpv.available() and not restart_mpv_service(mpv):
        raise MpvError("mpv IPC socket missing — restart audio-player-mpv.service failed")

    expected = f"DEV={mac}"
    audio_dev = str(mpv.get_property_safe("audio-device") or "")
    if expected.upper() not in audio_dev.upper():
        if not restart_mpv_service(mpv):
            raise MpvError("mpv did not restart after audio-device mismatch")
        # Re-read: a restart that comes back pointing at the wrong sink still
        # means playback would go to the wrong speaker.
        audio_dev = str(mpv.get_property_safe("audio-device") or "")
        if expected.upper() not in audio_dev.upper():
            raise MpvError(f"mpv audio-device mismatch: {audio_dev}")

    # Clear EOF pause state from finished local files
    if bool(mpv.get_property_safe("pause", False)) and bool(mpv.get_property_safe("idle-active", False)):
        mpv.stop()