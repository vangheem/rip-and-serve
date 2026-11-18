#!/usr/bin/env python3
"""Read MakeMKV robot output and print the feature title.

The ripper reads four lines from stdout: title id, disc name, disc id, duration.
A looping menu is often the longest title and has one chapter. The feature, or
the play-all on a TV disc, has more chapters. A name file overrides a useless
volume label. Same-label discs get a unique id so the next disc in a set is
not skipped.

An episode disc is different. The ripper then reads two lines: "episodes" and
a plan path. The plan lists each episode title, not the play-all.
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

def conf_root(key, default):
    """A path from /etc/rip-bluray.conf, or the built-in default."""
    try:
        for line in open("/etc/rip-bluray.conf", encoding="utf-8"):
            if line.startswith(key + "="):
                value = line.split("=", 1)[1].strip()
                if value:
                    return Path(value)
    except OSError:
        pass
    return Path(default)


META = conf_root("META_ROOT", "/var/lib/rip-bluray")
STATE = META / "state"
# The work disk keeps the big files: job scratch and moved-aside movies.
WORK = conf_root("WORK_ROOT", "/mnt/data/rip-work")
NAMES_PATH = STATE / "disc-names"
IDENTIFIED_PATH = STATE / "identified-names"
CACHE_PATH = STATE / "title-cache"
QUALITY_PATH = STATE / "encode-quality"
PREVIOUS_ENCODE = WORK / "previous-encode"
JOBS = WORK / "jobs"
LOG_PATH = META / "logs/ripper.log"
EPISODE_MAP = STATE / "episode-map.json"
PLAN_DIR = STATE / "plans"
IMDB_INDEX = STATE / "imdb/titles.json"
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
    labels = {}
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
        elif parts[0] == "label" and len(parts) == 2:
            labels[parts[1]] = value
    return fingerprints, durations, series, labels


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
    return conf_root("DEST_ROOT", "/mnt/data/videos/Movies")


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
        # A behind-the-scenes piece can have more chapters than the movie
        # and still be long enough to look like a feature. Quantum of Solace
        # offered a 45-minute extra with 34 chapters against the 1:46 film.
        longest_feature = max(features, key=lambda item: item["sec"])
        if longest_feature["sec"] >= 70 * 60 and longest_feature["chapters"] >= 8:
            shorter = [
                item for item in pool
                if item["sec"] < longest_feature["sec"] * 0.75
                and item["source"] != "navigation-error"
            ]
            if shorter and len(shorter) < len(pool):
                pool = [item for item in pool if item not in shorter]
        return max(
            pool,
            key=lambda item: (
                item["sec"],
                item["chapters"],
                item["audio"],
                1 if str(item["source"]).endswith(".mpls") else 0,
                item["size"],
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


GENERIC_LABELS = {
    "dvd video", "bluray", "blu ray", "bd rom", "video ts",
}


def identified_title(name):
    """A title learned from dialogue. Unique labels only; shared codes are not here."""
    folded = re.sub(r"[\s_-]+", " ", (name or "").lower()).strip()
    if not name or folded in GENERIC_LABELS:
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


def glued_title(name):
    """THEILLUSIONIST has no word breaks. Match the whole label to a catalog title."""
    words = name_tokens(name)
    if len(words) != 1 or len(words[0]) < 10:
        return ""
    needle = words[0]
    path = IMDB_INDEX
    try:
        index = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    hits = [hit for key, hit in index.items() if key.replace(" ", "") == needle]
    if not hits:
        return ""
    hits.sort(key=lambda item: item.get("votes") or 0, reverse=True)
    best = hits[0]
    votes = best.get("votes") or 0
    if votes < 1000:
        return ""
    if len(hits) > 1 and votes <= 4 * (hits[1].get("votes") or 0):
        return ""
    return best.get("title") or ""


DISC_LABEL = re.compile(r"^(?P<stem>.+?)\s+dis[ck]\s+(?P<number>\d+)$", re.I)
SERIES_KINDS = {"tvseries", "tvminiseries", "tv series", "tv mini-series"}


def expand_disc_set(name):
    """Pride Disc 1 is a truncated series title, not a finished movie name."""
    if re.search(r"\s-\s+disc\s+\d+$", name or "", re.I):
        return ""
    match = DISC_LABEL.match((name or "").strip())
    if not match:
        return ""
    stem = re.sub(r"[\s\-]+$", "", match.group("stem")).strip()
    number = int(match.group("number"))
    words = name_tokens(stem)
    if len(words) != 1 or len(words[0]) < 4:
        return ""
    query = words[0]
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
        note(f"series lookup failed for {name}")
        return ""
    best = None
    best_rank = 5001
    for item in payload.get("d") or []:
        kind = (item.get("q") or "").lower()
        if kind not in SERIES_KINDS:
            continue
        title = item.get("l") or ""
        got = name_tokens(title)
        if not got or got[0] != query:
            continue
        rank = item.get("rank") or 0
        if not rank or rank > 5000 or rank >= best_rank:
            continue
        best_rank = rank
        best = title
    if not best:
        return ""
    return f"{clean_name(best)} - Disc {number}"


def lookup_title(name):
    """Turn a volume label into a movie title. A miss must not fail the rip."""
    if not needs_lookup(name):
        return ""
    cache = load_cache()
    if name in cache:
        return "" if cache[name] == "-" else cache[name]
    glued = glued_title(name)
    if glued:
        pretty = clean_name(glued)
        cache[name] = pretty
        save_cache(cache)
        note(f"named {name} as {pretty}")
        return pretty
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


HALVES = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5}
SEASON_LABEL = re.compile(
    r"^(?P<show>.+?)\s+-\s+Season\s+(?P<season>\d+)"
    r"(?:\s+-\s+(?:Disc\s+(?P<disc>\d+)|(?P<half>First|Second|Third|Fourth|Fifth)\s+Half))?$",
    re.I,
)


def shows_root():
    try:
        for line in open("/etc/rip-bluray.conf", encoding="utf-8"):
            if line.startswith("SHOWS_ROOT="):
                value = line.split("=", 1)[1].strip()
                if value:
                    return Path(value)
    except OSError:
        pass
    return dest_root().parent / "TV Shows"


def parse_show_label(name):
    """A season label is a TV disc. 'Show - Disc 2' is a movie set, not one."""
    match = SEASON_LABEL.match((name or "").strip())
    if not match:
        return {"show": name, "season": 1, "part": 1, "season_label": False}
    half = (match.group("half") or "").lower()
    disc = match.group("disc")
    if half:
        part = HALVES[half]
    elif disc:
        part = int(disc)
    else:
        part = 1
    return {
        "show": match.group("show").strip(),
        "season": int(match.group("season")),
        "part": part,
        "season_label": True,
    }


def select_episodes(rows, season_label):
    """Episode titles to rip, or None when the disc is a movie.

    A season disc with a run of same-length titles is a TV disc. The play-all
    is the long title and is not ripped again. A movie set labeled 'Disc 2'
    does not come through here: that label is not a season label, and a single
    feature does not form a cluster.
    """
    shorts = [row for row in rows if 4 * 60 <= row["sec"] <= 45 * 60]
    if len(shorts) < 4:
        return None
    center = None
    best_count = 0
    for row in shorts:
        count = sum(1 for other in shorts if abs(other["sec"] - row["sec"]) <= 90)
        if count > best_count:
            best_count = count
            center = row["sec"]
    if center is None:
        return None
    cluster = [row for row in shorts if abs(row["sec"] - center) <= 90]
    if max(row["sec"] for row in cluster) - min(row["sec"] for row in cluster) > 120:
        return None
    median = sorted(row["sec"] for row in cluster)[len(cluster) // 2]
    long_titles = [row for row in rows if row["sec"] >= 50 * 60]
    # A season label is enough. Without one, the long title has to be the
    # episodes run together. A movie the same length as its deleted scenes is not.
    under_hour = [row for row in rows if 3 * 60 <= row["sec"] < 50 * 60]
    short_sum = sum(row["sec"] for row in under_hour)

    def is_playall(row):
        if median <= 0 or row["chapters"] < 6:
            return False
        if short_sum and abs(row["sec"] - short_sum) <= max(180, short_sum * 0.08):
            return True
        multiple = round(row["sec"] / median)
        if multiple < 6 or multiple > len(cluster):
            return False
        expected = multiple * median
        if abs(row["sec"] - expected) > max(120, expected * 0.08):
            return False
        return row["chapters"] >= multiple - 2

    playalls = [row for row in long_titles if is_playall(row)]
    if season_label and len(cluster) >= 4:
        pass
    elif playalls and len(cluster) >= 8:
        pass
    else:
        return None
    skip = {row["id"] for row in long_titles}
    chosen = []
    seen_size = set()
    for row in sorted(rows, key=lambda item: item["id"]):
        if row["id"] in skip or row["sec"] < 3 * 60 or row["sec"] >= 50 * 60:
            continue
        # Same length and the same number of bytes is the episode listed twice.
        size = row.get("size") or 0
        if size:
            key = (row["sec"], size)
            if key in seen_size:
                continue
            seen_size.add(key)
        chosen.append(row)
    if len(chosen) < 4:
        return None
    return chosen


def series_folder(show):
    """Bluey (2018) matches the local IMDb list. A miss keeps the disc's name."""
    key = re.sub(r"[^a-z0-9]+", " ", show.lower()).strip()
    path = IMDB_INDEX
    try:
        index = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return show, None
    hit = index.get(key) or {}
    if hit.get("type") not in {"tvSeries", "tvMiniSeries"}:
        return show, None
    year = hit.get("year") or None
    title = clean_name(hit.get("title") or show)
    if year:
        return f"{title} ({int(year)})", int(year)
    return title, None


def load_episode_map():
    try:
        return json.loads(EPISODE_MAP.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_episode_map(mapping):
    EPISODE_MAP.parent.mkdir(parents=True, exist_ok=True)
    temporary = EPISODE_MAP.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(mapping, indent=2) + "\n", encoding="utf-8")
    temporary.replace(EPISODE_MAP)


def episode_key(show, season, part):
    return f"{show}\t{season}\t{part}"


def assign_start(mapping, show, season, part, count, disc_id, folder):
    """Number this disc's episodes. A later disc waits for the earlier one."""
    key = episode_key(show, season, part)
    current = mapping.get(key)
    if (
        current
        and current.get("disc_id") == disc_id
        and current.get("count") == count
        and current.get("folder") == folder
    ):
        return current["start"], bool(current.get("provisional"))
    start = 1
    provisional = False
    if part > 1:
        covered = 0
        for previous in range(1, part):
            earlier = mapping.get(episode_key(show, season, previous))
            if not earlier or earlier.get("provisional"):
                provisional = True
                break
            covered = earlier["start"] + earlier["count"] - 1
        start = part * 1000 + 1 if provisional else covered + 1
    mapping[key] = {
        "disc_id": disc_id,
        "show": show,
        "folder": folder,
        "season": season,
        "part": part,
        "start": start,
        "count": count,
        "provisional": provisional,
    }
    return start, provisional


def episode_path(folder, show, season, number):
    code = f"S{season:02d}E{number:02d}"
    filename = f"{show} - {code}.mp4"
    directory = shows_root() / folder / f"Season {season:02d}"
    return directory / filename, code


def settle_episode_map(mapping):
    """Give a later disc real episode numbers once the earlier disc is known."""
    grouped = {}
    for row in mapping.values():
        grouped.setdefault((row["show"], row["season"]), []).append(row)
    changes = []
    for rows in grouped.values():
        known = {row["part"]: row for row in rows if not row.get("provisional")}
        for row in sorted(rows, key=lambda item: item["part"]):
            if not row.get("provisional"):
                continue
            if not all(part in known for part in range(1, row["part"])):
                continue
            start = 1
            for part in range(1, row["part"]):
                earlier = known[part]
                start = earlier["start"] + earlier["count"]
            if start == row["start"]:
                row["provisional"] = False
                known[row["part"]] = row
                continue
            changes.append({
                "show": row["show"],
                "folder": row["folder"],
                "season": row["season"],
                "old_start": row["start"],
                "new_start": start,
                "count": row["count"],
            })
            row["start"] = start
            row["provisional"] = False
            known[row["part"]] = row
    return changes


def apply_episode_renames(changes):
    """Rename files the encoder already finished. Leave one it is writing."""
    if not changes:
        return 0
    quality = {}
    try:
        for line in QUALITY_PATH.read_text(encoding="utf-8").splitlines():
            name, sep, tag = line.partition(" = ")
            if sep and name:
                quality[name] = tag.strip()
    except OSError:
        pass
    renamed = 0
    for change in changes:
        for offset in range(change["count"]):
            old_path, old_code = episode_path(
                change["folder"], change["show"], change["season"], change["old_start"] + offset
            )
            new_path, new_code = episode_path(
                change["folder"], change["show"], change["season"], change["new_start"] + offset
            )
            if old_path == new_path:
                continue
            if output_in_progress(old_path.name) or output_in_progress(new_path.name):
                note(f"leaving {old_path.name}; a job is still using it")
                continue
            if old_path.is_file():
                if new_path.exists():
                    note(f"not renaming {old_path.name}; {new_path.name} already exists")
                    continue
                new_path.parent.mkdir(parents=True, exist_ok=True)
                old_path.rename(new_path)
                renamed += 1
                note(f"renamed {old_path.name} to {new_path.name}")
            if old_path.name in quality:
                quality[new_path.name] = quality.pop(old_path.name)
            rewrite_job_output(old_path, new_path, old_code, new_code)
    if quality:
        lines = [f"{name} = {quality[name]}" for name in sorted(quality)]
        try:
            QUALITY_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError:
            pass
    return renamed


def rewrite_job_output(old_path, new_path, old_code, new_code):
    if not JOBS.is_dir():
        return
    old_text = str(old_path)
    for directory in JOBS.iterdir():
        output_path = directory / "output"
        try:
            current = output_path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if current != old_text:
            continue
        output_path.write_text(str(new_path) + "\n", encoding="utf-8")
        name_path = directory / "disc-name"
        try:
            name = name_path.read_text(encoding="utf-8").strip()
        except OSError:
            name = ""
        if name.endswith(old_code):
            name_path.write_text(name[: -len(old_code)] + new_code + "\n", encoding="utf-8")


def settle_episodes():
    mapping = load_episode_map()
    changes = settle_episode_map(mapping)
    if not changes:
        return 0
    save_episode_map(mapping)
    renamed = apply_episode_renames(changes)
    note(f"settled episode numbers; renamed {renamed} files")
    return renamed


def move_aside(path):
    """Keep a replaced file. The movie library must not keep the play-all."""
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        return False
    if output_in_progress(path.name):
        note(f"leaving {path.name} in place; a job is already using it")
        return False
    PREVIOUS_ENCODE.mkdir(parents=True, exist_ok=True)
    backup = PREVIOUS_ENCODE / path.name
    if backup.exists():
        backup = PREVIOUS_ENCODE / f"{path.stem}-{int(path.stat().st_mtime)}{path.suffix}"
    path.rename(backup)
    note(f"moved {path.name} aside")
    return True


def build_episode_plan(rows, parsed, disc_name, disc_id, kind):
    folder, _year = series_folder(parsed["show"])
    show = re.sub(r"\s+\(\d{4}\)$", "", folder)
    mapping = load_episode_map()
    start, provisional = assign_start(
        mapping, show, parsed["season"], parsed["part"], len(rows), disc_id, folder
    )
    save_episode_map(mapping)
    settle_episodes()
    mapping = load_episode_map()
    saved = mapping.get(episode_key(show, parsed["season"], parsed["part"])) or {}
    start = saved.get("start", start)
    provisional = bool(saved.get("provisional", provisional))
    episodes = []
    for offset, row in enumerate(rows):
        path, code = episode_path(folder, show, parsed["season"], start + offset)
        episodes.append({
            "title_id": row["id"],
            "code": code,
            "name": f"{show} - {code}",
            "duration": row["duration"],
            "output": str(path),
        })
    plan = {
        "disc_name": disc_name,
        "disc_id": disc_id,
        "show": show,
        "folder": folder,
        "season": parsed["season"],
        "part": parsed["part"],
        "kind": kind,
        "provisional": provisional,
        "episodes": episodes,
    }
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", disc_id).strip("-") or "disc"
    PLAN_DIR.mkdir(parents=True, exist_ok=True)
    path = PLAN_DIR / f"{safe}.json"
    path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    note(
        f"episode disc {disc_name}: {len(episodes)} episodes"
        + (" (numbering waits for the earlier disc)" if provisional else "")
    )
    return path


def emit_pending(plan_path):
    plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    kind = plan.get("kind") or "dvd"
    expected = QUALITY_TAG.get(kind) or QUALITY_TAG["dvd"]
    for episode in plan["episodes"]:
        output = Path(episode["output"])
        if output.is_file() and output.stat().st_size == 0:
            output.unlink()
        if output.is_file() and output.stat().st_size > 0:
            current = recorded_quality(output.name)
            if current == expected or output_in_progress(output.name):
                continue
            if current != expected:
                move_aside(output)
        elif output_in_progress(output.name):
            continue
        print(
            f"{episode['title_id']}\t{episode['name']}\t{episode['output']}\t{episode['duration']}"
        )


def retire_playall(plan_path):
    """Move the old play-all aside only after every episode file exists."""
    plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    for episode in plan["episodes"]:
        output = Path(episode["output"])
        if not output.is_file() or output.stat().st_size == 0:
            return
    movie = dest_root() / f"{plan['disc_name']}.mp4"
    if movie.is_file():
        move_aside(movie)


def self_test():
    def row(title_id, seconds, chapters=2, size=0):
        hours, rest = divmod(seconds, 3600)
        minutes, secs = divmod(rest, 60)
        return {
            "id": title_id,
            "sec": seconds,
            "chapters": chapters,
            "size": size,
            "duration": f"{hours}:{minutes:02d}:{secs:02d}",
        }

    def bluey(play_seconds, play_chapters, shorts, specials=()):
        rows = [row(0, play_seconds, play_chapters)]
        title_id = 1
        for seconds in shorts:
            rows.append(row(title_id, seconds, 2, size=title_id * 1000))
            title_id += 1
        for seconds in specials:
            rows.append(row(title_id, seconds, 2, size=title_id * 1000))
            title_id += 1
        return rows

    parsed = parse_show_label("Bluey - Season 1 - First Half")
    assert parsed == {"show": "Bluey", "season": 1, "part": 1, "season_label": True}
    assert parse_show_label("Bluey - Season 2 - Second Half")["part"] == 2
    assert parse_show_label("Gettysburg (1993) - Disc 2")["season_label"] is False

    first = select_episodes(bluey(3 * 3600 + 2 * 60 + 42, 27, [7 * 60 + 2] * 25 + [6 * 60 + 52]), True)
    assert first is not None and len(first) == 26 and all(item["sec"] < 50 * 60 for item in first)
    second_half = select_episodes(
        bluey(2 * 3600 + 48 * 60 + 40, 24, [7 * 60 + 20] * 30), True
    )
    assert second_half is not None and len(second_half) == 30
    specials = select_episodes(
        bluey(
            3 * 3600 + 19 * 60 + 55,
            30,
            [7 * 60 + 20] * 21 + [8 * 60 + 23, 7 * 60 + 51],
            specials=(29 * 60 + 41,),
        ),
        True,
    )
    assert specials is not None and len(specials) == 24
    bumpers = select_episodes(
        bluey(3 * 3600 + 25 * 60 + 51, 29, [7 * 60 + 20] * 27 + [7 * 60 + 51])
        + [row(80, 2 * 60 + 17, 1), row(81, 2 * 60 + 33, 1)],
        True,
    )
    assert bumpers is not None and all(item["sec"] >= 3 * 60 for item in bumpers)
    movie = [row(0, 106 * 60, 16, 50)] + [row(index, 8 * 60, 1, index) for index in range(1, 4)]
    assert select_episodes(movie, False) is None
    assert select_episodes(movie, True) is None
    extras = [row(0, 90 * 60, 18, 90)] + [row(index, 8 * 60, 1, index) for index in range(1, 11)]
    assert select_episodes(extras, False) is None

    mapping = {}
    start, provisional = assign_start(mapping, "Bluey", 1, 1, 26, "disc-a", "Bluey (2018)")
    assert (start, provisional) == (1, False)
    start, provisional = assign_start(mapping, "Bluey", 1, 2, 26, "disc-b", "Bluey (2018)")
    assert (start, provisional) == (27, False)
    later = {}
    start, provisional = assign_start(later, "Bluey", 1, 2, 26, "disc-b", "Bluey (2018)")
    assert (start, provisional) == (2001, True)
    assign_start(later, "Bluey", 1, 1, 26, "disc-a", "Bluey (2018)")
    changes = settle_episode_map(later)
    assert changes and changes[0]["old_start"] == 2001 and changes[0]["new_start"] == 27
    assert later[episode_key("Bluey", 1, 2)]["provisional"] is False
    print("ok")
    return 0


def main():
    args = sys.argv[1:]
    if args[:1] == ["--self-test"]:
        raise SystemExit(self_test())
    if args[:1] == ["--plan-field"] and len(args) == 3:
        plan = json.loads(Path(args[2]).read_text(encoding="utf-8"))
        value = plan.get(args[1], "")
        print(value)
        return
    if args[:1] == ["--pending"] and len(args) == 2:
        emit_pending(args[1])
        return
    if args[:1] == ["--retire-movie"] and len(args) == 2:
        retire_playall(args[1])
        return
    if args[:1] == ["--settle"]:
        settle_episodes()
        return
    if not args:
        raise SystemExit("usage: choose-title.py INFO")
    lines = open(args[0], encoding="utf-8", errors="replace").read().splitlines()
    (STATE / "last-info.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
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
    raw_name = clean_name(disc)
    parsed = parse_show_label(raw_name)
    episode_rows = select_episodes(title_rows(titles, audio_counts), parsed["season_label"])
    if episode_rows:
        label = disc_id or raw_name
        fingerprint = disc_fingerprint(device_path())
        _by_fingerprint, _by_duration, by_series, _by_label = load_rules()
        shared = catalog_code(label) or label in by_series
        if shared and (fingerprint or episode_rows[0]["duration"]):
            identity = f"{label}-{fingerprint or episode_rows[0]['duration']}"
        else:
            identity = label
        plan = build_episode_plan(episode_rows, parsed, raw_name, identity, disc_kind(lines))
        print("episodes")
        print(plan)
        return
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
    by_fingerprint, by_duration, by_series, by_label = load_rules()

    chosen = ""
    if fingerprint and fingerprint.lower() in by_fingerprint:
        chosen = by_fingerprint[fingerprint.lower()]
    elif (label, duration) in by_duration:
        chosen = by_duration[(label, duration)]
    elif label in by_label:
        chosen = by_label[label]
    elif raw_name in by_label:
        chosen = by_label[raw_name]
    elif label in by_series and (catalog_code(raw_name) or raw_name == label):
        used = existing_names()
        used.update(by_fingerprint.values())
        used.update(by_duration.values())
        chosen = next_disc_name(by_series[label], used)
    if not chosen:
        chosen = identified_title(raw_name) or identified_title(label)
    if not chosen:
        chosen = (
            expand_disc_set(raw_name)
            or lookup_title(raw_name)
            or (lookup_title(label) if label and label != raw_name else "")
            or raw_name
        )
    chosen = clean_name(chosen)
    try:
        import title_card
        minutes = 0
        parts = (duration or "").split(":")
        if len(parts) == 3:
            minutes = int(parts[0]) * 60 + int(parts[1])
        chosen = title_card.dated_name(chosen, minutes or None)
    except (OSError, ValueError, json.JSONDecodeError):
        pass

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
