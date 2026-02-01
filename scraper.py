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

# 5A..12D (acceptă și "7B cab. desen", "8D lab. bio", etc.)
CLASS_CODE_RE = re.compile(r"\b([5-9]|1[0-2])[A-D]\b", re.IGNORECASE)

# artefacte de tip "10C,D" / "11B,C" etc (merged cells)
CLASS_GROUP_RE = re.compile(
    r"^\s*-?\s*(?:[5-9]|1[0-2])[A-D](?:\s*[,/]\s*(?:[5-9]|1[0-2])[A-D])+\s*$",
    re.IGNORECASE
)

def get_latest_pdf_url(match_fn):
    html = requests.get(URL, headers=HEADERS, timeout=30).text
    pdfs = re.findall(r'href="([^"]+\.pdf)"', html, flags=re.IGNORECASE)
    if not pdfs:
        return None

    cand = []
    for href in pdfs:
        href_l = href.lower()
        if match_fn(href_l):
            cand.append(urljoin(URL, href))

    if not cand:
        return None

    def score(u: str):
        nums = re.findall(r"\d+", u)
        return [int(n) for n in nums] if nums else [0]

    cand.sort(key=score, reverse=True)
    return cand[0]

def file_hash(path):
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

def is_time_slot(s):
    s = (s or "").strip()
    return bool(TIME_RE.match(s))

def find_day_zones(page):
    words = page.extract_words(x_tolerance=2, y_tolerance=2)
    zones = []
    for w in words:
        t = w["text"].upper()
        if t in DAY_MARKERS:
            zones.append({"day": DAY_MARKERS[t], "top": w["top"], "bottom": w["bottom"]})
    zones.sort(key=lambda z: z["top"])
    return zones

def v_edge_len(e):
    top = e.get("top", e.get("y0", 0))
    bottom = e.get("bottom", e.get("y1", 0))
    return abs(bottom - top)

def h_edge_len(e):
    x0 = e.get("x0", 0)
    x1 = e.get("x1", 0)
    return abs(x1 - x0)

def h_edge_y(e):
    return e.get("top", e.get("y0", 0))

def get_global_x_bounds(page):
    # IMPORTANT: ignorăm verticale scurte (artefacte din antete "lab/cab")
    verts = [
        e for e in page.edges
        if e.get("orientation") == "v" and v_edge_len(e) > (page.height * 0.35)
    ]
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

    return x_bounds

def get_y_bounds_for_crop(page_crop):
    # IMPORTANT: ignorăm orizontale scurte (altfel îți sparg rândurile la antete cu "lab/cab")
    horiz = [
        e for e in page_crop.edges
        if e.get("orientation") == "h" and h_edge_len(e) > (page_crop.width * 0.60)
    ]
    ys = [h_edge_y(e) for e in horiz]
    y_bounds = sorted(cluster_positions(ys, tol=1.5))

    cleaned = []
    for y in y_bounds:
        if not cleaned or abs(y - cleaned[-1]) > 1.0:
            cleaned.append(y)
    return cleaned

def extract_class_code(s: str):
    if not s:
        return None
    s = re.sub(r"\s+", " ", s).strip()
    m = CLASS_CODE_RE.search(s)
    return m.group(0).upper() if m else None

def normalize_subject(subj: str) -> str:
    subj = (subj or "").strip()
    subj = re.sub(r"\s+", " ", subj)

    # curățăm leading junk (/, -, etc.)
    subj = re.sub(r"^[^A-Za-z0-9ĂÂÎȘȚăâîșț]+", "", subj).strip()

    if not subj:
        return ""

    # dacă e doar o literă (artefact)
    if re.fullmatch(r"[a-zA-Z]", subj):
        return ""

    # dacă e label de clasă / grup de clase (merged-cells)
    if extract_class_code(subj) and len(subj) <= 3:
        return ""
    if CLASS_GROUP_RE.fullmatch(subj):
        return ""

    # unele PDF-uri bagă "H9ABCD" / etc în celule merged
    if re.fullmatch(r"H(?:[5-9]|1[0-2])[A-D]{2,}", subj, flags=re.IGNORECASE):
        return ""

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

    # Detectăm header row după câte coduri de clase găsim în rând
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

    if header_r is None or best_hits < 8:
        return {}

    # Mapăm coloanele după codul de clasă extras din antet (merge și cu "8D lab. bio")
    col_to_class = {}
    seen = set()
    for c in range(1, n_cols):
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

def parse_pdf(pdf_path):
    final = {}
    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[0]
        x_bounds = get_global_x_bounds(page)

        zones = find_day_zones(page)
        if not zones:
            raise RuntimeError("Could not find day headers (MONTAG/DIENSTAG/...).")

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

def merge_schedules(a: dict, b: dict) -> dict:
    out = {}
    for src in (a, b):
        for cls, days in src.items():
            out.setdefault(cls, {})
            for day, entries in days.items():
                out[cls].setdefault(day, [])
                for e in entries:
                    if e not in out[cls][day]:
                        out[cls][day].append(e)
    return out

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
    liceu_url = get_latest_pdf_url(lambda s: "liceu" in s)
    gim_url = get_latest_pdf_url(lambda s: "gimnaziu" in s or "gimn" in s)

    if not liceu_url or not gim_url:
        print("Could not find both PDFs on site.")
        print("liceu:", liceu_url)
        print("gimnaziu:", gim_url)
        return

    # download both
    tmp_liceu = "temp_liceu.pdf"
    tmp_gim = "temp_gimnaziu.pdf"

    r1 = requests.get(liceu_url, headers=HEADERS, timeout=60)
    r1.raise_for_status()
    with open(tmp_liceu, "wb") as f:
        f.write(r1.content)

    r2 = requests.get(gim_url, headers=HEADERS, timeout=60)
    r2.raise_for_status()
    with open(tmp_gim, "wb") as f:
        f.write(r2.content)

    liceu_hash = file_hash(tmp_liceu)
    gim_hash = file_hash(tmp_gim)

    old_liceu_hash = None
    old_gim_hash = None
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                old = json.load(f)
                old_liceu_hash = old.get("sources", {}).get("liceu", {}).get("pdf_hash")
                old_gim_hash = old.get("sources", {}).get("gimnaziu", {}).get("pdf_hash")
        except Exception:
            pass

    if old_liceu_hash == liceu_hash and old_gim_hash == gim_hash:
        print("Both PDFs unchanged, skipping update.")
        for p in (tmp_liceu, tmp_gim):
            try:
                os.remove(p)
            except OSError:
                pass
        return

    liceu_sched = parse_pdf(tmp_liceu)
    gim_sched = parse_pdf(tmp_gim)
    schedule = merge_schedules(liceu_sched, gim_sched)

    out = {
        "updated_at": datetime.now(RO_TZ).strftime("%d.%m.%Y %H:%M"),
        "sources": {
            "liceu": {"source_pdf": liceu_url, "pdf_hash": liceu_hash},
            "gimnaziu": {"source_pdf": gim_url, "pdf_hash": gim_hash},
        },
        "schedule": schedule,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    for p in (tmp_liceu, tmp_gim):
        try:
            os.remove(p)
        except OSError:
            pass

    print("Updated timetable.json | classes:", len(schedule))

    changed = {
        "liceu_changed": old_liceu_hash != liceu_hash,
        "gimnaziu_changed": old_gim_hash != gim_hash,
    }

    notify_worker(
        title="Schedule updated",
        body="A new timetable PDF was detected. Open the app to refresh.",
        data={
            "updated_at": out["updated_at"],
            "sources": out["sources"],
            **changed,
        },
    )

if __name__ == "__main__":
    main()
