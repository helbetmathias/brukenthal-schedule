import pdfplumber
import requests
import json
import re
import hashlib
import os
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo
from typing import Optional, Dict, List, Tuple

RO_TZ = ZoneInfo("Europe/Bucharest")
URL = "https://brukenthal.ro/"
HEADERS = {"User-Agent": "Mozilla/5.0"}
OUTPUT_FILE = "timetable.json"

# Cloudflare Worker notify endpoint
WORKER_NOTIFY_URL = "https://shrill-tooth-d37a.ronzigamespro2007.workers.dev/notify"
WORKER_AUTH_KEY = os.getenv("WORKER_AUTH_KEY", "")  # set in GitHub Actions secrets

DAY_MARKERS = {
    "MONTAG": "Luni",
    "DIENSTAG": "Marti",
    "MITTWOCH": "Miercuri",
    "DONNERSTAG": "Joi",
    "FREITAG": "Vineri",
}

TIME_RE = re.compile(r"^\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}$")

LICEU_CLASSES = {
    "9A", "9B", "9C", "9D",
    "10A", "10B", "10C", "10D",
    "11A", "11B", "11C", "11D",
    "12A", "12B", "12C", "12D",
}
GIMNAZIU_CLASSES = {
    "5A", "5B", "5C", "5D",
    "6A", "6B", "6C", "6D",
    "7A", "7B", "7C", "7D",
    "8A", "8B", "8C", "8D",
}

CLASS_RE_LICEU = re.compile(r"\b(9[ABCD]|1[0-2][ABCD])\b")
CLASS_RE_GIM = re.compile(r"\b([5-8][ABCD])\b")


def get_latest_pdf_url(kind: str) -> Optional[str]:
    """
    kind: 'liceu' or 'gimnaziu'
    """
    html = requests.get(URL, headers=HEADERS, timeout=30).text
    pdfs = re.findall(r'href="([^"]+\.pdf)"', html, flags=re.IGNORECASE)
    if not pdfs:
        return None

    kws = []
    if kind == "liceu":
        kws = ["liceu"]
    elif kind == "gimnaziu":
        kws = ["gimnaziu", "gimnazi", "gimnaz"]  # mai tolerant
    else:
        return None

    candidates = []
    for href in pdfs:
        low = href.lower()
        if any(k in low for k in kws):
            candidates.append(urljoin(URL, href))

    if not candidates:
        return None

    def score(u: str) -> List[int]:
        nums = re.findall(r"\d+", u)
        return [int(n) for n in nums] if nums else [0]

    candidates.sort(key=score, reverse=True)
    return candidates[0]


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


def is_time_slot(s: str) -> bool:
    s = (s or "").strip()
    return bool(TIME_RE.match(s))


def find_day_zones(page) -> List[Dict[str, float]]:
    words = page.extract_words(x_tolerance=2, y_tolerance=2)
    zones = []
    for w in words:
        t = w["text"].upper()
        if t in DAY_MARKERS:
            zones.append({"day": DAY_MARKERS[t], "top": w["top"], "bottom": w["bottom"]})
    zones.sort(key=lambda z: z["top"])
    return zones


def get_global_x_bounds(page) -> List[float]:
    verts = [e for e in page.edges if e.get("orientation") == "v"]
    xs = [e["x0"] for e in verts]
    x_bounds = sorted(cluster_positions(xs, tol=1.5))

    # vrem 18 bounds => 17 coloane (Time + 16 clase)
    if len(x_bounds) > 18:
        best = None
        for i in range(0, len(x_bounds) - 17):
            cand = x_bounds[i:i + 18]
            width = cand[-1] - cand[0]
            if best is None or width > best[0]:
                best = (width, cand)
        x_bounds = best[1]

    return x_bounds


def get_y_bounds_for_crop(page_crop) -> List[float]:
    horiz = [e for e in page_crop.edges if e.get("orientation") == "h"]
    ys = [e["top"] for e in horiz]
    y_bounds = sorted(cluster_positions(ys, tol=1.5))

    cleaned = []
    for y in y_bounds:
        if not cleaned or abs(y - cleaned[-1]) > 1.0:
            cleaned.append(y)
    return cleaned


def normalize_subject(subj: str) -> str:
    subj = (subj or "").strip()
    subj = re.sub(r"\s+", " ", subj)

    # artefact: litera singura
    if re.fullmatch(r"[a-z]", subj):
        return ""

    # uneori se lipeste un prefix mic inainte de cuvant (artefact)
    subj = re.sub(r"^[a-z](?=[A-Z0-9ĂÂÎȘȚ])", "", subj).strip()

    if len(subj) < 2:
        return ""
    return subj


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

    return re.sub(r"\s+", " ", " ".join([l for l in out_lines if l]).strip())


def extract_class_and_note(header_text: str, kind: str) -> Tuple[Optional[str], str]:
    """
    Din antetul coloanei (ex: '8D lab. bio', '7B cab. desen') extrage:
      - cls = '8D'
      - note = 'lab. bio' / 'cab. desen' (doar daca exista)
    """
    t = re.sub(r"\s+", " ", (header_text or "").strip())
    if not t:
        return None, ""

    rx = CLASS_RE_LICEU if kind == "liceu" else CLASS_RE_GIM
    m = rx.search(t)
    if not m:
        return None, ""

    cls = m.group(1)
    note = (t.replace(cls, "", 1)).strip()
    note = re.sub(r"\s+", " ", note)
    return cls, note


def parse_day_block(day_crop, x_bounds: List[float], kind: str) -> Tuple[Dict[str, List[str]], Dict[str, str]]:
    y_bounds = get_y_bounds_for_crop(day_crop)
    if len(y_bounds) < 5:
        return {}, {}

    chars = day_crop.chars
    n_rows = len(y_bounds) - 1
    n_cols = len(x_bounds) - 1

    grid = [["" for _ in range(n_cols)] for _ in range(n_rows)]
    for r in range(n_rows):
        ry0, ry1 = y_bounds[r], y_bounds[r + 1]
        for c in range(n_cols):
            cx0, cx1 = x_bounds[c], x_bounds[c + 1]
            grid[r][c] = cell_text_from_chars(chars, cx0, cx1, ry0, ry1)

    # gasim header row: randul cu cele mai multe "clase" detectate (chiar daca are 'cab./lab.')
    header_r = None
    best_score = -1
    for r in range(min(12, n_rows)):
        found = 0
        for c in range(1, min(n_cols, 40)):
            cls, _ = extract_class_and_note(grid[r][c], kind)
            if cls:
                found += 1
        if found > best_score:
            best_score = found
            header_r = r

    # prag minim: macar 6 clase detectate
    if header_r is None or best_score < 6:
        return {}, {}

    # mapam col -> cls si cls -> note (note e pe ziua aia!)
    col_to_class: Dict[int, str] = {}
    class_note: Dict[str, str] = {}
    for c in range(1, n_cols):
        cls, note = extract_class_and_note(grid[header_r][c], kind)
        if not cls:
            continue
        col_to_class[c] = cls
        if note:
            class_note[cls] = note

    day_schedule: Dict[str, List[str]] = {}
    for r in range(header_r + 1, n_rows):
        time_txt = (grid[r][0] or "").strip()
        if not is_time_slot(time_txt):
            continue

        for c, cls in col_to_class.items():
            subj = normalize_subject(grid[r][c])
            if not subj:
                continue
            # filtreaza cazuri unde in celula ajunge doar numele clasei etc.
            if subj in LICEU_CLASSES or subj in GIMNAZIU_CLASSES:
                continue
            day_schedule.setdefault(cls, [])
            entry = f"{time_txt} | {subj}"
            if entry not in day_schedule[cls]:
                day_schedule[cls].append(entry)

    return day_schedule, class_note


def parse_pdf(pdf_path: str, kind: str) -> Tuple[Dict[str, Dict[str, List[str]]], Dict[str, Dict[str, str]]]:
    final: Dict[str, Dict[str, List[str]]] = {}
    notes: Dict[str, Dict[str, str]] = {}

    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[0]
        x_bounds = get_global_x_bounds(page)

        zones = find_day_zones(page)
        if not zones:
            raise RuntimeError(f"[{kind}] Could not find day headers (MONTAG/DIENSTAG/...).")

        for i, z in enumerate(zones):
            day_name = z["day"]
            y_start = max(0, z["top"] - 8)
            y_end = zones[i + 1]["top"] - 6 if i + 1 < len(zones) else page.height

            crop = page.crop((0, y_start, page.width, y_end))
            day_block, day_notes = parse_day_block(crop, x_bounds, kind)

            for cls, entries in day_block.items():
                final.setdefault(cls, {})
                final[cls].setdefault(day_name, [])
                for e in entries:
                    if e not in final[cls][day_name]:
                        final[cls][day_name].append(e)

            for cls, note in day_notes.items():
                if note:
                    notes.setdefault(cls, {})
                    notes[cls][day_name] = note

    return final, notes


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


def filter_by_classes(schedule: Dict[str, dict], allowed: set) -> Dict[str, dict]:
    return {k: v for k, v in (schedule or {}).items() if k in allowed}


def main():
    liceu_url = get_latest_pdf_url("liceu")
    gim_url = get_latest_pdf_url("gimnaziu")

    if not liceu_url or not gim_url:
        print("Could not find both PDF links on site.")
        print("liceu_url:", liceu_url)
        print("gim_url:", gim_url)
        return

    # load old output (for incremental update + safety)
    old = {}
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                old = json.load(f) or {}
        except Exception:
            old = {}

    old_sources = (old.get("sources") or {})
    old_hash_liceu = ((old_sources.get("liceu") or {}).get("pdf_hash") or "")
    old_hash_gim = ((old_sources.get("gimnaziu") or {}).get("pdf_hash") or "")

    old_schedule = old.get("schedule") or {}
    old_notes = old.get("day_notes") or {}

    # download both
    tmp_l = "temp_liceu.pdf"
    tmp_g = "temp_gimnaziu.pdf"

    r1 = requests.get(liceu_url, headers=HEADERS, timeout=60)
    r1.raise_for_status()
    with open(tmp_l, "wb") as f:
        f.write(r1.content)

    r2 = requests.get(gim_url, headers=HEADERS, timeout=60)
    r2.raise_for_status()
    with open(tmp_g, "wb") as f:
        f.write(r2.content)

    new_hash_liceu = file_hash(tmp_l)
    new_hash_gim = file_hash(tmp_g)

    liceu_changed = (new_hash_liceu != old_hash_liceu)
    gim_changed = (new_hash_gim != old_hash_gim)

    if not liceu_changed and not gim_changed:
        print("Both PDFs unchanged, skipping update.")
        try:
            os.remove(tmp_l)
            os.remove(tmp_g)
        except OSError:
            pass
        return

    # parse only what changed; reuse old for the other
    schedule_liceu: Dict[str, Dict[str, List[str]]] = filter_by_classes(old_schedule, LICEU_CLASSES)
    notes_liceu: Dict[str, Dict[str, str]] = filter_by_classes(old_notes, LICEU_CLASSES)

    schedule_gim: Dict[str, Dict[str, List[str]]] = filter_by_classes(old_schedule, GIMNAZIU_CLASSES)
    notes_gim: Dict[str, Dict[str, str]] = filter_by_classes(old_notes, GIMNAZIU_CLASSES)

    try:
        if liceu_changed:
            schedule_liceu, notes_liceu = parse_pdf(tmp_l, kind="liceu")
        if gim_changed:
            schedule_gim, notes_gim = parse_pdf(tmp_g, kind="gimnaziu")
    except Exception as e:
        # safety: do NOT overwrite OUTPUT_FILE if parsing fails
        print("Parsing failed, keeping old JSON. Error:", repr(e))
        raise
    finally:
        try:
            os.remove(tmp_l)
            os.remove(tmp_g)
        except OSError:
            pass

    merged_schedule = {}
    merged_schedule.update(schedule_liceu)
    merged_schedule.update(schedule_gim)

    merged_notes = {}
    merged_notes.update(notes_liceu)
    merged_notes.update(notes_gim)

    out = {
        "updated_at": datetime.now(RO_TZ).strftime("%d.%m.%Y %H:%M"),
        "sources": {
            "liceu": {"source_pdf": liceu_url, "pdf_hash": new_hash_liceu},
            "gimnaziu": {"source_pdf": gim_url, "pdf_hash": new_hash_gim},
        },
        "schedule": merged_schedule,
    }

    # include notes only if we actually have any
    if merged_notes:
        out["day_notes"] = merged_notes

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print("Updated timetable.json | classes:", len(merged_schedule))
    if merged_notes:
        print("Day notes present for classes:", len(merged_notes))

    notify_worker(
        title="Schedule updated",
        body="A new timetable PDF was detected. Open the app to refresh.",
        data={
            "updated_at": out["updated_at"],
            "liceu_pdf": out["sources"]["liceu"]["source_pdf"],
            "gimnaziu_pdf": out["sources"]["gimnaziu"]["source_pdf"],
        },
    )


if __name__ == "__main__":
    main()
