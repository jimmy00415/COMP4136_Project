"""Build the report and scientific figures offline from exact published evidence.

Document-only dependencies are separate from the frozen application's lock.
No network, model dispatch, production mutation or changes to study artifacts.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, Rectangle
from paired_evidence import EXPECTED, evidence, family_chart
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

FIGURES = ("architecture", "constraint_pipeline", "family_results")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def jsonl(path):
    return [json.loads(x) for x in path.read_text(encoding="utf8").splitlines()]


def save_figure(fig, name):
    (HERE / "figures").mkdir(exist_ok=True)
    fig.savefig(HERE / "figures" / (name + ".png"), dpi=300, facecolor="white")
    fig.savefig(HERE / "figures" / (name + ".svg"), facecolor="white")
    plt.close(fig)


def figures(result):
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 11, "svg.fonttype": "none"})
    fig, ax = plt.subplots(figsize=(10.2, 3.4))
    ax.set(xlim=(0, 10.2), ylim=(0, 3.4))
    ax.axis("off")

    def box(x, y, w, h, label):
        ax.add_patch(Rectangle((x, y), w, h, facecolor="#EFF4F8", edgecolor=BLUE, lw=1.2))
        ax.text(x + w / 2, y + h / 2, label, ha="center", va="center", fontsize=10, color=INK)

    def arrow(a, b):
        ax.add_patch(FancyArrowPatch(a, b, arrowstyle="-|>", mutation_scale=13, color=GREY, lw=1.2))

    box(0.15, 1.3, 1.65, 0.8, "Browser\nQuestion + history")
    box(2.2, 1.3, 1.8, 0.8, "API + query routing\nIdentity / constraints")
    box(4.4, 2, 2.1, 0.85, "Structured catalog\nPostgreSQL")
    box(4.4, 0.45, 2.1, 0.85, "Dense passages\npgvector + embeddings")
    box(6.95, 1.3, 1.7, 0.8, "Answer layer\nDirect / Vertex AI")
    box(8.95, 1.3, 1.1, 0.8, "Grounding\nCards + cites")
    arrow((1.8, 1.7), (2.2, 1.7))
    arrow((4, 1.7), (4.4, 2.4))
    arrow((4, 1.7), (4.4, 0.9))
    arrow((6.5, 2.4), (6.95, 1.7))
    arrow((6.5, 0.9), (6.95, 1.7))
    arrow((8.65, 1.7), (8.95, 1.7))
    ax.text(
        3.1,
        0.65,
        "Clarify / refuse when\nidentity or evidence is insufficient",
        ha="center",
        fontsize=9,
        color=GREY,
    )
    fig.tight_layout(pad=0.3)
    save_figure(fig, "architecture")
    fig, ax = plt.subplots(figsize=(10.2, 1.8))
    ax.set(xlim=(0, 10.2), ylim=(0, 1.8))
    ax.axis("off")
    labels = [
        "Parse constraints\n+ actual history",
        "Filter candidates\nALL required predicates",
        "Rank eligible items\nCount + no repetition",
        "Answer from evidence\nCitations + cards",
    ]
    for i, label in enumerate(labels):
        x = 0.08 + i * 2.55
        ax.add_patch(Rectangle((x, 0.55), 2.32, 0.82, facecolor="#EFF4F8", edgecolor=BLUE, lw=1.2))
        ax.text(x + 1.16, 0.96, label, ha="center", va="center", fontsize=10, color=INK)
        if i < 3:
            ax.add_patch(
                FancyArrowPatch(
                    (x + 2.32, 0.96),
                    (x + 2.55, 0.96),
                    arrowstyle="-|>",
                    mutation_scale=12,
                    color=GREY,
                )
            )
    fig.tight_layout(pad=0.2)
    save_figure(fig, "constraint_pipeline")
    family_chart(result, HERE)


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
        title="Hong Kong Movie RAG: Evidence-Grounded Chinese Question Answering and Conversational Recommendations",
        author="COMP4136 project owner; identity fields unfilled",
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
            colwidths = [width * 0.36, width * 0.64] if n == 2 else [width / n] * n
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
                "family_results.png": 195,
                "paired_difference.png": 135,
                "constraint_pipeline.png": 105,
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
        canvas.drawRightString(A4[0] - 48, A4[1] - 27, "Final report | 5 October 2026")
        canvas.setStrokeColor(colors.HexColor("#D9E1E7"))
        canvas.setLineWidth(0.5)
        canvas.line(48, A4[1] - 34, A4[0] - 48, A4[1] - 34)
        canvas.drawString(48, 25, "Final project report | Metadata development evaluation")
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
        + [HERE / "figures" / (name + ".png") for name in FIGURES]
        + [HERE / "figures" / (name + ".svg") for name in FIGURES]
    )
    manifest = {
        "built_at_utc": datetime.now(UTC).isoformat(),
        "evidence_checks": "passed; exact paired evidence bytes and independent paired aggregate reproduction",
        "reproduced_metrics": summary,
        "sources": {
            **EXPECTED,
            "paired_evidence.py": sha(HERE / "paired_evidence.py"),
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
                "figures": 3,
                "evidence_checks": "passed",
                "visual_review": "pending",
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
