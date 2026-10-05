"""Build the report and scientific figures offline from exact published evidence.

Document-only dependencies are separate from the frozen application's lock.
No network, model dispatch, production mutation or changes to study artifacts.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import random
import re
import statistics
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, Rectangle
from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Image,
    KeepTogether,
    PageBreak,
    Paragraph,
    Preformatted,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

ROOT = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent
BLUE, GOLD, INK, GREY = "#205A83", "#AD650D", "#243344", "#687782"
EXPECTED = {
    "challenge_cases.jsonl": "2ab256dc99112b6864f9c41f51f77d216ebff8aeccfa6458c30107f54787e7f5",
    "challenge_answers.jsonl": "095b5c18f7a8d918601c0093cb30528b6d3d2ba9ecd4a942a5d6defa8ab71968",
    "challenge_review.json": "85b94e4715b2135300b912825be136fadfde6825940f4d12b91c0d3df6c136e0",
    "challenge_summary.json": "037fd237957a498286d0f76bc4b86ed76309c9f9d848d443e5c1da213f5604c8",
}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def jsonl(path):
    return [json.loads(x) for x in path.read_text(encoding="utf8").splitlines()]


def evidence():
    for name, expected in EXPECTED.items():
        if sha(ROOT / "course" / name) != expected:
            raise ValueError("Published evidence byte mismatch: " + name)
    cases = jsonl(ROOT / "course/challenge_cases.jsonl")
    answers = jsonl(ROOT / "course/challenge_answers.jsonl")
    review = json.loads((ROOT / "course/challenge_review.json").read_text(encoding="utf8"))
    summary = json.loads((ROOT / "course/challenge_summary.json").read_text(encoding="utf8"))
    lookup = {(e["case_id"], e["arm"]): e for e in review["entries"]}
    case_lookup = {c["id"]: c for c in cases}
    assert len(cases) == 60 and len(case_lookup) == 60
    assert len(answers) == len(lookup) == len(review["entries"]) == 120
    assert set(lookup) == {(a["case_id"], a["arm"]) for a in answers}
    assert summary["human_audits"] == "pending" and not summary["submission_ready"]
    for arm in ("system", "baseline"):
        rows = [a for a in answers if a["arm"] == arm]
        verdicts = [lookup[a["case_id"], arm] for a in rows]
        assert len(rows) == 60
        assert sum(v["pass"] for v in verdicts) == summary["arms"][arm]["passed"]
        assert dict(Counter(v["outcome"] for v in verdicts)) == summary["arms"][arm]["outcomes"]
        assert (
            statistics.median(a["latency_s"] for a in rows)
            == summary["arms"][arm]["median_latency_s"]
        )
        for family, value in summary["arms"][arm]["families"].items():
            family_rows = [a for a in rows if case_lookup[a["case_id"]]["family"] == family]
            assert len(family_rows) == value["attempted"]
            assert sum(lookup[a["case_id"], arm]["pass"] for a in family_rows) == value["passed"]
    counts = Counter()
    groups = defaultdict(list)
    for case in cases:
        s, b = (lookup[case["id"], arm]["pass"] for arm in ("system", "baseline"))
        counts[(s, b)] += 1
        groups[case["cluster"]].append(int(s) - int(b))
    paired = summary["paired"]
    assert [counts[True, True], counts[True, False], counts[False, True], counts[False, False]] == [
        30,
        11,
        14,
        5,
    ]
    assert len(groups) == paired["clusters"] == 40
    assert (counts[True, False] - counts[False, True]) / 60 == paired["difference"]
    rng = random.Random(4136)
    clusters = list(groups.values())
    draws = []
    for _ in range(10000):
        sample = [rng.choice(clusters) for _ in clusters]
        draws.append(sum(sum(g) for g in sample) / sum(map(len, sample)))
    draws.sort()
    assert [draws[249], draws[9749]] == paired["cluster_bootstrap_95pct"]
    assert (summary["arms"]["system"]["passed"], summary["arms"]["baseline"]["passed"]) == (41, 44)
    return summary


def save_figure(fig, name):
    directory = HERE / "figures"
    directory.mkdir(exist_ok=True)
    fig.savefig(directory / (name + ".png"), dpi=300, facecolor="white")
    fig.savefig(directory / (name + ".svg"), facecolor="white", metadata={"Date": None})
    plt.close(fig)


def figures(summary):
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 12,
            "axes.labelcolor": INK,
            "text.color": INK,
            "axes.edgecolor": GREY,
            "svg.fonttype": "none",
        }
    )
    fig, ax = plt.subplots(figsize=(10.8, 4.05))
    ax.set(xlim=(0, 10.8), ylim=(0, 4.05))
    ax.axis("off")

    def box(x, y, w, h, title, detail, face="#F4F7FA", edge=BLUE):
        ax.add_patch(Rectangle((x, y), w, h, facecolor=face, edgecolor=edge, lw=1))
        ax.text(
            x + w / 2,
            y + h * 0.68,
            title,
            ha="center",
            va="center",
            fontsize=12.3,
            fontweight="bold",
        )
        ax.text(x + w / 2, y + h * 0.27, detail, ha="center", va="center", fontsize=10.7)

    def arrow(x1, y1, x2, y2, dashed=False):
        ax.add_patch(
            FancyArrowPatch(
                (x1, y1),
                (x2, y2),
                arrowstyle="-|>",
                mutation_scale=10,
                lw=0.9,
                color=GREY,
                linestyle="--" if dashed else "-",
            )
        )

    box(
        0.05,
        3.16,
        5.05,
        0.65,
        "Frozen release: 4,659 film metadata records",
        "Shared universe; gold stays outside inference",
    )
    box(
        5.65,
        3.16,
        5.1,
        0.65,
        "Production also has 5 PDFs / 21 passages",
        "Available, but outside this metadata evaluation",
        face="#FAFAFA",
        edge=GREY,
    )
    ax.text(0.05, 2.91, "DEPLOYED SYSTEM", fontsize=10, fontweight="bold", color=BLUE)
    box(0.05, 2.07, 2.28, 0.65, "Intent + context", "Domain / title / filters")
    box(2.78, 2.07, 2.28, 0.65, "Evidence selection", "Structured / vector")
    box(5.51, 2.07, 2.28, 0.65, "Answer paths", "Direct facts / Vertex")
    box(8.24, 2.07, 2.51, 0.65, "Validated output", "Citations + movie cards")
    for x1, x2 in [(2.33, 2.78), (5.06, 5.51), (7.79, 8.24)]:
        arrow(x1, 2.395, x2, 2.395)
    arrow(3.92, 3.16, 3.92, 2.74)
    arrow(8.2, 3.16, 7.1, 2.76, True)
    ax.text(0.05, 1.78, "SIMPLE BASELINE", fontsize=10, fontweight="bold", color=GOLD)
    box(0.05, 0.94, 2.28, 0.65, "Own actual history", "Current query + anchors", edge=GOLD)
    box(2.78, 0.94, 2.28, 0.65, "BM25 top eight", "Generic script tokens", edge=GOLD)
    box(5.51, 0.94, 2.28, 0.65, "Vertex reader", "Evidence-only prompt", edge=GOLD)
    box(8.24, 0.94, 2.51, 0.65, "Identity adapter", "Retrieved, marked IDs", edge=GOLD)
    for x1, x2 in [(2.33, 2.78), (5.06, 5.51), (7.79, 8.24)]:
        arrow(x1, 1.265, x2, 1.265)
    ax.text(
        5.4,
        0.39,
        "Same 60 turns  |  separate real histories  |  mechanical checks + explicit AI audit",
        ha="center",
        fontsize=11.4,
    )
    fig.subplots_adjust(left=0.015, right=0.985, bottom=0.015, top=0.99)
    save_figure(fig, "architecture")

    fig, ax = plt.subplots(figsize=(9.8, 1.6))
    ax.axis("off")
    ax.text(
        0.5,
        0.69,
        r"$s(q,d)=\sum_{t\in\mathrm{unique}(q)}\log\!\left(1+\frac{N-df(t)+0.5}{df(t)+0.5}\right)\,\frac{f(t,d)(k_1+1)}{f(t,d)+k_1(1-b+bL_d/L_{avg})}$",
        ha="center",
        va="center",
        fontsize=17,
    )
    ax.text(0.5, 0.15, r"$k_1=1.5\qquad b=0.75\qquad \mathrm{top\_k}=8$", ha="center", fontsize=15)
    fig.subplots_adjust(left=0.01, right=0.99, bottom=0.05, top=0.95)
    save_figure(fig, "bm25_equation")

    order = [
        ("ambiguity", "Ambiguity / explicit ID"),
        ("language", "Script variants"),
        ("compound", "Compound recommendations"),
        ("boundary", "Evidence / domain"),
        ("dialogue", "Dialogue"),
    ]
    fig, ax = plt.subplots(figsize=(9.4, 4.0))
    for arm, offset, color, fill, label in [
        ("system", -0.16, BLUE, True, "Deployed system"),
        ("baseline", 0.16, GOLD, False, "BM25 + LLM"),
    ]:
        values = [summary["arms"][arm]["families"][f] for f, _ in order]
        widths = [100 * v["passed"] / v["attempted"] for v in values]
        yy = [i + offset for i in range(len(order))]
        ax.barh(
            yy,
            widths,
            height=0.28,
            color=color if fill else "white",
            edgecolor=color,
            lw=1,
            hatch=None if fill else "///",
            label=label,
            zorder=3,
        )
        for y, x, v in zip(yy, widths, values):
            ax.text(
                x + 1.5, y, f"{v['passed']}/{v['attempted']}", va="center", fontsize=11, color=color
            )
    ax.set_yticks(range(len(order)), [label for _, label in order])
    ax.invert_yaxis()
    ax.set_xlim(0, 116)
    ax.set_xticks([0, 25, 50, 75, 100], ["0%", "25%", "50%", "75%", "100%"])
    ax.set_xlabel("AI-reviewed task completion", fontsize=12)
    ax.grid(axis="x", color="#DDE3E8", lw=0.6, zorder=0)
    ax.spines[["top", "right"]].set_visible(False)
    ax.tick_params(axis="y", length=0, labelsize=11)
    ax.legend(frameon=False, loc="lower left", bbox_to_anchor=(0, 1.03), ncols=2, fontsize=11)
    fig.subplots_adjust(left=0.32, right=0.965, top=0.86, bottom=0.17)
    save_figure(fig, "family_results")

    fig, ax = plt.subplots(figsize=(9.2, 2.2))
    lo, hi = [100 * x for x in summary["paired"]["cluster_bootstrap_95pct"]]
    delta = 100 * summary["paired"]["difference"]
    ax.axvline(0, color=INK, lw=0.9, linestyle="--")
    ax.plot([lo, hi], [0, 0], color=BLUE, lw=2)
    ax.plot([lo, hi], [0, 0], "|", color=BLUE, ms=16, mew=1.5)
    ax.plot(delta, 0, "o", color=BLUE, ms=9)
    ax.text(delta, 0.2, f"{delta:+.1f} pp", ha="center", fontweight="bold", fontsize=12)
    ax.text(lo, -0.18, f"{lo:+.1f}", ha="center", fontsize=11)
    ax.text(hi, -0.18, f"{hi:+.1f}", ha="center", fontsize=11)
    ax.set(
        xlim=(-30, 22),
        ylim=(-0.4, 0.46),
        yticks=[],
        xlabel="Deployed system − baseline (percentage points)",
    )
    ax.set_xticks([-30, -20, -10, 0, 10, 20])
    ax.grid(axis="x", lw=0.5, color="#E0E6EB")
    ax.spines[["left", "right", "top"]].set_visible(False)
    fig.subplots_adjust(left=0.08, right=0.98, bottom=0.28, top=0.98)
    save_figure(fig, "paired_difference")


def register_fonts(directory):
    for name, file in [
        ("Body", "arial.ttf"),
        ("Body-Bold", "arialbd.ttf"),
        ("Body-Italic", "ariali.ttf"),
        ("Mono", "cour.ttf"),
    ]:
        pdfmetrics.registerFont(TTFont(name, str(directory / file)))
    pdfmetrics.registerFontFamily(
        "Body", normal="Body", bold="Body-Bold", italic="Body-Italic", boldItalic="Body-Bold"
    )
    pdfmetrics.registerFont(TTFont("CJK", str(directory / "msjh.ttc"), subfontIndex=0))
    pdfmetrics.registerFontFamily("CJK", normal="CJK", bold="CJK", italic="CJK", boldItalic="CJK")


def inline(text):
    # Escape first, then add only renderer-owned markup.
    text = html.escape(text, quote=False)
    text = re.sub(r"`([^`]+)`", lambda m: '<font name="Mono" size="8.6">' + m[1] + "</font>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"\*([^*]+)\*", r"<i>\1</i>", text)
    text = re.sub(
        r"\[([^\]]+)\]\((https?://[^)]+)\)",
        r'<link href="\2" color="#205A83"><u>\1</u></link>',
        text,
    )
    text = re.sub(
        r"[\u2e80-\u9fff\uff00-\uffef]+",
        lambda m: '<font name="CJK">' + m[0] + "</font>",
        text,
    )
    # Exact byte hashes can wrap at their midpoint without losing characters.
    text = re.sub(r"(?<![0-9a-f])([0-9a-f]{32})([0-9a-f]{32})(?![0-9a-f])", r"\1<br/>\2", text)
    return text


def pdf(font_directory):
    register_fonts(font_directory)
    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            "Text",
            fontName="Body",
            fontSize=10.2,
            leading=14.1,
            textColor=colors.HexColor(INK),
            spaceAfter=8,
            splitLongWords=1,
        )
    )
    styles.add(
        ParagraphStyle(
            "TitleReport",
            parent=styles["Text"],
            fontName="Body-Bold",
            fontSize=26,
            leading=31,
            spaceAfter=7,
        )
    )
    styles.add(
        ParagraphStyle(
            "SubtitleReport",
            parent=styles["Text"],
            fontSize=15,
            leading=21,
            textColor=colors.HexColor(BLUE),
            spaceAfter=13,
        )
    )
    styles.add(
        ParagraphStyle(
            "SectionReport",
            parent=styles["Text"],
            fontName="Body-Bold",
            fontSize=16,
            leading=20,
            spaceBefore=6,
            spaceAfter=10,
            keepWithNext=1,
        )
    )
    styles.add(
        ParagraphStyle(
            "SubsectionReport",
            parent=styles["Text"],
            fontName="Body-Bold",
            fontSize=11.5,
            leading=15,
            spaceBefore=5,
            spaceAfter=6,
            keepWithNext=1,
        )
    )
    styles.add(
        ParagraphStyle(
            "CaptionReport",
            parent=styles["Text"],
            fontSize=8.9,
            leading=12,
            textColor=colors.HexColor(GREY),
            spaceAfter=10,
        )
    )
    styles.add(
        ParagraphStyle("CellReport", parent=styles["Text"], fontSize=9.0, leading=12, spaceAfter=0)
    )
    styles.add(
        ParagraphStyle(
            "CodeReport",
            fontName="Mono",
            fontSize=8.0,
            leading=11,
            textColor=colors.HexColor(INK),
            backColor=colors.HexColor("#F4F6F8"),
            borderPadding=8,
            spaceBefore=5,
            spaceAfter=9,
        )
    )
    output = HERE / "COMP4136_HK_Movie_RAG_Report.pdf"
    doc = SimpleDocTemplate(
        str(output),
        pagesize=A4,
        leftMargin=48,
        rightMargin=48,
        topMargin=49,
        bottomMargin=48,
        title="Hong Kong Movie RAG: Evidence-Grounded Question Answering and a Paired Diagnostic Evaluation",
        author="COMP4136 project owner — identity fields pending",
        subject="Metadata study; AI review; human audit pending",
        invariant=1,
    )
    width = A4[0] - 96
    lines = (HERE / "FINAL_REPORT.md").read_text(encoding="utf8").splitlines()
    story = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        if line == "<!-- pagebreak -->":
            story.append(PageBreak())
            i += 1
            continue
        if line.startswith("```"):
            block = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                # Wrap shell commands visually without adding execution claims.
                raw = lines[i]
                while len(raw) > 94:
                    cut = raw.rfind(" ", 0, 94)
                    cut = cut if cut > 0 else 94
                    block.append(raw[:cut])
                    raw = "    " + raw[cut:].lstrip()
                block.append(raw)
                i += 1
            story.append(Preformatted("\n".join(block), styles["CodeReport"]))
            i += 1
            continue
        if line.startswith("| "):
            table = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                row = [x.strip() for x in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-+:?", x) for x in row):
                    table.append([Paragraph(inline(x), styles["CellReport"]) for x in row])
                i += 1
            n = len(table[0])
            colwidths = [width * 0.29, width * 0.71] if n == 2 else [width / n] * n
            if n == 3:
                colwidths = (
                    [width * 0.32, width * 0.13, width * 0.55]
                    if "Turns" in table[0][1].text
                    else [width * 0.62, width * 0.19, width * 0.19]
                )
            t = Table(table, colWidths=colwidths, repeatRows=1, hAlign="LEFT")
            t.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E9EFF4")),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 7),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                        ("TOPPADDING", (0, 0), (-1, -1), 6),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                        ("LINEBELOW", (0, 0), (-1, 0), 0.7, colors.HexColor(BLUE)),
                        ("LINEBELOW", (0, 1), (-1, -1), 0.35, colors.HexColor("#D9E1E7")),
                    ]
                )
            )
            story.extend([t, Spacer(1, 10)])
            continue
        if line.startswith("!["):
            match = re.fullmatch(r"!\[([^\]]*)\]\(([^)]+)\)", line)
            path = HERE / match[2]
            with PILImage.open(path) as img:
                w, h = img.size
            target = width
            height = target * h / w
            maxheight = {
                "architecture.png": 175,
                "family_results.png": 220,
                "paired_difference.png": 135,
                "bm25_equation.png": 85,
            }.get(path.name, 250)
            if height > maxheight:
                target *= maxheight / height
                height = maxheight
            picture = Image(str(path), width=target, height=height)
            picture.hAlign = "CENTER"
            next_i = i + 1
            while next_i < len(lines) and not lines[next_i].strip():
                next_i += 1
            if next_i < len(lines) and lines[next_i].startswith("*Figure"):
                story.append(
                    KeepTogether(
                        [
                            picture,
                            Spacer(1, 5),
                            Paragraph(inline(lines[next_i]), styles["CaptionReport"]),
                        ]
                    )
                )
                i = next_i + 1
            else:
                story.append(picture)
                i += 1
            continue
        if line.startswith("# "):
            style = "TitleReport"
            text = line[2:]
        elif line.startswith("## "):
            text = line[3:]
            style = "SubtitleReport" if text.startswith("Evidence-Grounded") else "SectionReport"
        elif line.startswith("### "):
            style = "SubsectionReport"
            text = line[4:]
        elif line.startswith("- "):
            story.append(
                Paragraph(
                    inline(line[2:]),
                    ParagraphStyle(
                        "BulletReport",
                        parent=styles["Text"],
                        leftIndent=12,
                        firstLineIndent=0,
                        bulletIndent=0,
                    ),
                    bulletText="•",
                )
            )
            i += 1
            continue
        else:
            paragraph = [line]
            i += 1
            while (
                i < len(lines)
                and lines[i].strip()
                and not lines[i].startswith(("#", "|", "![", "<!--", "```", "- "))
            ):
                paragraph.append(lines[i].strip())
                i += 1
            story.append(Paragraph(inline(" ".join(paragraph)), styles["Text"]))
            continue
        story.append(Paragraph(inline(text), styles[style]))
        i += 1

    def frame(canvas, document):
        canvas.saveState()
        canvas.setFont("Body", 8)
        canvas.setFillColor(colors.HexColor(GREY))
        canvas.drawString(48, A4[1] - 27, "COMP4136  |  Hong Kong Movie RAG")
        canvas.drawRightString(A4[0] - 48, A4[1] - 27, "Technical report · 5 October 2026")
        canvas.setStrokeColor(colors.HexColor("#D9E1E7"))
        canvas.setLineWidth(0.5)
        canvas.line(48, A4[1] - 34, A4[0] - 48, A4[1] - 34)
        canvas.drawString(48, 25, "Frozen metadata evaluation · Report only")
        canvas.drawRightString(A4[0] - 48, 25, str(document.page))
        canvas.restoreState()

    doc.build(story, onFirstPage=frame, onLaterPages=frame)
    return output


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--font-directory", type=Path, default=Path("C:/Windows/Fonts"))
    args = ap.parse_args()
    summary = evidence()
    figures(summary)
    output = pdf(args.font_directory)
    generated = (
        [output]
        + sorted((HERE / "figures").glob("*.png"))
        + sorted((HERE / "figures").glob("*.svg"))
    )
    manifest = {
        "built_at_utc": datetime.now(UTC).isoformat(),
        "evidence_checks": "passed; independent aggregate/paired/bootstrap reproduction",
        "sources": {
            **EXPECTED,
            "FINAL_REPORT.md": sha(HERE / "FINAL_REPORT.md"),
            "build_report.py": sha(Path(__file__)),
        },
        "outputs": {str(p.relative_to(HERE)): sha(p) for p in generated},
        "runtime": {"matplotlib": matplotlib.__version__},
        "human_audits": "pending",
        "submission_ready": False,
        "visual_review": "pending; build success does not establish visual inspection",
    }
    (HERE / "build-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf8", newline="\n"
    )
    print(
        json.dumps(
            {
                "report": str(output),
                "figures": 4,
                "evidence_checks": "passed",
                "visual_review": "pending",
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
