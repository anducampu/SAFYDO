"""RDS / RBDS group decoder (IEC 62106, NRSC-4-B).

radio_cli hands over one raw group per `-r` call. Everything here is decoded from
those four 16-bit blocks, so it works for whatever a station happens to send.
Nothing is filled in by guesswork: a field stays empty until its data has arrived.
"""

from __future__ import annotations

import threading
from datetime import date, timedelta

PTY_RDS = (
    "None", "News", "Current Affairs", "Information", "Sport", "Education", "Drama", "Culture",
    "Science", "Varied", "Pop Music", "Rock Music", "Easy Listening", "Light Classical",
    "Serious Classical", "Other Music", "Weather", "Finance", "Children's Programmes",
    "Social Affairs", "Religion", "Phone-In", "Travel", "Leisure", "Jazz Music", "Country Music",
    "National Music", "Oldies Music", "Folk Music", "Documentary", "Alarm Test", "Alarm",
)
PTY_RBDS = (
    "None", "News", "Information", "Sports", "Talk", "Rock", "Classic Rock", "Adult Hits",
    "Soft Rock", "Top 40", "Country", "Oldies", "Soft", "Nostalgia", "Jazz", "Classical",
    "R&B", "Soft R&B", "Foreign Language", "Religious Music", "Religious Talk", "Personality",
    "Public", "College", "Spanish Talk", "Spanish Music", "Hip Hop", "Unassigned", "Unassigned",
    "Weather", "Emergency Test", "Emergency",
)
COVERAGE = ("Local", "International", "National", "Supra-regional") + tuple(
    f"Regional {n}" for n in range(1, 13)
)
ODA_NAMES = {
    0x4BD7: "RadioText+",
    0xCD46: "TMC traffic",
    0xCD47: "TMC traffic",
    0x4BD8: "RadioText+ (eRT)",
    0x6552: "Enhanced RadioText",
    0xC3B0: "iTunes tagging",
    0x0D45: "TMC (alert-C test)",
    0x7373: "Enhanced early warning",
    0xE911: "EAS open protocol",
}
_RT_PLUS_NAMES = (
    "", "title", "album", "track", "artist", "composition", "movement", "conductor", "composer",
    "band", "comment", "genre", "news", "local_news", "stock_market", "sport", "lottery",
    "horoscope", "daily_diversion", "health", "event", "scene", "cinema", "tv", "date_time",
    "weather", "traffic", "alarm", "advertisement", "url", "other", "station_short",
    "station_long", "programme_now", "programme_next", "programme_part", "host",
    "editorial_staff", "programme_frequency", "homepage", "subchannel", "hotline_phone",
    "studio_phone", "phone", "studio_sms", "sms", "hotline_email", "studio_email", "email", "mms",
    "chat", "chat_centre", "vote_question", "vote_centre", "", "", "", "", "", "place",
    "appointment", "identifier", "purchase", "get_data",
)
RT_PLUS_TAGS = {code: name for code, name in enumerate(_RT_PLUS_NAMES) if name}

# The RDS character set (IEC 62106 annex E) is ASCII only in places.
_HIGH = (
    "áàéèíìóòúùÑÇŞß¡Ĳ"
    "âäêëîïôöûüñçşğıĳ"
    "ªα©‰Ğěňőπ€£$←↑→↓"
    "º¹²³±İńűµ¿÷°¼½¾§"
    "ÁÀÉÈÍÌÓÒÚÙŘČŠŽÐĿ"
    "ÂÄÊËÎÏÔÖÛÜřčšžđŀ"
    "ÃÅÆŒŷÝÕØÞŊŔĆŚŹŦð"
    "ãåæœŵýõøþŋŕćśźŧ "
)
_LOW = {0x24: "¤", 0x5E: "―", 0x60: "║", 0x7E: "¯"}


def rds_char(code: int) -> str:
    if code >= 0x80:
        return _HIGH[code - 0x80]
    if code < 0x20 or code == 0x7F:
        return " "
    return _LOW.get(code, chr(code))


def af_code_khz(code: int) -> int | None:
    """VHF alternative-frequency code to kHz; None for anything else."""
    return 87500 + code * 100 if 1 <= code <= 204 else None


def lf_mf_code_khz(code: int) -> int | None:
    """The code following 250 in an AF list: a long or medium wave frequency."""
    if 1 <= code <= 15:
        return 153 + (code - 1) * 9
    if 16 <= code <= 135:
        return 531 + (code - 16) * 9
    return None


def rbds_callsign(pi: int) -> str:
    """North American call letters encoded in the PI code, where it follows the scheme."""
    if 0x1000 <= pi <= 0x54A7:
        first, n = "K", pi - 0x1000
    elif 0x54A8 <= pi <= 0x994F:
        first, n = "W", pi - 0x54A8
    else:
        return ""
    return first + chr(65 + n // 676) + chr(65 + n // 26 % 26) + chr(65 + n % 26)


class RdsDecoder:
    def __init__(self, region: str = "E") -> None:
        self.region = region
        # Fed by the RDS thread and read by the status thread: EON entries appearing
        # during a read would otherwise raise, and take the status broadcast with it.
        self._lock = threading.RLock()
        self.reset()

    def reset(self) -> None:
        """A new frequency is a new station: nothing carries over."""
        with self._lock:
            self._clear()

    def _clear(self) -> None:
        self.pi: int | None = None
        self.pty: int | None = None
        self.tp: bool | None = None
        self.ta: bool | None = None
        self.music: bool | None = None
        self.di: dict[str, bool] = {}
        self._di_seen = 0
        self._ps = [" "] * 8
        self._ps_seen = 0
        self.ps = ""
        self._rt = [" "] * 64
        self._rt_flag: int | None = None
        self._rt_end: int | None = None
        self._rt_seen: set[int] = set()
        self.rt = ""
        self.af: list[int] = []
        self.af_lf_mf: list[int] = []
        self.af_announced = 0
        self._af_next_is_lf_mf = False
        self.ecc: int | None = None
        self.language: int | None = None
        self.pin = ""
        self._ptyn = [" "] * 8
        self._ptyn_seen = 0
        self._ptyn_flag: int | None = None
        self.ptyn = ""
        self.clock = ""
        self.oda: dict[str, int] = {}
        self.rt_plus: dict[str, str] = {}
        self.eon: dict[int, dict] = {}
        self.groups: dict[str, int] = {}
        self.total_groups = 0

    # --- input -------------------------------------------------------------

    def feed(self, a: int, b: int, c: int, d: int, errors: tuple[int, int, int, int] = (0, 0, 0, 0)) -> bool:
        """One group. Returns False when it was rejected.

        The chip grades each block 0-3 and corrects up to the middle grades. Only
        clean or lightly corrected blocks count: at 11 groups a second there's no
        need to believe a damaged one, and a wrong letter sticks until overwritten.
        """
        if not (a or b or c or d) or max(errors) > 1:
            return False
        with self._lock:
            return self._feed(a, b, c, d, errors)

    def _feed(self, a: int, b: int, c: int, d: int, errors: tuple[int, int, int, int]) -> bool:
        if self.pi is not None and a != self.pi and errors[0]:
            return False
        self.pi = a
        kind = b >> 12
        version_b = bool(b & 0x0800)
        name = f"{kind}{'B' if version_b else 'A'}"
        self.groups[name] = self.groups.get(name, 0) + 1
        self.total_groups += 1
        self.tp = bool(b & 0x0400)
        self.pty = (b >> 5) & 0x1F

        if kind == 0:
            self._group0(b, c, d, version_b)
        elif kind == 1 and not version_b:
            self._group1a(c, d)
        elif kind == 2:
            self._group2(b, c, d, version_b)
        elif kind == 3 and not version_b:
            self._group3a(b, c, d)
        elif kind == 4 and not version_b:
            self._group4a(b, c, d)
        elif kind == 10 and not version_b:
            self._group10a(b, c, d)
        elif kind == 14 and not version_b:
            self._group14a(b, c, d)
        if name in self.oda and self.oda[name] == 0x4BD7:
            self._rt_plus(b, c, d)
        return True

    # --- groups ------------------------------------------------------------

    def _group0(self, b: int, c: int, d: int, version_b: bool) -> None:
        self.ta = bool(b & 0x10)
        self.music = bool(b & 0x08)
        segment = b & 3
        # DI is sent MSB first: segment 0 carries d3 (dynamic PTY).
        names = ("dynamic_pty", "compressed", "artificial_head", "stereo")
        self.di[names[segment]] = bool(b & 0x04)
        self._ps[segment * 2] = rds_char(d >> 8)
        self._ps[segment * 2 + 1] = rds_char(d & 0xFF)
        self._ps_seen |= 1 << segment
        if self._ps_seen == 0b1111:
            self.ps = "".join(self._ps).strip()
        if not version_b:
            self._af_codes(c >> 8, c & 0xFF)

    def _af_codes(self, hi: int, lo: int) -> None:
        for code in (hi, lo):
            if self._af_next_is_lf_mf:
                self._af_next_is_lf_mf = False
                khz = lf_mf_code_khz(code)
                if khz and khz not in self.af_lf_mf:
                    self.af_lf_mf.append(khz)
                continue
            if 224 <= code <= 249:
                self.af_announced = code - 224
            elif code == 250:
                self._af_next_is_lf_mf = True
            else:
                khz = af_code_khz(code)
                if khz and khz not in self.af:
                    self.af.append(khz)
                    self.af.sort()

    def _group1a(self, c: int, d: int) -> None:
        variant = (c >> 12) & 7
        if variant == 0:
            self.ecc = c & 0xFF
        elif variant == 3:
            self.language = c & 0xFFF
        day, hour, minute = (d >> 11) & 0x1F, (d >> 6) & 0x1F, d & 0x3F
        if day and hour < 24 and minute < 60:
            self.pin = f"day {day}, {hour:02d}:{minute:02d}"

    def _group2(self, b: int, c: int, d: int, version_b: bool) -> None:
        flag = (b >> 4) & 1
        if self._rt_flag is not None and flag != self._rt_flag:
            # The A/B flag flips when the station sends a new text.
            self._rt = [" "] * 64
            self._rt_seen = set()
            self._rt_end = None
        self._rt_flag = flag
        segment = b & 0xF
        if version_b:
            chars, at = [d >> 8, d & 0xFF], segment * 2
        else:
            chars, at = [c >> 8, c & 0xFF, d >> 8, d & 0xFF], segment * 4
        for i, code in enumerate(chars):
            if code == 0x0D:
                self._rt_end = at + i
                break
            self._rt[at + i] = rds_char(code)
        self._rt_seen.add(segment)
        end = self._rt_end if self._rt_end is not None else (32 if version_b else 64)
        size = 2 if version_b else 4
        needed = {n for n in range(0, (end + size - 1) // size)}
        if needed <= self._rt_seen:
            self.rt = " ".join("".join(self._rt[:end]).split())

    def _group3a(self, b: int, c: int, d: int) -> None:
        app = b & 0x1F
        if d:
            self.oda[f"{app >> 1}{'B' if app & 1 else 'A'}"] = d

    def _group4a(self, b: int, c: int, d: int) -> None:
        mjd = ((b & 3) << 15) | (c >> 1)
        hour = ((c & 1) << 4) | (d >> 12)
        minute = (d >> 6) & 0x3F
        if mjd < 50000 or hour > 23 or minute > 59:
            return
        half_hours = d & 0x1F
        offset = -half_hours if d & 0x20 else half_hours
        sign = "-" if offset < 0 else "+"
        when = date(1858, 11, 17) + timedelta(days=mjd)
        self.clock = (
            f"{when.isoformat()} {hour:02d}:{minute:02d} UTC"
            f" ({sign}{abs(offset) // 2}:{abs(offset) % 2 * 30:02d})"
        )

    def _group10a(self, b: int, c: int, d: int) -> None:
        flag = (b >> 4) & 1
        if self._ptyn_flag is not None and flag != self._ptyn_flag:
            self._ptyn = [" "] * 8
            self._ptyn_seen = 0
        self._ptyn_flag = flag
        at = (b & 1) * 4
        for i, code in enumerate((c >> 8, c & 0xFF, d >> 8, d & 0xFF)):
            self._ptyn[at + i] = rds_char(code)
        self._ptyn_seen |= 1 << (b & 1)
        if self._ptyn_seen == 0b11:
            self.ptyn = "".join(self._ptyn).strip()

    def _group14a(self, b: int, c: int, d: int) -> None:
        """EON: what another station on the same network is doing."""
        if not d:  # stations with no network to describe send PI 0000
            return
        other = self.eon.setdefault(d, {"pi": d, "ps": [" "] * 8, "af": [], "pty": None, "ta": None})
        variant = b & 0xF
        if variant <= 3:
            other["ps"][variant * 2] = rds_char(c >> 8)
            other["ps"][variant * 2 + 1] = rds_char(c & 0xFF)
        elif variant == 4:
            for code in (c >> 8, c & 0xFF):
                khz = af_code_khz(code)
                if khz and khz not in other["af"]:
                    other["af"].append(khz)
                    other["af"].sort()
        elif variant == 13:
            other["pty"] = (c >> 11) & 0x1F
            other["ta"] = bool(c & 1)

    def _rt_plus(self, b: int, c: int, d: int) -> None:
        if not b & 0x08:  # item running
            return
        type1 = ((b & 7) << 3) | (c >> 13)
        start1, len1 = (c >> 7) & 0x3F, (c >> 1) & 0x3F
        type2 = ((c & 1) << 5) | (d >> 11)
        start2, len2 = (d >> 5) & 0x3F, d & 0x1F
        for tag, start, length in ((type1, start1, len1), (type2, start2, len2)):
            name = RT_PLUS_TAGS.get(tag)
            if not name or start + length + 1 > 64:
                continue
            text = "".join(self._rt[start : start + length + 1]).strip()
            if text:
                self.rt_plus[name] = text

    # --- output ------------------------------------------------------------

    def pty_name(self, code: int | None) -> str:
        if code is None:
            return ""
        return (PTY_RBDS if self.region == "U" else PTY_RDS)[code]

    def snapshot(self) -> dict:
        with self._lock:
            return self._snapshot()

    def _snapshot(self) -> dict:
        pi = self.pi
        return {
            "pi": f"{pi:04X}" if pi is not None else "",
            "country_code": f"{pi >> 12:X}" if pi is not None else "",
            "coverage": COVERAGE[(pi >> 8) & 0xF] if pi is not None else "",
            "programme_ref": (pi & 0xFF) if pi is not None else None,
            "callsign": rbds_callsign(pi) if pi is not None and self.region == "U" else "",
            "ps": self.ps,
            "rt": self.rt,
            "pty": self.pty,
            "pty_name": self.pty_name(self.pty),
            "ptyn": self.ptyn,
            "tp": self.tp,
            "ta": self.ta,
            "music": self.music,
            "di": dict(self.di),
            "af": list(self.af),
            "af_lf_mf": list(self.af_lf_mf),
            "af_announced": self.af_announced,
            "ecc": f"{self.ecc:02X}" if self.ecc is not None else "",
            "language": f"{self.language:03X}" if self.language is not None else "",
            "pin": self.pin,
            "clock": self.clock,
            "oda": [
                {"group": g, "aid": f"{aid:04X}", "name": ODA_NAMES.get(aid, "")}
                for g, aid in sorted(self.oda.items())
            ],
            "rt_plus": dict(self.rt_plus),
            "eon": [
                {
                    "pi": f"{o['pi']:04X}",
                    "ps": "".join(o["ps"]).strip(),
                    "af": list(o["af"]),
                    "pty_name": self.pty_name(o["pty"]),
                    "ta": o["ta"],
                }
                for o in self.eon.values()
            ],
            "groups": dict(sorted(self.groups.items())),
            "total_groups": self.total_groups,
        }
