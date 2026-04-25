import pdfplumber
import requests
import json
import re
import hashlib
import os
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo
from typing import Dict, List, Tuple, Optional, Set, Any

RO_TZ = ZoneInfo("Europe/Bucharest")
URL = "https://brukenthal.ro/"
HEADERS = {"User-Agent": "Mozilla/5.0"}
OUTPUT_FILE = "timetable.json"

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

DAY_ORDER = ["Luni", "Marti", "Miercuri", "Joi", "Vineri"]
EXPECTED_DAYS = set(DAY_ORDER)

TIME_RE = re.compile(r"^\d{1,2}:\d{2}\s*[-–]\s*\d{1,2}:\d{2}$")
NOTE_HINTS = ("cab", "lab", "sala", "sală", "clasa", "clasă", "cl.", "cls", "aula")


def file_hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def combined_hash(values: List[str]) -> str:
    h = hashlib.sha256()
    for v in sorted(values):
        h.update(v.encode("utf-8"))
        h.update(b"\n")
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

    if re.fullmatch(r"[a-z]", subj):
        return ""

    subj = re.sub(r"^[a-z](?=[A-Z0-9ĂÂÎȘȚ])", "", subj).strip()

    if len(subj) < 2:
        return ""

    return subj


def download_to_tmp(pdf_url: str, tmp_name: str) -> None:
    resp = requests.get(pdf_url, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    with open(tmp_name, "wb") as f:
        f.write(resp.content)


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
    digits, letter = cls[:-1], cls[-1]
    return rf"\b{re.escape(digits)}\s*{re.escape(letter)}\b"


def extract_week_key(url: str) -> str:
    """
    Extrage cheia săptămânii din numele PDF-ului.
    Exemple:
      orar2025-2026def-S28-27-30.04.pdf -> S28
      gimnaziu_ORAR-2025-2026_S3m5.pdf -> S3M5
      gimnaziu_ORAR-2025-2026_S1m5_var2-1.pdf -> S1M5
    """
    base = os.path.splitext(os.path.basename(url))[0].upper()
    parts = re.split(r"[_-]+", base)

    for p in parts:
        if re.fullmatch(r"S\d+[A-Z0-9]*", p):
            return p

    m = re.search(r"(S\d+[A-Z0-9]*)", base)
    return m.group(1) if m else base


def detect_pdf_kind_and_days(pdf_path: str) -> Tuple[Optional[str], Set[str]]:
    try:
        with pdfplumber.open(pdf_path) as pdf:
            page = pdf.pages[0]

            text = normalize_ws(page.extract_text() or "")
            words = page.extract_words(x_tolerance=2, y_tolerance=2) or []
            wtext = normalize_ws(" ".join(w.get("text", "") for w in words))
            full_text = f"{text} {wtext}".upper()

            liceu_hits = sum(
                1 for c in LICEU_CLASSES
                if re.search(class_token_regex(c), full_text, flags=re.IGNORECASE)
            )
            gim_hits = sum(
                1 for c in GIMNAZIU_CLASSES
                if re.search(class_token_regex(c), full_text, flags=re.IGNORECASE)
            )

            kind = None
            if liceu_hits >= 4 and liceu_hits > gim_hits:
                kind = "liceu"
            elif gim_hits >= 4 and gim_hits > liceu_hits:
                kind = "gimnaziu"

            days = {
                ro_name
                for de_name, ro_name in DAY_MARKERS.items()
                if de_name in full_text
            }

            return kind, days

    except Exception:
        return None, set()


def choose_current_week_group(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    groups: Dict[str, List[Dict[str, Any]]] = {}

    for item in items:
        groups.setdefault(item["week_key"], []).append(item)

    best_group = max(
        groups.values(),
        key=lambda group: max(tuple(x["score"]) for x in group)
    )

    return best_group


def choose_pdfs_for_kind(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not items:
        return []

    full_week = [x for x in items if x["days"] == EXPECTED_DAYS or x["day_count"] == 5]

    if full_week:
        full_week.sort(key=lambda x: (x["score"], x["day_count"]), reverse=True)
        return [full_week[0]]

    selected: List[Dict[str, Any]] = []
    covered: Set[str] = set()
    remaining = items[:]

    while remaining:
        best = max(
            remaining,
            key=lambda x: (
                len(x["days"] - covered),
                x["day_count"],
                x["score"],
            )
        )

        new_days = best["days"] - covered
        if not new_days:
            break

        selected.append(best)
        covered |= best["days"]
        remaining.remove(best)

        if covered == EXPECTED_DAYS:
            break

    if not selected:
        items_sorted = sorted(items, key=lambda x: (x["day_count"], x["score"]), reverse=True)
        return [items_sorted[0]]

    selected.sort(key=lambda x: x["score"], reverse=True)
    return selected


def pick_latest_pdfs_by_kind(max_probe: int = 30) -> Dict[str, List[Dict[str, Any]]]:
    pdf_urls = get_all_pdf_urls()

    if not pdf_urls:
        return {}

    pdf_urls.sort(key=url_score, reverse=True)

    candidates: Dict[str, List[Dict[str, Any]]] = {
        "liceu": [],
        "gimnaziu": [],
    }

    for i, u in enumerate(pdf_urls[:max_probe]):
        tmp = f"temp_probe_{i}.pdf"

        try:
            download_to_tmp(u, tmp)
            kind, days = detect_pdf_kind_and_days(tmp)
        except Exception:
            kind, days = None, set()

        if kind not in ("liceu", "gimnaziu"):
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            continue

        candidates[kind].append({
            "url": u,
            "tmp": tmp,
            "days": set(days),
            "day_count": len(days),
            "score": url_score(u),
            "week_key": extract_week_key(u),
        })

    chosen: Dict[str, List[Dict[str, Any]]] = {}

    for kind, items in candidates.items():
        if not items:
            continue

        current_week_items = choose_current_week_group(items)
        selected = choose_pdfs_for_kind(current_week_items)

        if selected:
            chosen[kind] = selected

        keep = {item["tmp"] for item in selected}

        for item in items:
            if item["tmp"] not in keep:
                try:
                    os.remove(item["tmp"])
                except OSError:
                    pass

    return chosen


def find_day_zones(page) -> List[Dict[str, float]]:
    words = page.extract_words(x_tolerance=2, y_tolerance=2) or []
    zones = []

    for w in words:
        raw = normalize_ws(w.get("text") or "")
        compact = normalize_compact(raw)

        for de_name, ro_name in DAY_MARKERS.items():
            if de_name in compact:
                zones.append({
                    "day": ro_name,
                    "top": w["top"],
                    "bottom": w["bottom"],
                })
                break

    zones.sort(key=lambda z: z["top"])

    dedup: List[Dict[str, float]] = []
    for z in zones:
        if dedup and dedup[-1]["day"] == z["day"] and abs(dedup[-1]["top"] - z["top"]) < 4:
            continue
        dedup.append(z)

    return dedup


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
    target_boundaries = len(expected_classes) + 2

    verts = [e for e in day_crop.edges if e.get("orientation") == "v"]
    xs = sorted(e["x0"] for e in verts)

    if not xs:
        raise RuntimeError("No vertical edges found in day crop.")

    x_bounds = cluster_positions(xs, tol=2.5)
    x_bounds = merge_small_gaps(x_bounds, min_gap=8.0)

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

    for r in range(min(12, len(grid))):
        found = set()

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

    if best_score < max(6, len(expected_classes) // 2):
        return None

    return best_r


def extract_header_note(header_cell: str, cls: str) -> str:
    txt = normalize_ws(header_cell)

    if not txt:
        return ""

    pattern = class_token_regex(cls)

    if re.search(pattern, txt, flags=re.IGNORECASE):
        note = re.sub(pattern, "", txt, count=1, flags=re.IGNORECASE).strip()
        note = normalize_ws(note)
        note = note.strip(" -–|,.;:").strip()
        return note

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

    max_class_cols = min(len(expected_classes), n_cols - 1)
    col_to_class = {c: expected_classes[c - 1] for c in range(1, 1 + max_class_cols)}

    day_notes: Dict[str, str] = {}
    header_row = grid[header_r]

    for c, cls in col_to_class.items():
        note = extract_header_note(header_row[c] if c < len(header_row) else "", cls)

        if note:
            day_notes[cls] = note

    day_schedule: Dict[str, List[str]] = {cls: [] for cls in expected_classes}
    expected_class_set = {x.upper() for x in expected_classes}

    for r in range(header_r + 1, n_rows):
        time_txt = normalize_ws(grid[r][0])

        if not is_time_slot(time_txt):
            continue

        time_out = normalize_time_text(time_txt)

        for c, cls in col_to_class.items():
            subj = normalize_subject(grid[r][c])

            if not subj:
                continue

            if subj.upper() in expected_class_set:
                continue

            entry = f"{time_out} | {subj}"

            if entry not in day_schedule[cls]:
                day_schedule[cls].append(entry)

    day_schedule = {k: v for k, v in day_schedule.items() if v}

    return day_schedule, day_notes


def parse_pdf(
    pdf_path: str,
    expected_classes: List[str]
) -> Tuple[Dict[str, Dict[str, List[str]]], Dict[str, Dict[str, str]]]:
    final_schedule: Dict[str, Dict[str, List[str]]] = {}
    final_notes: Dict[str, Dict[str, str]] = {}

    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[0]

        zones = find_day_zones(page)

        if not zones:
            raise RuntimeError("Could not find day headers.")

        seen_days: Set[str] = set()
        unique_zones: List[Dict[str, float]] = []

        for z in zones:
            if z["day"] not in seen_days:
                unique_zones.append(z)
                seen_days.add(z["day"])

        zones = unique_zones

        for i, z in enumerate(zones):
            day_name = z["day"]
            y_start = max(0, z["top"] - 8)
            y_end = zones[i + 1]["top"] - 6 if i + 1 < len(zones) else page.height

            crop = page.crop((0, y_start, page.width, y_end))
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


def build_empty_kind_schedule(expected_classes: List[str]) -> Dict[str, Dict[str, List[str]]]:
    return {
        cls: {day: [] for day in DAY_ORDER}
        for cls in expected_classes
    }


def schedule_days_present(schedule: Dict[str, Dict[str, List[str]]]) -> Set[str]:
    out: Set[str] = set()

    for cls_days in schedule.values():
        for day, entries in cls_days.items():
            if entries:
                out.add(day)

    return out


def merge_schedule_parts(
    target: Dict[str, Dict[str, List[str]]],
    source: Dict[str, Dict[str, List[str]]]
) -> None:
    for cls, days in source.items():
        target.setdefault(cls, {})

        for day, entries in days.items():
            target[cls].setdefault(day, [])

            for e in entries:
                if e not in target[cls][day]:
                    target[cls][day].append(e)


def merge_notes_parts(
    target: Dict[str, Dict[str, str]],
    source: Dict[str, Dict[str, str]]
) -> None:
    for cls, days in source.items():
        target.setdefault(cls, {})

        for day, note in days.items():
            if day in target[cls] and target[cls][day] != note:
                if note not in target[cls][day]:
                    target[cls][day] = f"{target[cls][day]}; {note}"
            else:
                target[cls][day] = note


def notify_worker(title: str, body: str, data: dict) -> None:
    if not WORKER_AUTH_KEY:
        print("No WORKER_AUTH_KEY set, skipping notification.")
        return

    try:
        resp = requests.post(
            f"{WORKER_NOTIFY_URL}?key={WORKER_AUTH_KEY}",
            json={
                "title": title,
                "body": body,
                "data": data,
            },
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
    found = pick_latest_pdfs_by_kind(max_probe=30)

    if not found:
        print("No usable timetable PDFs found on site.")
        return

    old = load_old_state()
    old_sources = old.get("sources") or {}
    old_schedule: Dict[str, Dict[str, List[str]]] = old.get("schedule") or {}
    old_day_notes: Dict[str, Dict[str, str]] = old.get("day_notes") or {}

    schedule_all: Dict[str, Dict[str, List[str]]] = {
        cls: {day: list(entries) for day, entries in days.items()}
        for cls, days in old_schedule.items()
    }

    day_notes_all: Dict[str, Dict[str, str]] = {
        cls: dict(days)
        for cls, days in old_day_notes.items()
    }

    sources_out: Dict[str, Dict[str, Any]] = dict(old_sources)
    changed_any = not os.path.exists(OUTPUT_FILE)

    for kind, selected_items in found.items():
        expected_classes = KIND_TO_CLASSES[kind]

        kind_schedule: Dict[str, Dict[str, List[str]]] = build_empty_kind_schedule(expected_classes)
        kind_notes: Dict[str, Dict[str, str]] = {}

        selected_urls = [item["url"] for item in selected_items]
        selected_day_union: Set[str] = set()
        pdf_hashes: List[str] = []
        week_keys = sorted({item["week_key"] for item in selected_items})

        for item in selected_items:
            selected_day_union |= set(item["days"])
            pdf_hashes.append(file_hash(item["tmp"]))

            try:
                part_schedule, part_notes = parse_pdf(item["tmp"], expected_classes)
                merge_schedule_parts(kind_schedule, part_schedule)
                merge_notes_parts(kind_notes, part_notes)
            finally:
                try:
                    os.remove(item["tmp"])
                except OSError:
                    pass

        parsed_days = schedule_days_present(kind_schedule)

        parsed_class_count = sum(
            1 for cls, days in kind_schedule.items()
            if any(days.get(day) for day in DAY_ORDER)
        )

        if not parsed_days:
            print(f"[WARN] {kind}: selected PDFs produced no parsed schedule. Keeping old state.")
            continue

        kind_hash = combined_hash(pdf_hashes)
        old_hash = (old_sources.get(kind) or {}).get("pdf_hash")

        sources_out[kind] = {
            "source_pdfs": selected_urls,
            "pdf_hash": kind_hash,
            "pdf_hashes": pdf_hashes,
            "coverage_days": sorted(selected_day_union, key=lambda d: DAY_ORDER.index(d)),
            "parsed_days": sorted(parsed_days, key=lambda d: DAY_ORDER.index(d)),
            "week_keys": week_keys,
        }

        if kind_hash != old_hash:
            changed_any = True

        print(f"[{kind}] source PDFs: {len(selected_items)}")
        print(f"[{kind}] week keys: {week_keys}")
        print(f"[{kind}] selected day coverage: {sorted(selected_day_union, key=lambda d: DAY_ORDER.index(d))}")
        print(f"[{kind}] parsed days: {sorted(parsed_days, key=lambda d: DAY_ORDER.index(d))}")
        print(f"[{kind}] parsed classes: {parsed_class_count}")

        # IMPORTANT:
        # Înlocuim complet orarul vechi pentru liceu/gimnaziu.
        # Zilele lipsă din PDF-ul curent devin [].
        # Nu mai moștenim Vineri sau altă zi din săptămâna trecută.
        for cls in expected_classes:
            schedule_all[cls] = {
                day: list(kind_schedule.get(cls, {}).get(day, []))
                for day in DAY_ORDER
            }
            day_notes_all.pop(cls, None)

        merge_notes_parts(day_notes_all, kind_notes)

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

    print(
        "Updated timetable.json | classes:",
        len(schedule_all),
        "| day_notes classes:",
        len(out["day_notes"])
    )

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
