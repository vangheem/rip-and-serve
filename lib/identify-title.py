#!/usr/bin/env python3
"""Name a disc from a couple of dialogue samples.

Volume labels like HP4 or HARRY_POTTER do not say which film is on the disc.
Take two short clips, transcribe them, and search that text plus the label.
A confident title is remembered so the next insert of that disc is not ripped
again under the old name.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

STATE = Path("/mnt/data/rip-work/state")
CACHE = STATE / "identify-cache"
WORK = STATE / "identify-work"
LOG_PATH = Path("/mnt/data/rip-work/logs/identify.log")
IDENTIFIED = STATE / "identified-names"
NAMES_PATH = STATE / "disc-names"
QUALITY_PATH = STATE / "encode-quality"
RENAME_LOG = STATE / "rename-log"
REVIEW_PATH = STATE / "identify-review"
JOBS = Path("/mnt/data/rip-work/jobs")
FFMPEG = "/usr/lib/jellyfin-ffmpeg/ffmpeg"
FFPROBE = "/usr/lib/jellyfin-ffmpeg/ffprobe"
IMAGE = "jellyfin/jellyfin"

JUNK = {
    "ntsc", "pal", "ws", "scn", "ff", "usa", "r1", "r2", "dvd", "bd",
    "bluray", "blu", "ray", "disc", "d1", "d2", "ac", "se", "special",
    "edition", "widescreen",
}
STOP = JUNK | {
    "the", "a", "an", "of", "and", "part", "vol", "volume", "episode",
    "movie", "film",
}
IMDB_TYPES = {
    "feature", "movie", "tvSeries", "tvMovie", "tvSpecial", "video", "tvEpisode",
    "tvMiniSeries", "tvShort", "TV series", "TV movie", "TV mini-series",
    "TV special", "TV short",
}
SEARCH_VERSION = 2
SAMPLE_SECONDS = 18


def dest_root():
    try:
        for line in open("/etc/rip-bluray.conf", encoding="utf-8"):
            if line.startswith("DEST_ROOT="):
                return Path(line.split("=", 1)[1].strip() or "/mnt/data/videos/Movies")
    except OSError:
        pass
    return Path("/mnt/data/videos/Movies")


def log(message):
    line = f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S+00:00')} {message}"
    print(line, flush=True)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def read_key(name):
    for path in (Path.home() / name,):
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            return text
    raise SystemExit(f"missing ~/{name}")


def catalog_code(name):
    return bool(re.fullmatch(r"[A-Z]{0,3}\d{3,6}", name or ""))


def needs_identify(stem):
    """A finished title has normal words. A disc label does not."""
    if not stem:
        return True
    if re.search(r"[a-z]", stem) and "_" not in stem and "#" not in stem:
        return False
    return True


def tokens(value):
    text = value.lower().replace("_", " ").replace("-", " ")
    text = re.sub(r"[^a-z0-9' ]+", " ", text)
    found = []
    for token in text.split():
        token = token.strip("'")
        if not token or token in STOP:
            continue
        if token.isdigit() and len(token) != 4 and int(token) > 20:
            continue
        found.append(token)
    return found


def humanize(hint):
    text = hint.replace("_", " ").replace("#", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def clean_filename(name):
    safe = name.replace("\u2019", "'").replace("\u2018", "'")
    safe = safe.replace("\u00ae", "").replace("\u2122", "")
    safe = safe.replace("\u2013", "-").replace("\u2014", "-")
    safe = safe.replace(":", " - ")
    safe = re.sub(r'[\\/*?"<>|]+', " ", safe)
    safe = re.sub(r"\s+", " ", safe).strip(" .")
    safe = re.sub(r"(?:\s+-\s+){2,}", " - ", safe)
    return safe


def disc_number(stem):
    text = stem.upper().replace("-", "_")
    match = re.search(r"(?:^|_)D(\d+)(?:_|$)", text)
    if match:
        return int(match.group(1))
    match = re.search(r"(?:^|_)([A-C])(?:_|$)", text)
    if match and re.search(r"\d", text):
        return ord(match.group(1)) - ord("A") + 1
    return None


def series_labels():
    found = set()
    try:
        lines = NAMES_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return found
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "series":
            found.add(parts[1])
    return found


def load_map(path):
    found = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return found
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        name, sep, value = line.partition(" = ")
        if sep and name.strip() and value.strip():
            found[name.strip()] = value.strip()
    return found


def save_map(path, mapping):
    lines = [f"{key} = {mapping[key]}" for key in sorted(mapping)]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def remember(label, title):
    label = (label or "").strip()
    title = clean_filename(title)
    if not label or not title or label == title:
        return
    if catalog_code(label) or label in series_labels():
        log(f"not remembering shared label {label}")
        return
    mapping = load_map(IDENTIFIED)
    mapping[label] = title
    save_map(IDENTIFIED, mapping)


def cache_file(media):
    stat = media.stat()
    key = hashlib.sha1(f"{media}:{stat.st_size}:{stat.st_mtime_ns}".encode()).hexdigest()[:20]
    return CACHE / f"{key}.json"


def load_cache(media):
    path = cache_file(media)
    try:
        return json.loads(path.read_text(encoding="utf-8")), path
    except (OSError, json.JSONDecodeError):
        return {}, path


def save_cache(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def request_json(url, payload=None, headers=None, timeout=60, method=None):
    data = None if payload is None else json.dumps(payload).encode()
    if method == "POST" and data is None:
        data = b""
    req_headers = {"User-Agent": "personal-ripper/1.0", "Accept": "application/json"}
    if payload is not None:
        req_headers["Content-Type"] = "application/json"
    if headers:
        req_headers.update(headers)
    request = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    last_error = "request failed"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8", "replace")
            return json.loads(body)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            last_error = f"HTTP {exc.code} from {url.split('?')[0]}: {detail}"
            if exc.code in {429, 500, 502, 503} and attempt < 2:
                time.sleep(8 * (attempt + 1))
                continue
            raise RuntimeError(last_error) from exc
        except (OSError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = str(exc)[:300]
            if attempt < 2:
                time.sleep(5)
                continue
            raise RuntimeError(last_error) from exc
    raise RuntimeError(last_error)


def docker_sh(script, args, timeout=180):
    uid = os.getuid()
    gid = os.getgid()
    command = [
        "docker", "run", "--rm", "-i",
        "--user", f"{uid}:{gid}",
        "-v", "/mnt/data:/mnt/data",
        "--entrypoint", "sh",
        IMAGE,
        "-s",
        *args,
    ]
    try:
        completed = subprocess.run(
            command,
            input=script.encode(),
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("ffmpeg timed out") from exc
    output = completed.stdout.decode("utf-8", "replace")
    if completed.returncode != 0 and "DURATION=" not in output:
        error = completed.stderr.decode("utf-8", "replace")[-400:]
        raise RuntimeError(f"ffmpeg failed: {error}")
    return output


def probe_and_sample(media):
    work = WORK / hashlib.sha1(str(media).encode()).hexdigest()[:12]
    if work.exists():
        for child in work.iterdir():
            child.unlink()
    else:
        work.mkdir(parents=True)
    script = r"""
set -eu
ff=/usr/lib/jellyfin-ffmpeg/ffmpeg
probe=/usr/lib/jellyfin-ffmpeg/ffprobe
input=$1
outdir=$2
shift 2
dur=$("$probe" -v error -show_entries format=duration -of default=nw=1:nk=1 "$input" || echo 0)
echo "DURATION=$dur"
n=0
for offset in "$@"; do
  n=$((n + 1))
  out="$outdir/clip-$n.mp3"
  log="$outdir/clip-$n.log"
  if ! "$ff" -y -ss "$offset" -i "$input" -t %d -vn -map 0:a:0 -ac 1 -ar 16000 \
      -c:a libmp3lame -b:a 64k \
      -af silencedetect=noise=-32dB:d=0.25,volumedetect \
      "$out" >"$log" 2>&1; then
    "$ff" -y -ss "$offset" -i "$input" -t %d -vn -ac 1 -ar 16000 \
      -c:a libmp3lame -b:a 64k \
      -af silencedetect=noise=-32dB:d=0.25,volumedetect \
      "$out" >"$log" 2>&1 || true
  fi
  echo "CLIP=$n"
done
""" % (SAMPLE_SECONDS, SAMPLE_SECONDS)
    duration = 0.0
    # Probe first so the offsets are inside the file.
    probed = docker_sh(
        "probe=/usr/lib/jellyfin-ffmpeg/ffprobe\n"
        'dur=$("$probe" -v error -show_entries format=duration -of default=nw=1:nk=1 "$1" || echo 0)\n'
        'echo "DURATION=$dur"\n',
        [str(media)],
        timeout=90,
    )
    duration = 0.0
    for line in probed.splitlines():
        if line.startswith("DURATION="):
            try:
                duration = float(line.split("=", 1)[1] or 0)
            except ValueError:
                duration = 0.0
    offsets = sample_offsets(duration)
    docker_sh(script, [str(media), str(work), *[str(int(item)) for item in offsets]], timeout=240)
    clips = []
    for path in sorted(work.glob("clip-*.mp3")):
        log_path = path.with_suffix(".log")
        text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
        silences = len(re.findall(r"silence_end", text))
        volume = -99.0
        match = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?) dB", text)
        if match:
            volume = float(match.group(1))
        if path.stat().st_size < 1000 or volume < -45:
            continue
        clips.append({"path": path, "silences": silences, "volume": volume})
    clips.sort(key=lambda item: (item["silences"], item["volume"]), reverse=True)
    return duration, clips


def sample_offsets(duration):
    if duration < 90:
        return [max(0, duration * 0.4)]
    if duration < 20 * 60:
        return [duration * 0.35, duration * 0.65]
    start = max(5 * 60, duration * 0.08)
    end = duration - max(4 * 60, duration * 0.08)
    span = max(30, end - start)
    return [start + span * 0.22, start + span * 0.55, start + span * 0.78]


def multipart(fields, file_path):
    boundary = "----RipIdentifyBoundary"
    chunks = []
    for key, value in fields.items():
        chunks.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode()
        )
    payload = Path(file_path).read_bytes()
    chunks.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"clip.mp3\"\r\n"
        f"Content-Type: audio/mpeg\r\n\r\n".encode()
        + payload
        + b"\r\n"
    )
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), boundary


def transcribe_file(path):
    key = read_key(".elevenlabs.key")
    errors = []
    for model in ("scribe_v2", "scribe_v1"):
        body, boundary = multipart(
            {
                "model_id": model,
                "language_code": "en",
                "tag_audio_events": "false",
                "timestamps_granularity": "word",
            },
            path,
        )
        request = urllib.request.Request(
            "https://api.elevenlabs.io/v1/speech-to-text",
            data=body,
            headers={
                "xi-api-key": key,
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            errors.append(f"HTTP {exc.code} {detail}")
            if exc.code in {429, 500, 502, 503}:
                time.sleep(8)
            continue
        except (OSError, TimeoutError, json.JSONDecodeError) as exc:
            errors.append(str(exc)[:200])
            continue
        text = (payload.get("text") or "").strip()
        if not text:
            words = [
                word.get("text", "")
                for word in payload.get("words") or []
                if word.get("type") == "word"
            ]
            text = " ".join(words).strip()
        text = re.sub(r"\s+", " ", text)
        if text:
            return text
    log(f"no transcript for {Path(path).name}: {' | '.join(errors) or 'empty response'}")
    return ""


def word_count(text):
    return len(re.findall(r"[A-Za-z0-9']+", text or ""))


def transcribe_clips(clips, duration):
    if not clips:
        return ""
    limit = 1 if duration < 20 * 60 else 2
    parts = []
    for clip in clips[:limit]:
        try:
            text = transcribe_file(clip["path"])
        except (OSError, RuntimeError, json.JSONDecodeError) as exc:
            log(f"clip failed: {exc}")
            continue
        log(f"transcript ({clip['silences']} pauses): {text[:180]}")
        if word_count(text) >= 4:
            parts.append(text)
    if word_count(" ".join(parts)) < 8 and len(clips) > limit:
        text = transcribe_file(clips[limit]["path"])
        log(f"extra transcript: {text[:180]}")
        if word_count(text) >= 4:
            parts.append(text)
    return " ".join(parts).strip()


def tavily_search(query):
    key = read_key(".tavily.key")
    payload = request_json(
        "https://api.tavily.com/search",
        {
            "query": query[:390],
            "search_depth": "basic",
            "topic": "general",
            "max_results": 5,
            "include_answer": "basic",
        },
        headers={"Authorization": f"Bearer {key}"},
        timeout=45,
    )
    answer = (payload.get("answer") or "").replace("\n", " ")
    log(f"search: {query[:140]}")
    log(f"answer: {answer[:240]}")
    return {
        "answer": payload.get("answer") or "",
        "results": [
            {
                "title": item.get("title") or "",
                "url": item.get("url") or "",
                "score": item.get("score") or 0,
            }
            for item in payload.get("results") or []
        ],
    }


def parse_result_title(title):
    """Return (title, is_short). Keep a subtitle; drop the site name."""
    short = bool(re.search(r"tv short|tv special|television special", title, re.I))
    text = title.strip()
    text = re.sub(
        r"\s+[-|–—]\s+(?:Wikipedia|IMDb|Rotten Tomatoes|Fandom|Wookieepedia|"
        r"Common Sense Media|Parents' Guide.*|Official Trailer.*|Amazon.*)$",
        "",
        text,
        flags=re.I,
    )
    text = re.sub(r"\s+\((?:TV Short|TV Special|television special)[^)]*\)\s*$", "", text, flags=re.I)
    text = re.sub(r"\s+\(\d{4}\)\s*$", "", text)
    text = re.sub(r"\s+\((?:film|movie|video|tv episode|tv series|soundtrack)[^)]*\)\s*$", "", text, flags=re.I)
    text = text.replace("\u2013", "-").replace("\u2014", "-")
    text = re.split(r"\b(?:clipzone|screen bites|clip zone)\b", text, flags=re.I)[0]
    text = re.sub(r"\s+-\s+(?:chapter|episode)\b.*$", "", text, flags=re.I)
    text = re.sub(r"\s+(?:quotes|script|transcript|soundtrack|trailer|review|conversation)$", "", text, flags=re.I)
    text = text.strip(" -")
    lowered = text.lower()
    if any(word in lowered for word in (
        "film series", "franchise", "cast & crew", "full cast", "trailer",
        "chapter", "transcript", "script", "quotes", "conversation", "review",
    )):
        return "", False
    if len(text) < 2 or len(text) > 90:
        return "", False
    return text, short


def imdb_suggest(query):
    quoted = urllib.parse.quote(query)
    url = f"https://v2.sg.media-imdb.com/suggestion/{urllib.parse.quote(query[:1])}/{quoted}.json"
    try:
        payload = request_json(url, timeout=8)
    except (OSError, RuntimeError, json.JSONDecodeError, ValueError):
        return []
    return payload.get("d") or []


def searchable_title(title):
    text = title.replace("'", "")
    text = re.split(r"\b(?:clipzone|screen bites|clip zone|sorting ceremony)\b", text, flags=re.I)[0]
    text = re.sub(r"\s+-\s+.*$", "", text)
    return " ".join(text.split()[:8])


def imdb_canonical(candidate):
    wanted = set(tokens(candidate))
    if not wanted:
        return ""
    best = ""
    best_score = 0
    for item in imdb_suggest(candidate):
        if item.get("q") not in IMDB_TYPES:
            continue
        title = item.get("l") or ""
        got = set(tokens(title))
        overlap = len(wanted & got)
        rare = any(len(token) >= 5 for token in wanted & got)
        if len(wanted) == 1:
            if overlap < 1 or (item.get("rank") or 999999) > 10000:
                continue
        elif overlap < 2 and not (overlap == 1 and rare):
            continue
        score = overlap * 10 - len(got - wanted) * 2
        if item.get("q") in {"feature", "movie", "tvMovie", "video", "tvSpecial", "tvMiniSeries"}:
            score += 5
        rank = item.get("rank") or 100000
        score += max(0, 4 - rank // 8000)
        if score > best_score:
            best_score = score
            best = title
    return best


def search_blob(search):
    parts = [search.get("answer") or ""]
    parts.extend(item.get("title") or "" for item in search.get("results") or [])
    return " ".join(parts)


def supports(title, search):
    wanted = set(tokens(title))
    if not wanted:
        return False
    got = set(tokens(search_blob(search)))
    return len(wanted & got) >= max(1, len(wanted) - 1)


def alpha_words(hint):
    found = []
    for token in tokens(hint):
        word = re.sub(r"\d+$", "", token)
        if len(word) >= 3 and word.isalpha():
            found.append(word)
    return found


def opaque_label(hint):
    """A catalog code. Semantic search will invent a movie for one of these.

    A glued all-caps token looks like a word. It is still a code.
    A short title, or a label with separate words, is not.
    """
    if not alpha_words(hint):
        return True
    label = (hint or "").strip()
    raw = "".join(char for char in label if char.isalnum())
    return bool(
        raw
        and raw == label
        and label.upper() == label
        and (any(char.isdigit() for char in raw) or len(raw) >= 7)
    )


def label_fit(title, hint):
    hint_words = alpha_words(hint)
    if not hint_words:
        return 0
    title_words = tokens(title)
    title_phrase = " ".join(title_words)
    hint_phrase = " ".join(hint_words)
    # One shared word is not a title. "anne" is inside too many different works.
    score = 6 if len(hint_words) >= 2 and hint_phrase and hint_phrase in title_phrase else 0
    for token in hint_words:
        if token in title_words:
            score += 2 if len(token) >= 5 else 1
    return score


def query_label(hint):
    """Drop disc-side noise so the search is the words a person would use."""
    words = []
    for word in re.findall(r"[A-Za-z0-9]+", humanize(hint)):
        low = word.lower()
        if low in JUNK or low in STOP or len(word) == 1:
            continue
        if word.isdigit():
            if int(word) <= 10:
                words.append(word)
            continue
        word = re.sub(r"\d+$", "", word)
        if word:
            words.append(word)
    return " ".join(words) or humanize(hint)


def excerpt(transcript):
    words = (transcript or "").split()
    if len(words) <= 32:
        return " ".join(words)
    best = " ".join(words[:28])
    best_score = -1
    for start in range(0, len(words) - 18, 5):
        window = words[start:start + 28]
        score = sum(2 for word in window if word[:1].isupper())
        score += sum(1 for word in window if len(word) >= 7)
        if score > best_score:
            best_score = score
            best = " ".join(window)
    return best


def answer_titles(answer, hint):
    if not answer or re.search(r"do not provide|cannot identify|not enough|no information", answer, re.I):
        return []
    found = []
    for match in re.finditer(r'["“]([^"”]{3,90})["”]', answer):
        found.append(match.group(1))
    for match in re.finditer(r"\bis ([A-Z0-9][^.]{2,90}?)(?:,|\.| a | an )", answer):
        found.append(match.group(1))
    hint_norm = re.sub(r"[^a-z0-9]+", "", humanize(hint).lower())
    titles = []
    for item in found:
        title, _short = parse_result_title(item)
        if not title:
            continue
        if re.sub(r"[^a-z0-9]+", "", title.lower()) == hint_norm:
            continue
        titles.append(title)
    return titles


def add_candidate(scores, sources, title, score, short=False):
    if score <= 0 or not title:
        return
    key = clean_filename(title).lower()
    if not key:
        return
    scores[key] = scores.get(key, 0) + score
    current = sources.setdefault(key, {"title": title, "short": False})
    current["short"] = current["short"] or short


def candidates_from(search, hint, weight):
    found = []
    for item in search.get("results") or []:
        title, short = parse_result_title(item.get("title") or "")
        if not title:
            continue
        score = weight + float(item.get("score") or 0)
        if title.lower() in (search.get("answer") or "").lower():
            score += 1.2
        found.append((title, score, short))
    answer = search.get("answer") or ""
    for title in answer_titles(answer, hint):
        found.append((title, weight + 2.0, "tv special" in answer.lower() or "tv short" in answer.lower()))
    return found


def series_list(answer, hint):
    phrase = " ".join(alpha_words(hint))
    if len(phrase) < 5:
        return False
    return answer.lower().count(phrase) >= 3


def reconcile(transcript, hint, duration, dialogue, label, combined):
    scores = {}
    sources = {}
    for title, score, short in (
        candidates_from(combined, hint, 3.5)
        + candidates_from(dialogue, hint, 2.0)
        + candidates_from(label, hint, 1.2)
    ):
        add_candidate(scores, sources, title, score, short)
    if not scores:
        return {"title": "", "guess": "", "confident": False, "reason": "search returned no title"}
    combined_answer = (combined.get("answer") or "").lower()
    hint_words = set(alpha_words(hint))
    for key, info in list(sources.items()):
        scores[key] += label_fit(info["title"], hint)
        title_words = set(tokens(info["title"]))
        if info["title"].lower() in combined_answer:
            scores[key] += 4
        label_numbers = [word for word in re.findall(r"\d+", hint) if word != "0" and int(word) <= 10]
        if label_numbers and any(number in info["title"] for number in label_numbers):
            scores[key] += 5
        if hint_words and hint_words <= title_words:
            scores[key] += 3
        elif hint_words and not (hint_words & title_words):
            scores[key] -= 3
        spoken = set(tokens(info["title"])) & set(tokens(transcript))
        if spoken:
            scores[key] += 1.5 + len(spoken)
        if duration and duration < 45 * 60 and info["short"]:
            scores[key] += 3
        if duration and duration > 80 * 60 and info["short"]:
            scores[key] -= 2
    for key, info in list(sources.items()):
        for other_key, other in list(sources.items()):
            if key != other_key and set(tokens(info["title"])) < set(tokens(other["title"])):
                scores[other_key] = scores.get(other_key, 0) + scores.get(key, 0) * 0.25
    ranked = sorted(scores, key=scores.get, reverse=True)
    best_key = ranked[0]
    best = sources[best_key]["title"]
    official = (
        imdb_canonical(searchable_title(best))
        or imdb_canonical(best)
        or imdb_canonical(query_label(hint))
        or ""
    )
    # A one-word disc label that is itself a well-known film should stay that
    # film. "HUGO" is Hugo, not the novel it was based on.
    hint_hit = imdb_canonical(query_label(hint))
    hint_has_number = any(word.isdigit() for word in query_label(hint).split())
    if (
        hint_hit
        and not hint_has_number
        and len(alpha_words(hint)) == 1
        and set(tokens(hint_hit)) <= set(tokens(query_label(hint)))
        and (not official or len(tokens(official)) > len(tokens(hint_hit)) + 1)
    ):
        official = hint_hit
    chosen = official or best
    # HP5 must not become the unrelated film "Phoenix", and Madagascar 3 must
    # not become the first Madagascar.
    sequel_numbers = [
        word for word in re.findall(r"\d+", re.sub(r"D\d+", "", hint, flags=re.I))
        if word != "0" and int(word) <= 10
    ]
    if sequel_numbers:
        numbered = [
            info["title"] for info in sources.values()
            if any(number in info["title"] for number in sequel_numbers)
            and set(alpha_words(hint)) & set(tokens(info["title"]))
        ]
        if numbered:
            picked = max(numbered, key=lambda title: (label_fit(title, hint), len(tokens(title))))
            chosen = imdb_canonical(picked) or picked
            official = chosen
    if len(tokens(chosen)) == 1 and any(char.isdigit() for char in hint):
        longer = [
            info["title"] for info in sources.values()
            if set(tokens(chosen)) < set(tokens(info["title"])) and len(tokens(info["title"])) >= 4
        ]
        if longer:
            picked = max(longer, key=lambda title: len(set(tokens(title)) & set(tokens(hint))))
            better = imdb_canonical(picked)
            if better:
                chosen = better
                official = better
    fit = label_fit(chosen, hint)
    combined_ok = supports(chosen, combined)
    dialogue_ok = supports(chosen, dialogue)
    label_ok = supports(chosen, label)
    spoken = set(tokens(chosen)) & set(tokens(transcript))
    lead = scores[best_key] - (scores[ranked[1]] if len(ranked) > 1 else 0)
    listed = series_list(search_blob(label) + " " + search_blob(combined), hint)
    reason = (
        f"fit={fit:.0f} combined={'yes' if combined_ok else 'no'} "
        f"dialogue={'yes' if dialogue_ok else 'no'} label={'yes' if label_ok else 'no'} "
        f"spoken={','.join(sorted(spoken)) or '-'} lead={lead:.1f}"
    )
    confident = bool(
        chosen and lead >= 0.8 and (
            combined_ok or (fit >= 6 and label_ok) or (spoken and dialogue_ok)
        )
    )
    if official and fit >= 8 and (label_ok or combined_ok):
        confident = True
    if not official:
        confident = False
        reason += "; no catalog match"
    if official and combined_ok and label_ok:
        confident = True
    hint_letters = re.sub(r"[^a-z]", "", re.sub(r"\d+", "", hint.lower()))
    title_letters = re.sub(r"[^a-z]", "", (official or "").lower())
    if official and label_ok and len(hint_letters) >= 6 and hint_letters in title_letters:
        confident = True
    if opaque_label(hint):
        # Commentary can mention another film. That is not this disc's title
        # unless the code itself is inside the title.
        code_letters = re.sub(r"[^a-z]", "", re.sub(r"\d+", "", (hint or "").lower()))
        named_letters = re.sub(r"[^a-z]", "", (chosen or "").lower())
        if not code_letters or code_letters not in named_letters:
            confident = False
            reason += "; catalog code does not match the title"
    if listed and not (set(tokens(chosen)) - set(tokens(hint))) & set(tokens(transcript)):
        distinctive = {token for token in set(tokens(hint)) & set(tokens(chosen)) if len(token) >= 5}
        numbered = any(word.isdigit() and int(word) <= 10 for word in re.findall(r"\d+", hint))
        if not distinctive and not numbered:
            confident = False
            reason += "; label is a series, dialogue did not pick a film"
    if duration and duration < 40 * 60 and fit >= 6 and not opaque_label(hint) and sources.get(best_key, {}).get("short") and combined_ok:
        confident = bool(chosen)
        reason += "; short matches the label"
    return {
        "title": clean_filename(chosen) if confident else "",
        "guess": clean_filename(chosen),
        "confident": confident,
        "reason": reason,
    }


def title_card_decision(media):
    """A title printed on screen beats a guess from dialogue."""
    try:
        import title_card
        found = title_card.match(Path(media))
    except (OSError, RuntimeError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        log(f"title card failed for {Path(media).name}: {exc}")
        return None
    if not found:
        return None
    log(f"title card {Path(media).name} -> {found['title']} ({found['reason']})")
    return {
        "title": found["title"],
        "guess": found["title"],
        "confident": True,
        "reason": found["reason"],
        "duration": found.get("duration") or 0,
        "transcript": "",
    }


def identify(media, hint, refresh_search=False):
    card = title_card_decision(media)
    if card:
        return card
    media = Path(media)
    cached, cache_path = load_cache(media)
    duration = float(cached.get("duration") or 0)
    transcript = cached.get("transcript") or ""
    searches = {} if refresh_search or cached.get("search_version") != SEARCH_VERSION else (cached.get("searches") or {})
    if not cached.get("sampled"):
        log(f"sampling {media.name}")
        duration, clips = probe_and_sample(media)
        transcript = transcribe_clips(clips, duration)
        cached.update({
            "sampled": True,
            "duration": duration,
            "transcript": transcript,
            "searches": {},
        })
        save_cache(cache_path, cached)
        searches = {}
    if not searches.get("combined") or not searches.get("label") or not searches.get("dialogue"):
        spoken = excerpt(transcript)
        label_text = query_label(hint)
        if spoken:
            dialogue_query = f"What movie or TV special contains this dialogue? {spoken}"
            combined_query = (
                f"Which movie or TV special is this? Disc label: \"{label_text}\". "
                f"Dialogue: \"{spoken}\""
            )
        else:
            dialogue_query = f"{label_text} movie or TV special title"
            combined_query = dialogue_query
        searches = {
            "dialogue": tavily_search(dialogue_query),
            "label": tavily_search(f"{label_text} movie or TV special title"),
            "combined": tavily_search(combined_query),
        }
        cached.update({
            "duration": duration,
            "transcript": transcript,
            "searches": searches,
            "search_version": SEARCH_VERSION,
        })
        save_cache(cache_path, cached)
    decision = reconcile(
        transcript,
        hint,
        duration,
        searches.get("dialogue") or {},
        searches.get("label") or {},
        searches.get("combined") or {},
    )
    decision["duration"] = duration
    decision["transcript"] = transcript
    cached["decision"] = {
        "title": decision["title"],
        "confident": decision["confident"],
        "reason": decision["reason"],
    }
    save_cache(cache_path, cached)
    return decision


def output_in_progress(basename):
    encoder = Path("/mnt/data/rip-work/status/encoder")
    try:
        text = encoder.read_text(encoding="utf-8")
    except OSError:
        text = ""
    if "state=encoding" in text:
        for line in text.splitlines():
            if line.startswith("job="):
                output = Path(line.split("=", 1)[1].strip()) / "output"
                try:
                    if Path(output.read_text(encoding="utf-8").strip()).name == basename:
                        return True
                except OSError:
                    pass
    if not JOBS.is_dir():
        return False
    for directory in JOBS.iterdir():
        try:
            status = (directory / "status").read_text(encoding="utf-8").strip()
            output = (directory / "output").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if status in {"ripping", "ripped", "encoding", "identifying"} and Path(output).name == basename:
            return True
    return False


def move_quality(old_name, new_name):
    mapping = load_map(QUALITY_PATH)
    if old_name not in mapping:
        return
    mapping[new_name] = mapping.pop(old_name)
    save_map(QUALITY_PATH, mapping)


def rename_movie(src, title):
    src = Path(src)
    dest = src.with_name(clean_filename(title) + src.suffix)
    if dest == src:
        return dest
    if dest.exists():
        raise FileExistsError(dest.name)
    src.rename(dest)
    move_quality(src.name, dest.name)
    with RENAME_LOG.open("a", encoding="utf-8") as handle:
        handle.write(
            f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S+00:00')} "
            f"{src.name} -> {dest.name}\n"
        )
    return dest


def taken_names():
    return {path.stem for path in dest_root().glob("*.mp4")}


def assign_filename(title, hint, taken):
    plain = clean_filename(title)
    numbered = [name for name in taken if name == plain or name.startswith(plain + " - Disc ")]
    if not numbered:
        number = disc_number(hint)
        if number and number > 1:
            return f"{plain} - Disc {number}"
        return plain
    if plain in taken and not any(name.startswith(plain + " - Disc ") for name in taken):
        return ""
    number = disc_number(hint)
    if number and f"{plain} - Disc {number}" not in taken:
        return f"{plain} - Disc {number}"
    count = 1
    while f"{plain} - Disc {count}" in taken:
        count += 1
    return f"{plain} - Disc {count}"


def apply_job(job):
    job = Path(job)
    try:
        disc_name = (job / "disc-name").read_text(encoding="utf-8").strip()
        mkv = (job / "mkv").read_text(encoding="utf-8").strip()
    except OSError:
        log(f"job {job} is missing a name or mkv")
        return 0
    disc_id = ""
    try:
        disc_id = (job / "disc-id").read_text(encoding="utf-8").strip()
    except OSError:
        pass
    if not needs_identify(disc_name):
        log(f"keeping {disc_name}")
        return 0
    if not Path(mkv).is_file():
        log(f"mkv missing for {disc_name}")
        return 0
    decision = identify(Path(mkv), disc_name)
    if not decision["confident"]:
        log(f"unsure about {disc_name}: {decision['reason']} guess={decision.get('guess')}")
        return 0
    filename = assign_filename(decision["title"], disc_name, taken_names())
    if not filename:
        log(f"{disc_name} looks like {decision['title']}, which is already in the library")
        return 0
    dest = dest_root() / f"{filename}.mp4"
    if dest.exists():
        log(f"not renaming job; {dest.name} already exists")
        return 0
    (job / "disc-name").write_text(filename + "\n", encoding="utf-8")
    (job / "output").write_text(str(dest) + "\n", encoding="utf-8")
    remember(disc_name, filename)
    if disc_id and disc_id != disc_name:
        remember(disc_id, filename)
    log(f"named {disc_name} as {filename}")
    return 0


def library_files():
    files = []
    for path in sorted(dest_root().glob("*.mp4")):
        if not needs_identify(path.stem):
            continue
        if output_in_progress(path.name):
            log(f"skipping {path.name}; a job is using it")
            continue
        files.append(path)
    return files


def sane_title(title):
    if not title or len(title) > 80 or word_count(title) > 12:
        return False
    lowered = title.lower()
    return not any(word in lowered for word in (
        "clip", "transcript", "chapter", "screen bites", "conversation", "quotes",
    ))


def family_key(stem):
    return re.sub(r"(_[A-C]|_D\d+)(_.*)?$", "", stem, flags=re.I)


def planned_name(title, hint, taken, number=None):
    if number:
        filename = f"{clean_filename(title)} - Disc {number}"
        if filename not in taken:
            return filename
    return assign_filename(title, hint, taken)


def fix_library():
    files = library_files()
    log(f"{len(files)} files need a real title")
    unsure = []
    groups = {}
    held = []
    for path in files:
        try:
            decision = identify(path, path.stem)
        except (OSError, RuntimeError, json.JSONDecodeError) as exc:
            log(f"failed {path.name}: {exc}")
            unsure.append(f"{path.name}: {exc}")
            continue
        if not decision["confident"] or not sane_title(decision.get("title") or ""):
            if decision.get("confident"):
                decision = dict(decision)
                decision["reason"] = (decision.get("reason") or "") + "; title looked like a clip or chapter"
            held.append((path, decision))
            continue
        log(f"decision {path.name} -> {decision['title']} ({decision['reason']})")
        groups.setdefault(decision["title"].lower(), []).append((path, decision))
    families = {}
    for items in groups.values():
        for path, decision in items:
            families.setdefault(family_key(path.stem), set()).add(decision["title"])
    for path, decision in held:
        titles = families.get(family_key(path.stem)) or set()
        duration = decision.get("duration") or 0
        sibling_duration = 0
        for items in groups.values():
            for other, other_decision in items:
                if family_key(other.stem) == family_key(path.stem):
                    sibling_duration = other_decision.get("duration") or sibling_duration
        same_length = (
            not duration or not sibling_duration
            or max(duration, sibling_duration) <= min(duration, sibling_duration) * 1.5
        )
        if len(titles) == 1 and family_key(path.stem) != path.stem and same_length:
            title = next(iter(titles))
            decision = dict(decision)
            decision["title"] = title
            decision["confident"] = True
            log(f"decision {path.name} -> {title} (same disc set)")
            groups.setdefault(title.lower(), []).append((path, decision))
            continue
        log(f"unsure {path.name}: {decision['reason']} guess={decision.get('guess')}")
        unsure.append(f"{path.name}: {decision.get('guess') or 'no guess'} ({decision['reason']})")

    renamed = 0
    taken = taken_names()
    for items in groups.values():
        items.sort(key=lambda item: (disc_number(item[0].stem) or 99, item[0].name))
        title = items[0][1]["title"]
        durations = [item[1].get("duration") or 0 for item in items]
        if len(items) > 1 and min(durations) and max(durations) > min(durations) * 1.5:
            for path, _decision in items:
                note = f"{path.name}: {title} also matched a file of a very different length"
                log(f"unsure {note}")
                unsure.append(note)
            continue
        markers = [disc_number(path.stem) for path, _decision in items]
        use_markers = (
            len(items) > 1
            and all(number is not None for number in markers)
            and len(set(markers)) == len(markers)
        )
        for index, (path, _decision) in enumerate(items, 1):
            number = markers[index - 1] if use_markers else (index if len(items) > 1 else None)
            filename = planned_name(title, path.stem, taken, number if len(items) > 1 else None)
            if not filename or filename in taken:
                log(f"leaving {path.name}; {title} is already in the library")
                unsure.append(f"{path.name}: collides with {title}")
                continue
            if filename == path.stem:
                remember(path.stem, filename)
                continue
            try:
                dest = rename_movie(path, filename)
            except FileExistsError as exc:
                log(f"leaving {path.name}; {exc}")
                unsure.append(f"{path.name}: {exc} already exists")
                continue
            taken.discard(path.stem)
            taken.add(dest.stem)
            remember(path.stem, dest.stem)
            log(f"renamed {path.name} -> {dest.name}")
            renamed += 1
    REVIEW_PATH.write_text("\n".join(unsure) + ("\n" if unsure else ""), encoding="utf-8")
    log(f"renamed {renamed}, still unsure {len(unsure)}")
    return renamed


def refresh_jellyfin():
    password_path = Path.home() / "jellyfin" / "admin-password"
    try:
        password = password_path.read_text(encoding="utf-8").strip()
    except OSError:
        log("jellyfin password file missing; library not refreshed")
        return
    header = 'MediaBrowser Client="rip", Device="rip", DeviceId="rip-identify", Version="1.0"'
    try:
        auth = request_json(
            "http://127.0.0.1:8096/Users/AuthenticateByName",
            {"Username": os.environ.get("JELLYFIN_USER") or Path.home().name, "Pw": password},
            headers={"Authorization": header, "Content-Type": "application/json"},
            timeout=15,
        )
    except (OSError, RuntimeError, json.JSONDecodeError):
        log("jellyfin login failed; library not refreshed")
        return
    token = auth.get("AccessToken") or ""
    user = (auth.get("User") or {}).get("Id") or ""
    if not token:
        log("jellyfin did not return a token")
        return
    auth_header = header + f', Token="{token}"'
    request = urllib.request.Request(
        "http://127.0.0.1:8096/Library/Refresh",
        data=b"",
        headers={"Authorization": auth_header},
        method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=15).read()
    except urllib.error.HTTPError as exc:
        if exc.code not in {200, 204}:
            log(f"jellyfin refresh failed: HTTP {exc.code}")
            return
    except OSError as exc:
        log(f"jellyfin refresh failed: {exc}")
        return
    log("asked Jellyfin to rescan the library")
    if user:
        log(f"jellyfin user {user} scan requested")


def main():
    args = sys.argv[1:]
    if "--apply-job" in args:
        index = args.index("--apply-job")
        raise SystemExit(apply_job(args[index + 1]))
    if "--fix-library" in args:
        renamed = fix_library()
        if renamed:
            refresh_jellyfin()
        return
    files = []
    refresh = "--refresh-search" in args
    for index, arg in enumerate(args):
        if arg == "--file" and index + 1 < len(args):
            files.append(args[index + 1])
    if not files:
        raise SystemExit("usage: identify-title.py --file PATH | --fix-library | --apply-job JOB")
    for raw in files:
        path = Path(raw)
        decision = identify(path, path.stem, refresh_search=refresh)
        print(json.dumps({
            "file": path.name,
            "title": decision.get("title"),
            "guess": decision.get("guess"),
            "confident": decision.get("confident"),
            "reason": decision.get("reason"),
            "transcript": (decision.get("transcript") or "")[:400],
        }, indent=2))


if __name__ == "__main__":
    main()
