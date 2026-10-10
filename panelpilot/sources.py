"""Deterministic text extraction for every source document.

Writes sources/text/<doc_id>.txt (the text rules quote from), sources/sections.jsonl (the units the
extractor sees) and sources/MANIFEST.csv (raw + text hashes; rules.py refuses text whose hash drifts).

Same raw files in -> byte-identical text out. The only cleaning applied, all mechanical:
  * PDF control characters dropped (e.g. \\x03 before "(minimum age")
  * the CDC PDF's Wingdings bullet, extracted as "\\ty ", becomes "• "
  * HTML: scripts/styles/nav removed, text joined with newlines
"""
import csv
import hashlib
import json
import pathlib
import re

import pymupdf
from bs4 import BeautifulSoup

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "sources"

DOCS = [
    # doc_id, file, source_url, retrieved
    ("assignment", "assignment.pdf", "PanelPilot-Assignment.pdf (provided by Independence OS)", "2026-10-09"),
    ("uspstf_ab", "uspstf_a_and_b.html",
     "https://www.uspreventiveservicestaskforce.org/uspstf/recommendation-topics/uspstf-a-and-b-recommendations", "2026-10-09"),
    ("cdc_child", "cdc_child_schedule.pdf",
     "https://web.archive.org/web/20260930211047/https://www.cdc.gov/vaccines/hcp/imz-schedules/downloads/child/"
     "0-18yrs-child-combined-schedule.pdf (cdc.gov returns 403 to scripted clients; document is the 2025 schedule)",
     "2026-10-09"),
    ("bright_futures", "bright_futures_periodicity.pdf", "https://downloads.aap.org/AAP/PDF/periodicity_schedule.pdf", "2026-10-09"),
    ("uspstf_crc_2016", "uspstf_crc_2016.html",
     "https://web.archive.org/web/20200220215119/https://www.uspreventiveservicestaskforce.org/Page/Document/"
     "RecommendationStatementFinal/colorectal-cancer-screening2 (2016 statement, superseded 2021)", "2026-10-09"),
    ("uspstf_crc_2021", "uspstf_crc_2021.html",
     "https://www.uspreventiveservicestaskforce.org/uspstf/recommendation/colorectal-cancer-screening", "2026-10-09"),
]

# Vaccine note headings in the CDC notes pages, in document order. Fixed list = auditable split.
CDC_HEADINGS = [
    "COVID-19 vaccination", "Dengue vaccination", "Diphtheria, tetanus, and pertussis (DTaP)",
    "Haemophilus influenzae type b vaccination", "Hepatitis A vaccination", "Hepatitis B vaccination",
    "Human papillomavirus vaccination", "Influenza vaccination", "Measles, mumps, and rubella vaccination",
    "Meningococcal serogroup A,C,W,Y vaccination", "Meningococcal serogroup B vaccination", "Mpox vaccination",
    "Pneumococcal vaccination", "Poliovirus vaccination", "Respiratory syncytial virus immunization",
    "Rotavirus vaccination", "Tetanus, diphtheria, and pertussis (Tdap)", "Varicella vaccination",
]
CDC_NOTES_PAGES = range(4, 15)  # 0-based: printed pages 5-15


def clean(text: str) -> str:
    text = text.replace("\ty ", "• ")
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)


def pdf_pages(path):
    return [clean(p.get_text()) for p in pymupdf.open(path)]


def html_soup(path):
    s = BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"), "html.parser")
    for t in s(["script", "style", "nav", "header", "footer", "noscript"]):
        t.decompose()
    return s


def cell(td):
    return re.sub(r"[ \t]+", " ", td.get_text(" ", strip=True))


def uspstf_ab(path):
    s = html_soup(path)
    blocks = []
    for tr in s.select("table tr")[1:]:
        tds = tr.find_all("td")
        if len(tds) < 4:
            continue
        topic, desc, grade, date = (cell(t) for t in tds[:4])
        blocks.append((topic, f"TOPIC: {topic}\nDESCRIPTION: {desc}\nGRADE: {grade}\nDATE: {date}"))
    return blocks


def crc_page(path):
    """Full page text (for quoting) + sections: each recommendation-summary row and the screening-methods table."""
    s = html_soup(path)
    full = clean("\n".join(line for line in s.get_text("\n", strip=True).splitlines() if line.strip()))
    sections = []
    tables = s.find_all("table")
    summary = next(t for t in tables if t.get_text().strip().startswith("Population"))
    for tr in summary.find_all("tr")[1:]:
        tds = tr.find_all("td")
        if len(tds) >= 3:
            pop = cell(tds[0])
            sections.append((pop, f"POPULATION: {pop}\nRECOMMENDATION: {cell(tds[1])}\nGRADE: {cell(tds[2])}"))
    methods = next((t for t in tables if t.get_text(" ", strip=True).lower().startswith("screening method")), None)
    if methods is not None:
        rows = "\n".join(" | ".join(cell(td) for td in tr.find_all(["td", "th"])) for tr in methods.find_all("tr"))
        # test intervals ("Every 10 y") live in the methods table, not the population row: give the extractor both,
        # so a complete rule can be quoted from one extraction unit
        sections = [(title, f"{body}\n\nSCREENING METHODS TABLE:\n{rows}") for title, body in sections]
    return full, sections


def cdc_sections(pages):
    out = [(f"page {i + 1}", p) for i, p in enumerate(pages)]
    notes = "".join(pages[i] for i in CDC_NOTES_PAGES)
    starts, pos = [], 0
    for h in CDC_HEADINGS:
        m = re.compile(r"(^|\n)" + re.escape(h)).search(notes, pos)
        if m is None:
            raise SystemExit(f"CDC heading not found: {h}")
        starts.append((h, m.start() + len(m.group(1))))
        pos = m.end()
    for (h, a), (_, b) in zip(starts, starts[1:] + [("", len(notes))]):
        out.append((f"notes: {h}", notes[a:b].strip()))
    return out


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def main():
    (SRC / "text").mkdir(exist_ok=True)
    sections, manifest = [], []
    for doc_id, fname, url, retrieved in DOCS:
        path = SRC / fname
        if doc_id == "uspstf_ab":
            blocks = uspstf_ab(path)
            text = "\n\n".join(b for _, b in blocks)
            secs = blocks
        elif doc_id.startswith("uspstf_crc"):
            text, secs = crc_page(path)
        else:
            pages = pdf_pages(path)
            text = "\n".join(pages)
            if doc_id == "cdc_child":
                secs = cdc_sections(pages)
            elif doc_id == "bright_futures":
                secs = [(f"page {i + 1}", p) for i, p in enumerate(pages)]
            else:
                secs = []  # the assignment is the calibration spec, hand-encoded, never sent to the extractor
        tb = text.encode("utf-8")
        (SRC / "text" / f"{doc_id}.txt").write_bytes(tb)
        manifest.append([doc_id, fname, url, retrieved, sha(path.read_bytes()), sha(tb)])
        for i, (title, body) in enumerate(secs):
            sections.append({"doc_id": doc_id, "section_id": f"{doc_id}:{i:03d}", "title": title, "text": body})

    with open(SRC / "MANIFEST.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["doc_id", "file", "source_url", "retrieved", "raw_sha256", "text_sha256"])
        w.writerows(manifest)
    with open(SRC / "sections.jsonl", "w") as f:
        for s in sections:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")

    counts = {}
    for s in sections:
        c = counts.setdefault(s["doc_id"], [0, 0])
        c[0] += 1
        c[1] += len(s["text"])
    for d, (n, ch) in counts.items():
        print(f"{d:18s} sections={n:3d} chars={ch}")
    print(f"total sections={len(sections)} chars={sum(c[1] for c in counts.values())}")


if __name__ == "__main__":
    main()
