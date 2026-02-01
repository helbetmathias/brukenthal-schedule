import pdfplumber
import requests
import json
import re
import hashlib
import os
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

RO_TZ = ZoneInfo("Europe/Bucharest")
BASE_URL = "https://brukenthal.ro/"
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

# accept -, – , —
TIME_RE = re.compile(r"^\d{1,2}:\d{2}\s*[–\-—]\s*\d{1,2}:\d{2}$")

# 5A..12D
CLASS_CODE_RE = re.compile(r"\b([5-9]|1[0-2])[A-D]\b", re.IGNORECASE)

# OPTIONAL: mici corecții pentru artefacte frecvente (poți extinde)
SUBJECT_PREFIX_FIXES = [
    (re.compile(r"^(st)(?=[A-Z]|-)", re.IGNORECASE), "Ist"),
    (re.compile(r"^(nfo)(?=[A-Z]|-)", re.IGNORECASE), "Info"),
    (re.compile(r"^(otbal)", re.IGNORECASE), "Fotbal"),
]


def get_latest_pdf_url(kind: str) -> str | None:
    """
    kind: 'liceu' or 'gimnaziu'
    """
    html = requests.get(BASE_URL, headers=HEADERS, timeout=30).text
    pdfs = re.findall(r'href="([^"]+\.pdf)"', html, flags=re.IGNORECASE)
    if not pdfs:
        return None

    def ok(href: str) -> bool:
        h = href.lower()
        if kind == "liceu":
            return "liceu" in h
        if kind == "gimnaziu":
            return ("gimnaziu" in h) or ("gimn" in h) or ("gimnaz" in h)
        return False

    candidates = [urljoin(BASE_URL, h) for h in pdfs if ok(h)]
    if not candidates:
        return None

    # sortăm după toate numerele din URL (an, lună, zi, Sxx etc.)
    def score(url: str):
        nums = re.findall(r"\d+", url)
        return [int(n) for n in nums] if nums else [0]

    candidates.sort(key=score, reverse=True)
    return candidates[0]


def file_hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def cluster_positions(values, tol=1.5):
    values = sorted(values)
    clusters = []
    for v in values:
        if not clusters or abs(v - clusters[-1][-1]) > tol:
            clusters.append([v])
        else:
            clusters[-1].append(v)
    return [sum(c) / len(c) for c in clusters]


def is_time_slot(s: str) -> bool:
    s = (s or "").strip()
    return bool(TIME_RE.match(s))


def extract_class_code(s: str) -> str | None:
    if not s:
        return None
    s = re.sub(r"\s+", " ", s).strip()
    m = CLASS_CODE_RE.search(s)
    return m.group(0).upper() if m else None


def find_day_zones(page):
    # robust: strip punctuation from extracted words
    words = page.extract_words(x_tolerance=2, y_tolerance=2)
    zones = []
    for w in words:
        raw = (w.get("text") or "").upper()
        t = re.sub(r"[^A-ZĂÂÎȘȚ]", "", raw)
        if t in DAY_MARKERS:
            zones.append({"day": DAY_MARKERS[t], "top": w["top"], "bottom": w["bottom"]})
    zones.sort(key=lambda z: z["top"])
    return zones


def get_global_x_bounds(page):
    # IMPORTANT: fără filtre pe lungime (tabelele au segmente scurte pe zile)
    verts = [e for e in page.edges if e.get("orientation") == "v"]
    xs = [e["x0"] for e in verts if "x0" in e]
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

    # fallback: dacă nu avem destule, aproximăm uniform (mai bine decât nimic)
    if len(x_bounds) < 18 and xs:
        lo, hi = min(xs), max(xs)
        step = (hi - lo) / 17.0 if hi > lo else 1.0
        x_bounds = [lo + i * step for i in range(18)]

    return x_bounds


def get_y_bounds_for_crop(page_crop):
    # IMPORTANT: fără filtre pe lungime
    horiz = [e for e in page_crop.edges if e.get("orientation") == "h"]
    ys = [e.get("top", e.get("y0", 0)) for e in horiz]
    y_bounds = sorted(cluster_positions(ys, tol=1.5))

    cleaned = []
    for y in y_bounds:
        if not cleaned or abs(y - cleaned[-1]) > 1.0:
            cleaned.append(y)
    return cleaned


def normalize_subject(subj: str) -> str:
    subj = (subj or "").strip()
    subj = re.sub(r"\s+", " ", subj)
    if len(subj) < 2:
        return ""

    # aruncă celule “a”, “b”, etc.
    if re.fullmatch(r"[a-z]", subj):
        return ""

    # corecții mici pt artefacte (opțional)
    for rx, repl in SUBJECT_PREFIX_FIXES:
        subj = rx.sub(repl, subj)

    # dacă rămâne foarte scurt, ignoră
    subj = subj.strip()
    if len(subj) < 2:
        return ""

    return subj


def cell_text_from_chars(
    chars,
    x0, x1, y0, y1,
    y_tol=1.2,
    x_gap=1.0,
    x_pad_left=0.6,   # mai mic ca să nu “mănânce” prima literă
    x_pad_right=0.35,
    y_pad=0.2
):
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


def parse_day_block(day_crop, x_bounds):
    y_bounds = get_y_bounds_for_crop(day_crop)
    if len(y_bounds) < 5:
        return {}

    chars = day_crop.chars
    n_rows = len(y_bounds) - 1
    n_cols = len(x_bounds) - 1
    if n_cols < 5:
        return {}

    grid = [["" for _ in range(n_cols)] for _ in range(n_rows)]
    for r in range(n_rows):
        ry0, ry1 = y_bounds[r], y_bounds[r + 1]
        for c in range(n_cols):
            cx0, cx1 = x_bounds[c], x_bounds[c + 1]
            grid[r][c] = cell_text_from_chars(chars, cx0, cx1, ry0, ry1)

    # header row = rândul cu cele mai multe coduri de clasă (chiar dacă are "lab./cab." în antet)
    header_r = None
    best_hits = -1
    for r in range(min(12, n_rows)):
        hits = 0
        for c in range(1, n_cols):
            if extract_class_code(grid[r][c]):
                hits += 1
        if hits > best_hits:
            best_hits = hits
            header_r = r

    if header_r is None or best_hits < 6:
        return {}

    # mapăm coloanele după clasa din antet: "8D lab. bio" -> "8D"
    col_to_class = {}
    seen = set()
    for c in range(1, min(17, n_cols)):  # max 16 clase
        cls = extract_class_code(grid[header_r][c])
        if not cls or cls in seen:
            continue
        col_to_class[c] = cls
        seen.add(cls)

    if not col_to_class:
        return {}

    day_schedule = {cls: [] for cls in seen}

    for r in range(header_r + 1, n_rows):
        time_txt = (grid[r][0] or "").strip()
        if not is_time_slot(time_txt):
            continue

        for c, cls in col_to_class.items():
            subj = normalize_subject(grid[r][c])
            if not subj:
                continue
            entry = f"{time_txt} | {subj}"
            if entry not in day_schedule[cls]:
                day_schedule[cls].append(entry)

    return {k: v for k, v in day_schedule.items() if v}


def parse_pdf(pdf_path: str):
    """
    returnează { "5A": {"Luni":[...], ...}, "9A": {...}, ... }
    """
    final = {}

    with pdfplumber.open(pdf_path) as pdf:
        for page_idx, page in enumerate(pdf.pages):
            zones = find_day_zones(page)
            if not zones:
                continue

            x_bounds = get_global_x_bounds(page)
            if not x_bounds or len(x_bounds) < 10:
                continue

            for i, z in enumerate(zones):
                day_name = z["day"]
                y_start = max(0, z["top"] - 8)
                y_end = zones[i + 1]["top"] - 6 if i + 1 < len(zones) else page.height

                crop = page.crop((0, y_start, page.width, y_end))
                day_block = parse_day_block(crop, x_bounds)

                for cls, entries in day_block.items():
                    final.setdefault(cls, {})
                    final[cls].setdefault(day_name, [])
                    for e in entries:
                        if e not in final[cls][day_name]:
                            final[cls][day_name].append(e)

    return final


def merge_schedules(dst: dict, src: dict):
    for cls, days in src.items():
        dst.setdefault(cls, {})
        for day, entries in days.items():
            dst[cls].setdefault(day, [])
            for e in entries:
                if e not in dst[cls][day]:
                    dst[cls][day].append(e)


def notify_worker(title, body, data):
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


def main():
    liceu_url = get_latest_pdf_url("liceu")
    gim_url = get_latest_pdf_url("gimnaziu")

    if not liceu_url and not gim_url:
        print("No PDF links found on site (liceu/gimnaziu).")
        return

    # citește vechiul json (hash-uri)
    old_hashes = {"liceu": None, "gimnaziu": None}
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                old = json.load(f)
                old_hashes["liceu"] = (old.get("sources", {}).get("liceu", {}) or {}).get("pdf_hash")
                old_hashes["gimnaziu"] = (old.get("sources", {}).get("gimnaziu", {}) or {}).get("pdf_hash")
        except Exception:
            pass

    sources_out = {}
    combined_schedule = {}

    changed_any = False
    tmp_files = []

    def download_and_parse(name: str, url: str | None):
        nonlocal changed_any, combined_schedule

        if not url:
            print(f"[{name}] no url")
            return

        print(f"[{name}] fetching: {url}")
        resp = requests.get(url, headers=HEADERS, timeout=60)
        resp.raise_for_status()

        tmp = f"temp_{name}.pdf"
        with open(tmp, "wb") as f:
            f.write(resp.content)
        tmp_files.append(tmp)

        h = file_hash(tmp)
        sources_out[name] = {"source_pdf": url, "pdf_hash": h}

        if old_hashes.get(name) == h:
            print(f"[{name}] PDF unchanged (hash match).")
            return

        print(f"[{name}] PDF changed -> parsing...")
        sched = parse_pdf(tmp)
        print(f"[{name}] parsed classes:", len(sched))

        merge_schedules(combined_schedule, sched)
        changed_any = True

    download_and_parse("liceu", liceu_url)
    download_and_parse("gimnaziu", gim_url)

    # cleanup temp files
    for p in tmp_files:
        try:
            os.remove(p)
        except OSError:
            pass

    # dacă nu s-a schimbat nimic, ieșim fără să rescriem json
    if not changed_any:
        print("No changes detected in any PDF. Skipping update.")
        return

    out = {
        "updated_at": datetime.now(RO_TZ).strftime("%d.%m.%Y %H:%M"),
        "sources": sources_out,
        "schedule": combined_schedule,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print("Updated timetable.json | classes:", len(combined_schedule))

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
