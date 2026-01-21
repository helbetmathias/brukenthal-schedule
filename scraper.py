import pdfplumber
import requests
import json
import re
from datetime import datetime

URL = "https://brukenthal.ro/"
HEADERS = {"User-Agent": "Mozilla/5.0"}
OUTPUT_FILE = "timetable.json"

COLUMNS_ORDER = [
    "Time",
    "9A", "9B", "9C", "9D",
    "10A", "10B", "10C", "10D",
    "11A", "11B", "11C", "11D",
    "12A", "12B", "12C", "12D",
]

DAY_MARKERS = {
    "MONTAG": "Luni",
    "DIENSTAG": "Marti",
    "MITTWOCH": "Miercuri",
    "DONNERSTAG": "Joi",
    "FREITAG": "Vineri",
}

TIME_RE = re.compile(r"^\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}$")

def get_latest_pdf_url():
    html = requests.get(URL, headers=HEADERS, timeout=30).text
    m = re.search(r'href="([^"]*orarliceu[^"]*\.pdf)"', html, re.IGNORECASE)
    if not m:
        return None
    href = m.group(1)
    # handle relative href
    if href.startswith("/"):
        return URL.rstrip("/") + href
    if href.lower().startswith("http"):
        return href
    return URL.rstrip("/") + "/" + href.lstrip("/")

def cluster_positions(values, tol=1.5):
    """Cluster numeric positions that are within `tol`."""
    values = sorted(values)
    clusters = []
    for v in values:
        if not clusters or abs(v - clusters[-1][-1]) > tol:
            clusters.append([v])
        else:
            clusters[-1].append(v)
    return [sum(c) / len(c) for c in clusters]

def overlap(a0, a1, b0, b1):
    """1D overlap length."""
    return max(0.0, min(a1, b1) - max(a0, b0))

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

def get_global_x_bounds(page):
    # In this PDF, grid is mostly rect edges -> page.edges has them.
    verts = [e for e in page.edges if e["orientation"] == "v"]
    xs = [e["x0"] for e in verts]
    x_bounds = cluster_positions(xs, tol=1.5)

    # Expect 18 vertical boundaries (Time + 16 classes => 17 cols => 18 borders)
    # If extra noise exists, keep the most plausible set: the widest span.
    x_bounds = sorted(x_bounds)
    if len(x_bounds) > 18:
        # keep 18 that maximize width (simple heuristic)
        best = None
        for i in range(0, len(x_bounds) - 17):
            cand = x_bounds[i:i+18]
            width = cand[-1] - cand[0]
            if best is None or width > best[0]:
                best = (width, cand)
        x_bounds = best[1]
    return x_bounds

def get_y_bounds_for_crop(page_crop):
    horiz = [e for e in page_crop.edges if e["orientation"] == "h"]
    ys = [e["y0"] for e in horiz]
    y_bounds = sorted(cluster_positions(ys, tol=1.5))

    # Filter out extremely close duplicates (just in case)
    cleaned = []
    for y in y_bounds:
        if not cleaned or abs(y - cleaned[-1]) > 1.0:
            cleaned.append(y)
    return cleaned

def build_cell_word_map(words, x_bounds, y_bounds, min_x_overlap_ratio=0.30, min_y_overlap_ratio=0.40):
    """
    Map each word into (row_idx, col_idx) cells by overlap with column and row intervals.
    If a word spans across multiple columns, it will be assigned to ALL overlapping cols.
    """
    # intervals
    col_intervals = [(x_bounds[i], x_bounds[i+1]) for i in range(len(x_bounds)-1)]
    row_intervals = [(y_bounds[i], y_bounds[i+1]) for i in range(len(y_bounds)-1)]

    cells = {}  # (r,c) -> list of (x0, text) for ordering

    for w in words:
        wx0, wx1 = w["x0"], w["x1"]
        wy0, wy1 = w["top"], w["bottom"]
        ww = max(1e-6, wx1 - wx0)
        wh = max(1e-6, wy1 - wy0)

        # find row(s) by y overlap
        row_hits = []
        for r, (ry0, ry1) in enumerate(row_intervals):
            ov = overlap(wy0, wy1, ry0, ry1)
            if (ov / wh) >= min_y_overlap_ratio:
                row_hits.append(r)

        if not row_hits:
            continue

        # find col(s) by x overlap
        col_hits = []
        for c, (cx0, cx1) in enumerate(col_intervals):
            ov = overlap(wx0, wx1, cx0, cx1)
            if (ov / ww) >= min_x_overlap_ratio:
                col_hits.append(c)

        if not col_hits:
            continue

        for r in row_hits:
            for c in col_hits:
                cells.setdefault((r, c), []).append((wx0, w["text"]))

    # sort word fragments left-to-right inside each cell
    for k in list(cells.keys()):
        cells[k].sort(key=lambda t: t[0])

    return cells

def parse_day_block(day_crop, x_bounds):
    y_bounds = get_y_bounds_for_crop(day_crop)
    if len(y_bounds) < 5:
        return {}  # nothing useful

    # Extract words from crop
    words = day_crop.extract_words(x_tolerance=1, y_tolerance=1, keep_blank_chars=False)

    cell_words = build_cell_word_map(words, x_bounds, y_bounds)

    n_rows = len(y_bounds) - 1
    n_cols = len(x_bounds) - 1

    # Build text grid
    grid = [["" for _ in range(n_cols)] for _ in range(n_rows)]
    for (r, c), parts in cell_words.items():
        txt = " ".join(t for _, t in parts).strip()
        # Normalize multiple spaces
        txt = re.sub(r"\s+", " ", txt)
        grid[r][c] = txt

    # Find header row containing 9A..12D
    header_r = None
    needed = {"9A", "9B", "12D"}
    for r in range(min(6, n_rows)):  # header is near top
        row_text = " ".join(grid[r]).replace(" ", "")
        if all(k in row_text for k in needed):
            header_r = r
            break

    if header_r is None:
        # fallback: first row where many class labels appear
        best = (-1, None)
        for r in range(min(8, n_rows)):
            score = sum(1 for lab in COLUMNS_ORDER[1:] if lab in grid[r][1:])
            if score > best[0]:
                best = (score, r)
        header_r = best[1]

    if header_r is None:
        return {}

    # Build mapping col index -> class name (assume fixed order)
    # col 0 is Time, col 1..16 map to classes
    col_to_class = {}
    for c in range(1, min(17, n_cols)):
        col_to_class[c] = COLUMNS_ORDER[c]

    # Parse rows below header
    day_schedule = {cls: [] for cls in COLUMNS_ORDER[1:]}
    for r in range(header_r + 1, n_rows):
        time_txt = grid[r][0].strip()
        if not is_time_slot(time_txt):
            continue

        for c, cls in col_to_class.items():
            subj = grid[r][c].strip()
            if not subj:
                continue
            # avoid accidentally carrying header labels
            if subj in COLUMNS_ORDER:
                continue
            day_schedule[cls].append(f"{time_txt} | {subj}")

    # remove empties
    day_schedule = {k: v for k, v in day_schedule.items() if v}
    return day_schedule

def parse_pdf(pdf_path):
    final = {}

    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[0]

        # Global x bounds (same for all days)
        x_bounds = get_global_x_bounds(page)

        # Day zones by header text
        zones = find_day_zones(page)
        if not zones:
            raise RuntimeError("Could not find day headers (MONTAG/DIENSTAG/...).")

        for i, z in enumerate(zones):
            day_name = z["day"]
            y_start = max(0, z["top"] - 8)  # small margin above title
            y_end = zones[i+1]["top"] - 6 if i + 1 < len(zones) else page.height

            crop = page.crop((0, y_start, page.width, y_end))
            day_block = parse_day_block(crop, x_bounds)

            # day_block is {class: [entries...]} so merge into final
            for cls, entries in day_block.items():
                final.setdefault(cls, {})
                final[cls].setdefault(day_name, [])
                # keep order, avoid duplicates
                for e in entries:
                    if e not in final[cls][day_name]:
                        final[cls][day_name].append(e)

    return final

def main():
    pdf_url = get_latest_pdf_url()
    if not pdf_url:
        print("No PDF link found on site.")
        return

    pdf_data = requests.get(pdf_url, headers=HEADERS, timeout=60).content
    tmp = "temp.pdf"
    with open(tmp, "wb") as f:
        f.write(pdf_data)

    schedule = parse_pdf(tmp)
    out = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "schedule": schedule
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print("OK:", OUTPUT_FILE)

if __name__ == "__main__":
    main()
