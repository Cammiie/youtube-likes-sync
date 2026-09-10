import re
import unicodedata
from difflib import SequenceMatcher


def normalized(s):
    s = unicodedata.normalize("NFKC", str(s)).casefold()
    s = re.sub(r"\b(official\s*(music\s*)?(video|audio)|lyrics?\s*video|lyrics?|visuali[sz]er)\b", "", s)
    return " ".join(re.sub(r"[^\w\s]", " ", s).split())


def similarity(a, b):
    a, b = normalized(a), normalized(b)
    return SequenceMatcher(None, a, b).ratio() if a and b else 0


def versions(s):
    return set(re.findall(r"\b(live|remix|remaster(?:ed)?|acoustic|instrumental|karaoke|cover|sped|slowed|edit)\b", normalized(s)))


def score(source, candidate):
    if source.get("isrc") and source["isrc"].upper() == candidate.get("isrc", "").upper():
        return 1000.0
    title = candidate["title"] + " " + candidate.get("version", "")
    artist = " ".join(source.get("artists", []))
    target_artist = " ".join(candidate.get("artists", []))
    value = 50 * similarity(source["title"], title) + 30 * similarity(artist, target_artist)
    source_duration, target_duration = source.get("duration") or 0, candidate.get("duration") or 0
    if source_duration and target_duration:
        value += 10 * max(0, 1 - abs(float(source_duration) - float(target_duration)) / 30)
    value += 5 * similarity(source.get("album", ""), candidate.get("album", ""))
    value += 5 if versions(source["title"]) == versions(title) else -15
    return value


def choose(source, candidates):
    if not candidates:
        return None
    # Numeric id makes equal scores deterministic across instances and restarts.
    return max(candidates, key=lambda c: (score(source, c), -int(c["id"])))
