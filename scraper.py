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
URL = "https://brukenthal.ro/"
HEADERS = {"User-Agent": "Mozilla/5.0"}
OUTPUT_FILE = "timetable.json"

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


COLUMNS_BY_LEVEL = {
    "liceu": [
        "Time",
        "9A", "9B", "9C", "9D",
        "10A", "10B", "10C", "10D",
        "11A", "11B", "11C", "11D",
        "12A", "12B", "12C", "12D",
    ],
    "gimnaziu": [
        "Time",
        "5A", "5B", "5C", "5D",
        "6A", "6B", "6C", "6D",
        "7A", "7B", "7C", "7D",
        "8A", "8B", "8C", "8D",
    ],
}


def normalize_time_range(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s*-\s*", "-", s)
    return s


def is_time_slot(s: str) -> bool:
    return bool(TIME_RE.match((s or "").strip()))


def file_hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def score_url(u: str):
    nums = re.findall(r"\d+", u)
    return [int(n) for n in nums] if nums else [0]


def extract_pdf_urls_from_site():
    html = requests.get(URL, headers=HEADERS, timeout=30).text
    hrefs = re.findall(r'href="([^"]+\.pdf)"', html, flags=re.IGNORECASE)
    urls = [urljoin(URL, h) for h in hrefs]
    return list(dict.fromkeys(urls))


def get_latest_pdf_urls():
    pdf_urls = extract_pdf_urls_from_site()
    if not pdf_urls:
        return {"liceu": None, "gimnaziu": None}

    def pick(pattern: str):
        cand = [u for u in pdf_urls if re.search(pattern, u, flags=re.IGNORECASE)]
        if not cand:
            return None
        cand.sort(key=score_url, reverse=True)
        return cand[0]

    return {
        "liceu": pick(r"liceu"),
        "gimnaziu": pick(r"gimnaz"),
    }


def cluster_positions(values, tol=1.5):
    values = sorted(values)
    clusters = []
    for v in values:
        if not clusters or abs(v - clusters[-1][-1]) > tol:
            clusters.append([v])
        else:
            clusters[-1].append(v)
    return [sum(c) / len(c) for c in clusters]


def find_day_zones(page):
    words = page.extract_words(x_tolerance=2, y_tolerance=2)
    zones = []
    for w in words:
        t = w["text"].upper()
        if t in DAY_MARKERS:
            zones.append({"day": DAY_MARKERS[t], "top": w["top"], "bottom": w["bottom"]})
    zones.sort(key=lambda z: z["top"])
    return zones


def get_global_x_bounds(page):
    verts = [e for e in page.edges if e.get("orientation") == "v"]
    xs = [e["x0"] for e in verts]
    x_bounds = sorted(cluster_positions(xs, tol=1.5))

    # 18 bounds => 17 coloane (Time + 16 clase)
    if len(x_bounds) > 18:
        best = None
        for i in range(0, len(x_bounds) - 17):
            cand = x_bounds[i:i + 18]
            width = cand[-1] - cand[0]
            if best is None or width > best[0]:
                best = (width, cand)
        x_bounds = best[1]

    return x_bounds


def get_y_bounds_for_crop(page_crop):
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

    if re.fullmatch(r"[a-z]", subj):
        return ""

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


def parse_day_block(day_crop, x_bounds, columns_order):
    y_bounds = get_y_bounds_for_crop(day_crop)
    if len(y_bounds) < 5:
        return {}

    chars = day_crop.chars
    n_rows = len(y_bounds) - 1
    n_cols = len(x_bounds) - 1

    grid = [["" for _ in range(n_cols)] for _ in range(n_rows)]

    # IMPORTANT: padding diferit pt Time vs coloanele de clase
    for r in range(n_rows):
        ry0, ry1 = y_bounds[r], y_bounds[r + 1]
        for c in range(n_cols):
            cx0, cx1 = x_bounds[c], x_bounds[c + 1]
            if c == 0:
                # Time column: padding mic (vrem să prindă bine ora)
                grid[r][c] = cell_text_from_chars(chars, cx0, cx1, ry0, ry1, x_pad_left=0.6, x_pad_right=0.6)
            else:
                # Class columns: padding mai mare stânga ca să nu “prindă” ora din stânga
                grid[r][c] = cell_text_from_chars(chars, cx0, cx1, ry0, ry1, x_pad_left=3.0, x_pad_right=0.6)

    # detect header row
    header_r = None
    best_score = -1
    for r in range(min(10, n_rows)):
        score = sum(1 for lab in columns_order[1:] if lab in grid[r])
        if score > best_score:
            best_score = score
            header_r = r

    if header_r is None or best_score < 5:
        return {}

    max_class_cols = min(len(columns_order) - 1, n_cols - 1)
    col_to_class = {c: columns_order[c] for c in range(1, max_class_cols + 1)}

    day_schedule = {cls: [] for cls in columns_order[1:]}

    for r in range(header_r + 1, n_rows):
        time_txt = normalize_time_range((grid[r][0] or "").strip())
        if not is_time_slot(time_txt):
            continue

        for c, cls in col_to_class.items():
            subj_raw = normalize_subject(grid[r][c])

            # FIX 1: dacă în “materie” ai de fapt intervalul orar, ignoră-l
            if not subj_raw:
                continue
            subj_norm = normalize_time_range(subj_raw)

            # ignorăm exact intervalele orare (artefactul tău)
            if is_time_slot(subj_norm):
                continue

            # FIX 2: ignoră dacă “materia” e identică cu ora rândului
            if subj_norm.replace(" ", "") == time_txt.replace(" ", ""):
                continue

            # sanity: nu lăsa să fie chiar numele unei clase
            if subj_norm in columns_order:
                continue

            day_schedule[cls].append(f"{time_txt} | {subj_norm}")

    return {k: v for k, v in day_schedule.items() if v}


def schedule_entry_count(schedule: dict) -> int:
    total = 0
    for cls_map in schedule.values():
        for entries in cls_map.values():
            total += len(entries)
    return total


def parse_page(page, columns_order):
    final = {}
    x_bounds = get_global_x_bounds(page)

    zones = find_day_zones(page)
    if not zones:
        return {}

    for i, z in enumerate(zones):
        day_name = z["day"]
        y_start = max(0, z["top"] - 8)
        y_end = zones[i + 1]["top"] - 6 if i + 1 < len(zones) else page.height

        crop = page.crop((0, y_start, page.width, y_end))
        day_block = parse_day_block(crop, x_bounds, columns_order)

        for cls, entries in day_block.items():
            final.setdefault(cls, {})
            final[cls].setdefault(day_name, [])
            for e in entries:
                if e not in final[cls][day_name]:
                    final[cls][day_name].append(e)

    return final


def parse_pdf_best_page(pdf_path, columns_order):
    best = {}
    best_cnt = -1
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            sched = parse_page(page, columns_order)
            cnt = schedule_entry_count(sched)
            if cnt > best_cnt:
                best_cnt = cnt
                best = sched
    return best if best_cnt > 0 else {}


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
    latest = get_latest_pdf_urls()

    liceu_url = latest.get("liceu")
    gimn_url = latest.get("gimnaziu")

    if not liceu_url and not gimn_url:
        print("No liceu/gimnaziu PDF link found on site.")
        return

    pdf_urls = [u for u in [liceu_url, gimn_url] if u]
    tmp_files = []

    # load old
    old = {}
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                old = json.load(f) or {}
        except Exception:
            old = {}

    old_urls = old.get("source_pdf") or []
    old_hashes = old.get("pdf_hash") or []

    new_hashes = []
    schedules = []

    for url in pdf_urls:
        resp = requests.get(url, headers=HEADERS, timeout=60)
        resp.raise_for_status()

        safe = hashlib.md5(url.encode("utf-8")).hexdigest()[:10]
        tmp = f"temp_{safe}.pdf"
        with open(tmp, "wb") as f:
            f.write(resp.content)
        tmp_files.append(tmp)

        h = file_hash(tmp)
        new_hashes.append(h)

        level = "liceu" if "liceu" in url.lower() else "gimnaziu"
        cols = COLUMNS_BY_LEVEL[level]
        sched = parse_pdf_best_page(tmp, cols)
        schedules.append(sched)

    # cleanup temp
    for t in tmp_files:
        try:
            os.remove(t)
        except OSError:
            pass

    # skip if unchanged
    if old_urls == pdf_urls and old_hashes == new_hashes:
        print("PDFs unchanged, skipping update.")
        return

    # merge schedules (liceu + gimnaziu într-un singur dict, ca la tine)
    merged = {}
    for sched in schedules:
        for cls, daymap in sched.items():
            if cls not in merged:
                merged[cls] = daymap
            else:
                # dacă există deja (teoretic nu), combinăm
                for day, entries in daymap.items():
                    merged[cls].setdefault(day, [])
                    for e in entries:
                        if e not in merged[cls][day]:
                            merged[cls][day].append(e)

    out = {
        "updated_at": datetime.now(RO_TZ).strftime("%d.%m.%Y %H:%M"),
        "source_pdf": pdf_urls,
        "pdf_hash": new_hashes,
        "schedule": merged,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print("Updated timetable.json | classes:", len(merged))

    notify_worker(
        title="Schedule updated",
        body="A new timetable PDF was detected. Open the app to refresh.",
        data={"updated_at": out["updated_at"], "source_pdf": out["source_pdf"], "pdf_hash": out["pdf_hash"]},
    )


if __name__ == "__main__":
    main()
