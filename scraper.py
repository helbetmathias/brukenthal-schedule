import pdfplumber
import requests
import json
import re
import hashlib
import os
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo
from typing import Dict, List, Tuple, Optional, Set

RO_TZ = ZoneInfo("Europe/Bucharest")
URL = "https://brukenthal.ro/"
HEADERS = {"User-Agent": "Mozilla/5.0"}
OUTPUT_FILE = "timetable.json"

# Cloudflare Worker notify endpoint (optional)
WORKER_NOTIFY_URL = "https://shrill-tooth-d37a.ronzigamespro2007.workers.dev/notify"
WORKER_AUTH_KEY = os.getenv("WORKER_AUTH_KEY", "")

LICEU_CLASSES = [
    "9A", "9B", "9C", "9D",
    "10A", "10B", "10C", "10D",
    "11A", "11B", "11C", "11D",
    "12A", "12B", "12C", "12D",
]

GIMNAZIU_CLASSES = [
    "5A", "5B", "5C", "5D",
    "6A", "6B", "6C", "6D",
    "7A", "7B", "7C", "7D",
    "8A", "8B", "8C", "8D",
]

KIND_TO_CLASSES: Dict[str, List[str]] = {
    "liceu": LICEU_CLASSES,
    "gimnaziu": GIMNAZIU_CLASSES,
}

DAY_MARKERS = {
    "MONTAG": "Luni",
    "DIENSTAG": "Marti",
    "MITTWOCH": "Miercuri",
    "DONNERSTAG": "Joi",
    "FREITAG": "Vineri",
}

TIME_RE = re.compile(r"^\d{1,2}:\d{2}\s*[-–]\s*\d{1,2}:\d{2}$")

# Heuristic keywords that indicate a room/lab note in header (if class token is missing)
NOTE_HINTS = ("cab", "lab", "sala", "sală", "clasa", "clasă", "cl.", "cls", "aula")


def file_hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def cluster_positions(values: List[float], tol: float = 1.5) -> List[float]:
    values = sorted(values)
    clusters: List[List[float]] = []
    for v in values:
        if not clusters or abs(v - clusters[-1][-1]) > tol:
            clusters.append([v])
        else:
            clusters[-1].append(v)
    return [sum(c) / len(c) for c in clusters]


def normalize_ws(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def normalize_compact(s: str) -> str:
    return re.sub(r"\s+", "", (s or "")).upper()


def normalize_time_text(s: str) -> str:
    s = normalize_ws(s)
    s = s.replace("–", "-")
    s = re.sub(r"\s*-\s*", "-", s)
    return s


def is_time_slot(s: str) -> bool:
    s = normalize_ws(s)
    return bool(TIME_RE.match(s))


def normalize_subject(subj: str) -> str:
    subj = normalize_ws(subj)

    # junk single lowercase
    if re.fullmatch(r"[a-z]", subj):
        return ""

    # remove a single stray lowercase prefix only if it's clearly an OCR artifact (aXxx)
    subj = re.sub(r"^[a-z](?=[A-Z0-9ĂÂÎȘȚ])", "", subj).strip()

    if len(subj) < 2:
        return ""
    return subj


def download_to_tmp(pdf_url: str, tmp_name: str) -> None:
    resp = requests.get(pdf_url, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    with open(tmp_name, "wb") as f:
        f.write(resp.content)


# ---------------------------
# PDF discovery + kind detection
# ---------------------------

def get_all_pdf_urls() -> List[str]:
    resp = requests.get(URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    html = resp.text

    hrefs = re.findall(r'href=["\']([^"\']+\.pdf)["\']', html, flags=re.IGNORECASE)
    urls = [urljoin(URL, h) for h in hrefs]

    seen: Set[str] = set()
    out: List[str] = []
    for u in urls:
        u = u.split("#", 1)[0]
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def url_score(u: str) -> List[int]:
    nums = re.findall(r"\d+", u)
    return [int(n) for n in nums] if nums else [0]


def class_token_regex(cls: str) -> str:
    # allow "10 A" or "10A"
    digits, letter = cls[:-1], cls[-1]
    return rf"\b{re.escape(digits)}\s*{re.escape(letter)}\b"


def detect_pdf_kind_fast(pdf_path: str) -> Optional[str]:
    """
    Return 'liceu' / 'gimnaziu' / None by scanning first page text for class tokens.
    """
    try:
        with pdfplumber.open(pdf_path) as pdf:
            page = pdf.pages[0]
            text = normalize_ws(page.extract_text() or "")

            liceu_hits = sum(1 for c in LICEU_CLASSES if re.search(class_token_regex(c), text, flags=re.IGNORECASE))
            gim_hits = sum(1 for c in GIMNAZIU_CLASSES if re.search(class_token_regex(c), text, flags=re.IGNORECASE))

            # fallback: sometimes extract_text is poor; try words
            if max(liceu_hits, gim_hits) < 4:
                words = page.extract_words(x_tolerance=2, y_tolerance=2) or []
                wtext = normalize_ws(" ".join(w.get("text", "") for w in words))
                liceu_hits = max(
                    liceu_hits,
                    sum(1 for c in LICEU_CLASSES if re.search(class_token_regex(c), wtext, flags=re.IGNORECASE))
                )
                gim_hits = max(
                    gim_hits,
                    sum(1 for c in GIMNAZIU_CLASSES if re.search(class_token_regex(c), wtext, flags=re.IGNORECASE))
                )

            if liceu_hits >= 4 and liceu_hits > gim_hits:
                return "liceu"
            if gim_hits >= 4 and gim_hits > liceu_hits:
                return "gimnaziu"
            return None
    except Exception:
        return None


def pick_latest_pdfs_by_kind(max_probe: int = 8) -> Dict[str, Dict[str, str]]:
    """
    Downloads up to max_probe newest-ish PDFs and assigns them to liceu/gimnaziu
    based on content. Returns:
      {
        "liceu": {"url": ..., "tmp": ...},
        "gimnaziu": {"url": ..., "tmp": ...}
      }
    Keeps temp files for the winners (caller will parse & delete).
    """
    pdf_urls = get_all_pdf_urls()
    if not pdf_urls:
        return {}

    pdf_urls.sort(key=url_score, reverse=True)

    found: Dict[str, Dict[str, str]] = {}
    for i, u in enumerate(pdf_urls[:max_probe]):
        tmp = f"temp_probe_{i}.pdf"
        try:
            download_to_tmp(u, tmp)
        except Exception:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            continue

        kind = detect_pdf_kind_fast(tmp)

        if kind not in ("liceu", "gimnaziu"):
            try:
                os.remove(tmp)
            except OSError:
                pass
            continue

        if kind not in found:
            found[kind] = {"url": u, "tmp": tmp}
        else:
            try:
                os.remove(tmp)
            except OSError:
                pass

        if "liceu" in found and "gimnaziu" in found:
            break

    return found


# ---------------------------
# Table parsing
# ---------------------------

def find_day_zones(page) -> List[Dict[str, float]]:
    words = page.extract_words(x_tolerance=2, y_tolerance=2)
    zones = []
    for w in words:
        t = (w.get("text") or "").upper()
        if t in DAY_MARKERS:
            zones.append({"day": DAY_MARKERS[t], "top": w["top"], "bottom": w["bottom"]})
    zones.sort(key=lambda z: z["top"])
    return zones


def get_y_bounds_for_crop(page_crop) -> List[float]:
    horiz = [e for e in page_crop.edges if e.get("orientation") == "h"]
    ys = [e["top"] for e in horiz]
    y_bounds = sorted(cluster_positions(ys, tol=1.5))

    cleaned: List[float] = []
    for y in y_bounds:
        if not cleaned or abs(y - cleaned[-1]) > 1.0:
            cleaned.append(y)
    return cleaned


def merge_small_gaps(bounds: List[float], min_gap: float = 8.0) -> List[float]:
    """
    Collapse fake extra boundaries created by thick/double vertical rules.
    Real columns are wide; fake spacer columns are tiny.
    """
    if not bounds:
        return bounds

    out = [bounds[0]]
    for x in bounds[1:]:
        if x - out[-1] < min_gap:
            out[-1] = (out[-1] + x) / 2.0
        else:
            out.append(x)
    return out


def get_x_bounds_for_day(day_crop, expected_classes: List[str]) -> List[float]:
    """
    Detect x boundaries for one day block only.
    Expected boundaries:
      left border
      time/class separators
      ...
      right border

    Count must be:
      time column + N class columns => N+1 columns => N+2 boundaries
    """
    target_boundaries = len(expected_classes) + 2

    verts = [e for e in day_crop.edges if e.get("orientation") == "v"]
    xs = sorted(e["x0"] for e in verts)

    if not xs:
        raise RuntimeError("No vertical edges found in day crop.")

    # First cluster nearby raw positions
    x_bounds = cluster_positions(xs, tol=2.5)

    # Then collapse fake tiny columns caused by thick/double borders
    x_bounds = merge_small_gaps(x_bounds, min_gap=8.0)

    # If too many boundaries remain, merge the smallest gaps until the count matches
    while len(x_bounds) > target_boundaries:
        gaps = [x_bounds[i + 1] - x_bounds[i] for i in range(len(x_bounds) - 1)]
        i = min(range(len(gaps)), key=gaps.__getitem__)
        merged = (x_bounds[i] + x_bounds[i + 1]) / 2.0
        x_bounds = x_bounds[:i] + [merged] + x_bounds[i + 2:]

    if len(x_bounds) != target_boundaries:
        raise RuntimeError(
            f"Bad x-boundary count: got {len(x_bounds)}, expected {target_boundaries}. "
            f"Bounds={list(map(lambda x: round(x, 2), x_bounds))}"
        )

    widths = [x_bounds[i + 1] - x_bounds[i] for i in range(len(x_bounds) - 1)]
    tiny = [round(w, 2) for w in widths if w < 12]
    if tiny:
        raise RuntimeError(f"Still have suspicious tiny columns after merge: {tiny}")

    return x_bounds


def cell_text_from_chars(
    chars,
    x0, x1, y0, y1,
    y_tol=1.2,
    x_gap=1.0,
    x_pad_left=1.4,
    x_pad_right=0.35,
    y_pad=0.2
) -> str:
    sx0 = x0 + x_pad_left
    sx1 = x1 - x_pad_right
    sy0 = y0 + y_pad
    sy1 = y1 - y_pad

    if sx1 <= sx0:
        sx0, sx1 = x0, x1
    if sy1 <= sy0:
        sy0, sy1 = y0, y1

    sel = []
    for ch in chars:
        cx = (ch["x0"] + ch["x1"]) / 2
        cy = (ch["top"] + ch["bottom"]) / 2
        if (sx0 < cx < sx1) and (sy0 < cy < sy1):
            sel.append(ch)

    if not sel:
        return ""

    sel.sort(key=lambda c: (c["top"], c["x0"]))

    lines = []
    cur = []
    cur_top = None
    for ch in sel:
        if cur_top is None or abs(ch["top"] - cur_top) <= y_tol:
            cur.append(ch)
            cur_top = ch["top"] if cur_top is None else (cur_top * 0.7 + ch["top"] * 0.3)
        else:
            lines.append(cur)
            cur = [ch]
            cur_top = ch["top"]
    if cur:
        lines.append(cur)

    out_lines = []
    for line in lines:
        line.sort(key=lambda c: c["x0"])
        s = ""
        prev = None
        for ch in line:
            if prev is not None and (ch["x0"] - prev["x1"]) > x_gap:
                s += " "
            s += ch["text"]
            prev = ch
        out_lines.append(s.strip())

    return normalize_ws(" ".join([l for l in out_lines if l]))


def header_cell_matches_class(cell_text: str, cls: str) -> bool:
    txt = normalize_ws(cell_text)
    if not txt:
        return False

    if re.search(class_token_regex(cls), txt, flags=re.IGNORECASE):
        return True

    return normalize_compact(txt) == cls.upper()


def detect_header_row(grid: List[List[str]], expected_classes: List[str]) -> Optional[int]:
    best_r = None
    best_score = -1

    # header should be near top
    for r in range(min(12, len(grid))):
        found = set()

        # ignore column 0 (time/day label column)
        for cell in grid[r][1:]:
            for cls in expected_classes:
                if header_cell_matches_class(cell, cls):
                    found.add(cls)

        score = len(found)
        if score > best_score:
            best_score = score
            best_r = r

    if best_r is None:
        return None

    # need at least half of the classes to be confident
    if best_score < max(6, len(expected_classes) // 2):
        return None

    return best_r


def extract_header_note(header_cell: str, cls: str) -> str:
    txt = normalize_ws(header_cell)
    if not txt:
        return ""

    pattern = class_token_regex(cls)

    # if it contains the class token, strip it out
    if re.search(pattern, txt, flags=re.IGNORECASE):
        note = re.sub(pattern, "", txt, count=1, flags=re.IGNORECASE).strip()
        note = normalize_ws(note)
        note = note.strip(" -–|,.;:").strip()
        return note

    # fallback heuristic
    low = txt.lower()
    if any(h in low for h in NOTE_HINTS) and normalize_compact(txt) != cls.upper():
        return txt.strip(" -–|,.;:").strip()

    return ""


def parse_day_block(
    day_crop,
    x_bounds: List[float],
    expected_classes: List[str]
) -> Tuple[Dict[str, List[str]], Dict[str, str]]:
    y_bounds = get_y_bounds_for_crop(day_crop)
    if len(y_bounds) < 5:
        return {}, {}

    widths = [x_bounds[i + 1] - x_bounds[i] for i in range(len(x_bounds) - 1)]
    if any(w < 12 for w in widths):
        raise RuntimeError(f"Suspicious x bounds, tiny column widths found: {widths}")

    chars = day_crop.chars
    n_rows = len(y_bounds) - 1
    n_cols = len(x_bounds) - 1

    grid = [["" for _ in range(n_cols)] for _ in range(n_rows)]
    for r in range(n_rows):
        ry0, ry1 = y_bounds[r], y_bounds[r + 1]
        for c in range(n_cols):
            cx0, cx1 = x_bounds[c], x_bounds[c + 1]
            grid[r][c] = cell_text_from_chars(chars, cx0, cx1, ry0, ry1)

    header_r = detect_header_row(grid, expected_classes)
    if header_r is None:
        return {}, {}

    # fixed column mapping by index: col 0 is times, cols 1..N are classes
    max_class_cols = min(len(expected_classes), n_cols - 1)
    col_to_class = {c: expected_classes[c - 1] for c in range(1, 1 + max_class_cols)}

    # Extract notes from header row
    day_notes: Dict[str, str] = {}
    header_row = grid[header_r]
    for c, cls in col_to_class.items():
        note = extract_header_note(header_row[c] if c < len(header_row) else "", cls)
        if note:
            day_notes[cls] = note

    day_schedule: Dict[str, List[str]] = {cls: [] for cls in expected_classes}

    for r in range(header_r + 1, n_rows):
        time_txt = normalize_ws(grid[r][0])
        if not is_time_slot(time_txt):
            continue

        time_out = normalize_time_text(time_txt)

        for c, cls in col_to_class.items():
            subj = normalize_subject(grid[r][c])
            if not subj:
                continue

            # prevent weird accidental echoes of class labels
            if subj.upper() in {x.upper() for x in expected_classes}:
                continue

            entry = f"{time_out} | {subj}"
            if entry not in day_schedule[cls]:
                day_schedule[cls].append(entry)

    day_schedule = {k: v for k, v in day_schedule.items() if v}
    return day_schedule, day_notes


def parse_pdf(pdf_path: str, expected_classes: List[str]) -> Tuple[Dict[str, Dict[str, List[str]]], Dict[str, Dict[str, str]]]:
    final_schedule: Dict[str, Dict[str, List[str]]] = {}
    final_notes: Dict[str, Dict[str, str]] = {}

    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[0]

        zones = find_day_zones(page)
        if not zones:
            raise RuntimeError("Could not find day headers (MONTAG/DIENSTAG/...).")

        for i, z in enumerate(zones):
            day_name = z["day"]
            y_start = max(0, z["top"] - 8)
            y_end = zones[i + 1]["top"] - 6 if i + 1 < len(zones) else page.height

            crop = page.crop((0, y_start, page.width, y_end))

            # IMPORTANT: detect x-bounds per day block, not once globally for whole page
            x_bounds = get_x_bounds_for_day(crop, expected_classes)

            day_block, day_notes = parse_day_block(crop, x_bounds, expected_classes)

            for cls, entries in day_block.items():
                final_schedule.setdefault(cls, {})
                final_schedule[cls].setdefault(day_name, [])
                for e in entries:
                    if e not in final_schedule[cls][day_name]:
                        final_schedule[cls][day_name].append(e)

            for cls, note in day_notes.items():
                final_notes.setdefault(cls, {})
                if day_name in final_notes[cls] and final_notes[cls][day_name] != note:
                    if note not in final_notes[cls][day_name]:
                        final_notes[cls][day_name] = f"{final_notes[cls][day_name]}; {note}"
                else:
                    final_notes[cls][day_name] = note

    final_notes = {cls: dn for cls, dn in final_notes.items() if dn}
    return final_schedule, final_notes


def notify_worker(title: str, body: str, data: dict) -> None:
    if not WORKER_AUTH_KEY:
        print("No WORKER_AUTH_KEY set, skipping notification.")
        return

    try:
        resp = requests.post(
            f"{WORKER_NOTIFY_URL}?key={WORKER_AUTH_KEY}",
            json={"title": title, "body": body, "data": data},
            timeout=30,
        )
        print("Worker notify:", resp.status_code, resp.text[:200])
    except Exception as e:
        print("Worker notify failed:", repr(e))


def load_old_state() -> dict:
    if not os.path.exists(OUTPUT_FILE):
        return {}
    try:
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def main() -> None:
    found = pick_latest_pdfs_by_kind(max_probe=10)
    if not found:
        print("No usable timetable PDFs found on site.")
        return

    old = load_old_state()
    old_sources = old.get("sources") or {}
    old_schedule: Dict[str, Dict[str, List[str]]] = old.get("schedule") or {}
    old_day_notes: Dict[str, Dict[str, str]] = old.get("day_notes") or {}

    # Start from old state so if one PDF is missing this week, it doesn't vanish from JSON
    schedule_all: Dict[str, Dict[str, List[str]]] = dict(old_schedule)
    day_notes_all: Dict[str, Dict[str, str]] = dict(old_day_notes)

    sources_out: Dict[str, Dict[str, str]] = dict(old_sources)
    changed_any = not os.path.exists(OUTPUT_FILE)

    for kind, info in found.items():
        pdf_url = info["url"]
        tmp = info["tmp"]
        expected_classes = KIND_TO_CLASSES[kind]

        pdf_hash = file_hash(tmp)
        old_hash = (old_sources.get(kind) or {}).get("pdf_hash")

        sources_out[kind] = {"source_pdf": pdf_url, "pdf_hash": pdf_hash}
        if pdf_hash != old_hash:
            changed_any = True

        # remove old entries for this kind so they get replaced cleanly
        for cls in expected_classes:
            schedule_all.pop(cls, None)
            day_notes_all.pop(cls, None)

        try:
            new_schedule, new_notes = parse_pdf(tmp, expected_classes)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

        for cls, days in new_schedule.items():
            schedule_all.setdefault(cls, {})
            for day, entries in days.items():
                schedule_all[cls].setdefault(day, [])
                for e in entries:
                    if e not in schedule_all[cls][day]:
                        schedule_all[cls][day].append(e)

        for cls, dn in new_notes.items():
            day_notes_all.setdefault(cls, {})
            for day, note in dn.items():
                if day in day_notes_all[cls] and day_notes_all[cls][day] != note:
                    if note not in day_notes_all[cls][day]:
                        day_notes_all[cls][day] = f"{day_notes_all[cls][day]}; {note}"
                else:
                    day_notes_all[cls][day] = note

    if not changed_any and os.path.exists(OUTPUT_FILE):
        print("No detected changes, skipping update.")
        return

    out = {
        "updated_at": datetime.now(RO_TZ).strftime("%d.%m.%Y %H:%M"),
        "sources": sources_out,
        "schedule": schedule_all,
        "day_notes": {k: v for k, v in day_notes_all.items() if v},
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print("Updated timetable.json | classes:", len(schedule_all), "| day_notes classes:", len(out["day_notes"]))

    notify_worker(
        title="Schedule updated",
        body="A new timetable PDF was detected. Open the app to refresh.",
        data={
            "updated_at": out["updated_at"],
            "sources": out["sources"],
        },
    )


if __name__ == "__main__":
    main()
