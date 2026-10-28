#!/usr/bin/env python3
"""Read MakeMKV robot output and print the feature title.

The ripper reads four lines from stdout: title id, disc name, disc id, duration.
A looping menu is often the longest title and has one chapter. The feature, or
the play-all on a TV disc, has more chapters. A name file overrides a useless
volume label. Same-label discs get a unique id so the next disc in a set is
not skipped.
"""

import hashlib
import json
import os
import re
import struct
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

NAMES_PATH = Path("/mnt/data/rip-work/state/disc-names")
IDENTIFIED_PATH = Path("/mnt/data/rip-work/state/identified-names")
CACHE_PATH = Path("/mnt/data/rip-work/state/title-cache")
QUALITY_PATH = Path("/mnt/data/rip-work/state/encode-quality")
PREVIOUS_ENCODE = Path("/mnt/data/rip-work/previous-encode")
JOBS = Path("/mnt/data/rip-work/jobs")
LOG_PATH = Path("/mnt/data/rip-work/logs/ripper.log")
QUALITY_TAG = {
    "dvd": "dvd-x264-rf16-slow",
    "bluray": "bd-x264-rf18-slow",
}
JUNK = {
    "ntsc", "pal", "ws", "scn", "ff", "usa", "r1", "r2", "dvd", "bd",
    "bluray", "blu", "ray", "disc", "d1", "d2",
}
# Extra words that do not make a different movie. "minecraft" is not one of these.
ALLOWED_EXTRA = {
    "part", "parts", "volume", "vol", "edition", "special", "collection",
    "chronicles", "series", "complete", "trilogy", "episode", "episodes",
    "first", "second", "third", "fourth", "fifth",
}


def field(line):
    match = re.match(r"^[A-Z]+:(?:\d+,)+\d+,(.*)$", line)
    if not match:
        return ""
    value = match.group(1).strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return value


def seconds(value):
    parts = value.split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(float(parts[2]))
    if len(parts) == 2:
        return int(parts[0]) * 60 + int(float(parts[1]))
    return int(float(value))


def device_path():
    try:
        for line in open("/etc/rip-bluray.conf", encoding="utf-8"):
            if line.startswith("DEVICE="):
                return line.split("=", 1)[1].strip() or "/dev/sr0"
    except OSError:
        pass
    return "/dev/sr0"


def disc_fingerprint(device):
    """Hash DVD IFO headers. Same volume label, different disc, different hash."""
    try:
        fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return ""
    try:
        def read_at(offset, size):
            os.lseek(fd, offset, os.SEEK_SET)
            data = os.read(fd, size)
            if len(data) != size:
                raise OSError("short read")
            return data

        pvd = read_at(16 * 2048, 2048)
        if pvd[1:6] != b"CD001":
            return ""
        extent = struct.unpack_from("<I", pvd, 158)[0]
        length = struct.unpack_from("<I", pvd, 166)[0]
        root = read_at(extent * 2048, ((length + 2047) // 2048) * 2048)[:length]
        video = find_entry(root, b"VIDEO_TS")
        if video is None:
            return ""
        vext, vlen = video
        vdata = read_at(vext * 2048, ((vlen + 2047) // 2048) * 2048)[:vlen]
        ifos = [
            (name, ext, size)
            for name, ext, size in walk(vdata)
            if name.upper().endswith(".IFO")
        ]
        if not ifos:
            return ""
        digest = hashlib.sha1()
        for name, ext, size in sorted(ifos):
            digest.update(name.encode())
            digest.update(read_at(ext * 2048, 2048)[: min(size, 2048)])
        return digest.hexdigest()[:12]
    except OSError:
        return ""
    finally:
        os.close(fd)


def walk(data):
    entries = []
    index = 0
    while index + 33 < len(data):
        rec_len = data[index]
        if rec_len == 0:
            index = (index + 2048) & ~2047
            continue
        name_len = data[index + 32]
        name = data[index + 33 : index + 33 + name_len].split(b";")[0].decode("ascii", "replace")
        if name not in {"\x00", "\x01", ".", ".."}:
            entries.append(
                (
                    name,
                    struct.unpack_from("<I", data, index + 2)[0],
                    struct.unpack_from("<I", data, index + 10)[0],
                )
            )
        index += rec_len
    return entries


def find_entry(data, wanted):
    for name, ext, size in walk(data):
        if name.upper() == wanted.decode():
            return ext, size
    return None


def load_rules():
    fingerprints = {}
    durations = {}
    series = {}
    try:
        lines = NAMES_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return fingerprints, durations, series
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        parts = key.split()
        if not value or not parts:
            continue
        if parts[0] == "fingerprint" and len(parts) == 2:
            fingerprints[parts[1].lower()] = value
        elif parts[0] == "duration" and len(parts) == 3:
            durations[(parts[1], parts[2])] = value
        elif parts[0] == "series" and len(parts) == 2:
            series[parts[1]] = value
    return fingerprints, durations, series


def existing_names():
    names = set()
    if not JOBS.is_dir():
        return names
    for directory in JOBS.iterdir():
        try:
            name = (directory / "disc-name").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if name:
            names.add(name)
    return names


def next_disc_name(series, used):
    number = 1
    while f"{series} - Disc {number}" in used:
        number += 1
    return f"{series} - Disc {number}"


def catalog_code(name):
    return bool(re.fullmatch(r"[A-Z]{0,3}\d{3,6}", name or ""))


def clean_name(name):
    safe = name.replace("\u2019", "'").replace("\u2018", "'")
    safe = safe.replace("\u00ae", "").replace("\u2122", "")
    safe = re.sub(r'[\\/:*?"<>|]+', " ", safe)
    safe = re.sub(r"\s+", " ", safe).strip(" .")
    return safe or "bluray"


def disc_kind(lines):
    for line in lines:
        if not line.startswith("CINFO:"):
            continue
        if "DVD disc" in line:
            return "dvd"
        if "Blu-ray disc" in line:
            return "bluray"
    return "bluray"


def dest_root():
    try:
        for line in open("/etc/rip-bluray.conf", encoding="utf-8"):
            if line.startswith("DEST_ROOT="):
                return Path(line.split("=", 1)[1].strip() or "/mnt/data/videos/Movies")
    except OSError:
        pass
    return Path("/mnt/data/videos/Movies")


def recorded_quality(basename):
    try:
        lines = QUALITY_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in lines:
        name, sep, tag = line.partition(" = ")
        if sep and name == basename:
            return tag.strip()
    return ""


def output_in_progress(basename):
    if not JOBS.is_dir():
        return False
    for directory in JOBS.iterdir():
        try:
            status = (directory / "status").read_text(encoding="utf-8").strip()
            output = (directory / "output").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if status in {"ripping", "ripped", "encoding"} and Path(output).name == basename:
            return True
    return False


def retire_old_encode(chosen, kind):
    """Move an older encode aside so the ripper does not treat it as done."""
    dest = dest_root() / f"{chosen}.mp4"
    if not dest.is_file() or dest.stat().st_size == 0:
        return
    expected = QUALITY_TAG[kind]
    current = recorded_quality(dest.name)
    if current == expected:
        return
    if output_in_progress(dest.name):
        note(f"leaving {dest.name} in place; a job is already using it")
        return
    PREVIOUS_ENCODE.mkdir(parents=True, exist_ok=True)
    backup = PREVIOUS_ENCODE / dest.name
    if backup.exists():
        backup = PREVIOUS_ENCODE / f"{dest.stem}-{int(dest.stat().st_mtime)}{dest.suffix}"
    dest.rename(backup)
    note(f"old encode {dest.name} was {current or 'the previous preset'}; ripping again as {expected}")


def note(message):
    line = f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S+00:00')} [choose-title] {message}\n"
    try:
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        print(message, file=sys.stderr)


def title_rows(titles, audio_counts):
    rows = []
    for title_id, attrs in titles.items():
        raw = attrs.get(9, "")
        if not raw:
            continue
        try:
            sec = seconds(raw)
            chapters = int(attrs.get(8) or 0)
            size = int(attrs.get(11) or 0)
        except ValueError:
            continue
        source = (attrs.get(16) or "").lower()
        rows.append({
            "id": title_id,
            "sec": sec,
            "chapters": chapters,
            "size": size,
            "audio": audio_counts.get(title_id, 0),
            "source": source,
            "duration": raw,
        })
    return rows


NAV_ERROR = re.compile(
    r"Title #(\d+) \(([^)]+)\) was skipped due to navigation error"
)


def navigation_titles(lines):
    """MakeMKV hides some real movies. The duration is still in the message."""
    found = []
    seen = set()
    for line in lines:
        text = field(line) or line
        match = NAV_ERROR.search(text)
        if not match:
            continue
        title_id = int(match.group(1))
        if title_id in seen:
            continue
        seen.add(title_id)
        duration = match.group(2)
        found.append({
            "id": title_id,
            "sec": seconds(duration),
            "chapters": 1,
            "size": 0,
            "audio": 1,
            "source": "navigation-error",
            "duration": duration,
        })
    return found


def choose_feature(titles, audio_counts, lines=()):
    """Prefer a feature-length title over a short extra.

    A looping menu is often the longest title and has one or two chapters.
    A short extra can have more chapters than the movie. Length decides that.
    A movie MakeMKV skipped for a navigation error is still a candidate.
    """
    rows = title_rows(titles, audio_counts)
    by_id = {item["id"]: item for item in rows}
    for item in navigation_titles(lines):
        if item["sec"] < 45 * 60:
            continue
        current = by_id.get(item["id"])
        if current is None:
            rows.append(item)
            by_id[item["id"]] = item
        elif item["sec"] > current["sec"]:
            current.update(item)
    if not rows:
        return None
    features = [item for item in rows if item["sec"] >= 45 * 60]
    if features:
        chaptered = [
            item for item in features
            if item["chapters"] >= 4 and item["source"] != "navigation-error"
        ]
        pool = features
        if chaptered:
            longest_program = max(item["sec"] for item in chaptered)
            pool = [
                item for item in features
                if item["source"] == "navigation-error"
                or not (
                    item["chapters"] <= 3
                    and item["sec"] > 1.5 * longest_program
                )
            ] or features
        return max(
            pool,
            key=lambda item: (
                item["chapters"],
                item["audio"],
                1 if str(item["source"]).endswith(".mpls") else 0,
                item["size"],
                item["sec"],
            ),
        )
    longest = max(rows, key=lambda item: (item["sec"], item["size"]))
    real = [
        item for item in rows
        if item["sec"] >= 10 * 60 and item["chapters"] >= 2
    ]
    pool = real or [item for item in rows if item["sec"] >= 10 * 60] or rows
    # A few-chapter title that is much longer than a chaptered program is a loop.
    chaptered = [item for item in pool if item["chapters"] >= 4 and item["sec"] >= 40 * 60]
    if chaptered:
        pool = [
            item for item in pool
            if not (
                item["chapters"] <= 3
                and item["sec"] > 1.5 * max(other["sec"] for other in chaptered)
            )
        ] or pool
    best = max(
        pool,
        key=lambda item: (
            item["chapters"],
            item["audio"],
            1 if item["source"].endswith(".mpls") else 0,
            item["size"],
            item["sec"],
        ),
    )
    if best["id"] != longest["id"]:
        note(
            f"skipped longer title {longest['id']} "
            f"({longest['duration']}, {longest['chapters']} chapters) "
            f"for title {best['id']} ({best['duration']}, {best['chapters']} chapters)"
        )
    return best


def name_tokens(value):
    text = value.lower().replace("_", " ").replace("-", " ")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    tokens = []
    for token in text.split():
        if token in JUNK or token in {"the", "a", "an", "of", "and"}:
            continue
        if token.isdigit() and len(token) != 4 and int(token) > 20:
            continue
        tokens.append(token)
    return tokens


def needs_lookup(name):
    if not name or catalog_code(name):
        return False
    if re.search(r"[a-z]", name) and " " in name:
        return False
    tokens = name_tokens(name)
    if not tokens:
        return False
    if len(tokens) == 1 and len(tokens[0]) < 5:
        return False
    return True


def load_cache():
    found = {}
    try:
        lines = CACHE_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return found
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        found[key.strip()] = value.strip()
    return found


def save_cache(cache):
    lines = [f"{key} = {cache[key]}" for key in sorted(cache)]
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def identified_title(name):
    """A title learned from dialogue. Unique labels only; shared codes are not here."""
    if not name:
        return ""
    try:
        lines = IDENTIFIED_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        key, sep, value = line.partition(" = ")
        if sep and key.strip() == name and value.strip():
            return value.strip()
    return ""


def lookup_title(name):
    """Turn a volume label into a movie title. A miss must not fail the rip."""
    if not needs_lookup(name):
        return ""
    cache = load_cache()
    if name in cache:
        return "" if cache[name] == "-" else cache[name]
    query = " ".join(name_tokens(name))
    if not query:
        return ""
    url = (
        "https://v2.sg.media-imdb.com/suggestion/"
        + urllib.parse.quote(query[0])
        + "/"
        + urllib.parse.quote(query)
        + ".json"
    )
    try:
        request = urllib.request.Request(url, headers={
            "User-Agent": "personal-ripper/1.0",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except (OSError, ValueError, json.JSONDecodeError):
        note(f"title lookup failed for {name}")
        return ""
    wanted = set(name_tokens(name))
    best = None
    best_score = -1
    for item in payload.get("d") or []:
        if item.get("q") not in {"feature", "movie", "tvSeries", "tvMovie", "tvSpecial", "video"}:
            continue
        title = item.get("l") or ""
        got = set(name_tokens(title))
        if not wanted or not wanted <= got:
            continue
        extra = got - wanted
        if any(token not in ALLOWED_EXTRA and not token.isdigit() for token in extra):
            continue
        # One-word labels match too many obscure films. Keep those unless the
        # hit is a well-known title and adds no extra words.
        if len(wanted) == 1 and (extra or not item.get("rank") or item["rank"] > 10000):
            continue
        score = 100 - len(extra) * 15
        if score > best_score:
            best_score = score
            best = title
    if not best or best_score < 85:
        note(f"no confident title for {name}")
        return ""
    pretty = clean_name(best)
    cache[name] = pretty
    save_cache(cache)
    note(f"named {name} as {pretty}")
    return pretty


def main():
    lines = open(sys.argv[1], encoding="utf-8", errors="replace").read().splitlines()
    Path("/mnt/data/rip-work/state/last-info.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    hidden = navigation_titles(lines)
    if hidden:
        note("scan hid " + ", ".join(f"title {item["id"]} ({item["duration"]})" for item in hidden))
    if any("Failed to open disc" in line for line in lines):
        raise SystemExit("failed to open disc")
    disc = ""
    disc_id = ""
    titles = {}
    audio_counts = {}
    for line in lines:
        if line.startswith("CINFO:"):
            match = re.match(r"CINFO:(\d+),", line)
            if not match:
                continue
            code = match.group(1)
            if code in {"2", "30"} and not disc:
                disc = field(line)
            elif code == "32" and not disc_id:
                disc_id = field(line)
        elif line.startswith("TINFO:"):
            match = re.match(r"TINFO:(\d+),(\d+),", line)
            if not match:
                continue
            titles.setdefault(int(match.group(1)), {})[int(match.group(2))] = field(line)
        elif line.startswith("SINFO:"):
            match = re.match(r"SINFO:(\d+),\d+,1,\d+,(.*)$", line)
            if match and "Audio" in match.group(2):
                audio_counts[int(match.group(1))] = audio_counts.get(int(match.group(1)), 0) + 1
    if not disc:
        raise SystemExit("no disc name")
    chosen_title = choose_feature(titles, audio_counts, lines)
    if chosen_title is None:
        raise SystemExit("no titles")
    best = chosen_title["id"]
    note(
        f"chose title {best} ({chosen_title["duration"]}, "
        f"{chosen_title["chapters"]} chapters)"
    )

    raw_name = clean_name(disc)
    label = disc_id or raw_name
    duration = chosen_title["duration"]
    fingerprint = disc_fingerprint(device_path())
    by_fingerprint, by_duration, by_series = load_rules()

    chosen = ""
    if fingerprint and fingerprint.lower() in by_fingerprint:
        chosen = by_fingerprint[fingerprint.lower()]
    elif (label, duration) in by_duration:
        chosen = by_duration[(label, duration)]
    elif label in by_series and (catalog_code(raw_name) or raw_name == label):
        used = existing_names()
        used.update(by_fingerprint.values())
        used.update(by_duration.values())
        chosen = next_disc_name(by_series[label], used)
    if not chosen:
        chosen = identified_title(raw_name) or identified_title(label)
    if not chosen:
        chosen = lookup_title(raw_name) or raw_name
    chosen = clean_name(chosen)

    # A shared catalog label must not be the hold id, or the next disc in the
    # set is skipped. A unique label can keep its own id so the cheap skip works.
    shared = catalog_code(label) or label in by_series
    if shared and chosen != raw_name and (fingerprint or duration):
        identity = f"{label}-{fingerprint or duration}"
    else:
        identity = label

    retire_old_encode(chosen, disc_kind(lines))

    print(best)
    print(chosen)
    print(identity)
    print(duration)


if __name__ == "__main__":
    main()
