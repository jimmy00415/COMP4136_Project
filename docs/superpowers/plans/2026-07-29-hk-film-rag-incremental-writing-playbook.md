# Hong Kong Film RAG Incremental Writing Playbook Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a concise, visually verified, four-page Chinese PDF that teaches film-analysis authors to create factually correct, self-contained, evidence-backed RAG knowledge blocks using only a binary usable/unusable gate.

**Architecture:** A reviewable Markdown file is the single content source. A focused ReportLab script parses a deliberately small Markdown subset, renders each explicit source page into one A4 PDF page, and fails when content overflows. Standard-library `unittest` tests validate the copy contract and generated PDF; pypdf checks structure/text, and Poppler PNG rendering plus visual inspection checks layout.

**Tech Stack:** Python 3 from the bundled Codex runtime, ReportLab 4.4.9, pypdf 6.10.0, pdfplumber 0.11.9, standard-library `unittest`, Poppler `pdfinfo`/`pdftoppm`, Windows DengXian fonts.

## Global Constraints

- The approved design is `docs/superpowers/specs/2026-07-29-hk-film-rag-incremental-writing-playbook-design.md` at or after commit `e4d3a66`.
- The author-facing PDF is exactly four A4 portrait pages with no separate cover.
- The author-facing workflow must not require A/B testing, model baselines, retrieval metrics, numeric scoring, percentages, fixed word counts, or fixed claim counts.
- The only publication result is `可用` or `不可用`; do not introduce intermediate grades or statuses.
- Every usable block must be film-specific, self-contained, evidence-backed, and free of known factual errors.
- Film observations require an actual edition/scene/time locator; external facts require a source that opens and supports the statement; interpretations must be visibly grounded in facts or observations.
- Never invent a timecode, page number, quotation, budget, production anecdote, influence relationship, or citation.
- The PDF may explain that the unsupported “three sequels” statement is unusable. The corrected original-run scope may be stated only with a human-readable source citation.
- Use Fortune Star Media’s official `Aces Go Places V` catalogue page, `https://www.fortunestarentertainment.com/acesgoplacesv.html`, as the primary citation that Part V is the last film in the original series; state the scope as the 1982-1989 original run so the later 1997 title is not silently conflated.
- The original `Hong_Kong_Film_RAG_Writing_Playbook_Jimmy_Chen.pdf` remains unchanged with SHA-256 `8ce2e03268c976b49c87f0e6412a67ee6afd93156a0a097c3cfada9b9b17702f`.
- The new PDF is a training artifact, not a movie `movie_document` and not movie-fact ground truth.
- Do not modify RAG ingestion, chunking, embedding, retrieval, GCP, archive, or cleanup code.
- Use `C:\Windows\Fonts\Deng.ttf` and `C:\Windows\Fonts\Dengb.ttf`; register and embed them through ReportLab.
- Write temporary renders only below `tmp/pdfs/playbook-v2/` and remove them after final inspection.
- Write the final artifact to `output/pdf/Hong_Kong_Film_RAG_Incremental_Writing_Playbook_Jimmy_Chen.pdf`.
- Stage and commit only the files named in this plan; preserve all existing untracked movie PDFs, posters, and delivery folders.

---

## File Structure

```text
RAG/
├── .gitignore
├── docs/
│   ├── playbooks/
│   │   └── hk-film-rag-incremental-writing-playbook.md
│   └── superpowers/
│       ├── specs/2026-07-29-hk-film-rag-incremental-writing-playbook-design.md
│       └── plans/2026-07-29-hk-film-rag-incremental-writing-playbook.md
├── scripts/
│   └── build_hk_film_rag_incremental_writing_playbook.py
├── tests/
│   ├── test_playbook_content.py
│   └── test_playbook_pdf.py
├── output/pdf/
│   └── Hong_Kong_Film_RAG_Incremental_Writing_Playbook_Jimmy_Chen.pdf
└── tmp/pdfs/playbook-v2/
    └── page-1.png ... page-4.png
```

- The Markdown file owns every reader-facing sentence, heading, example, source note, and checklist item.
- The Python script owns parsing, typography, colors, page geometry, headers/footers, metadata, overflow detection, and PDF output.
- `test_playbook_content.py` owns the author-copy contract independent of PDF generation.
- `test_playbook_pdf.py` owns PDF structure, metadata, extraction, page-count, original-hash, and embedded-output checks.
- The rendered PNG directory is temporary and must not be committed.

### Task 1: Write the four-page author copy under a binary content contract

**Files:**
- Create: `tests/test_playbook_content.py`
- Create: `docs/playbooks/hk-film-rag-incremental-writing-playbook.md`

**Interfaces:**
- Produces: a UTF-8 Markdown source split by the exact marker `<!-- PAGE_BREAK -->` into four pages.
- Produces: the exact page-title list consumed later by `build_pdf()` and PDF tests.
- Consumes: the approved design spec and the official Fortune Star source URL in Global Constraints.

- [ ] **Step 1: Write the failing content-contract test**

Create `tests/test_playbook_content.py` with the following contract:

```python
from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE = REPO_ROOT / "docs/playbooks/hk-film-rag-incremental-writing-playbook.md"
PAGE_BREAK = "<!-- PAGE_BREAK -->"
PAGE_TITLES = (
    "# 1｜什么样的电影分析对 RAG 有用",
    "# 2｜怎样快速写出可用知识块",
    "# 3｜《最佳拍档》：可用与不可用",
    "# 4｜直接套用：模板与交付检查",
)
BANNED = (
    "A 组",
    "B 组",
    "N/U/E/A",
    "CONTEXT",
    "ACCEPT",
    "UNRESOLVED",
    "REJECT",
    "评分",
    "分数",
    "百分比",
    "通过率",
    "加权总分",
    "00:xx",
)


class PlaybookContentContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text = SOURCE.read_text(encoding="utf-8")
        self.pages = [page.strip() for page in self.text.split(PAGE_BREAK)]

    def test_source_has_exactly_four_named_pages(self) -> None:
        self.assertEqual(len(self.pages), 4)
        self.assertEqual(tuple(page.splitlines()[0] for page in self.pages), PAGE_TITLES)

    def test_copy_uses_only_binary_gate(self) -> None:
        self.assertIn("可用", self.text)
        self.assertIn("不可用", self.text)
        for banned in BANNED:
            self.assertNotIn(banned, self.text)

    def test_copy_contains_actionable_evidence_rules(self) -> None:
        for required in (
            "片源版本",
            "实际时间码",
            "来源能够打开并支持原声明",
            "事实、观察、解释",
            "没有已知事实错误",
            "一个知识块只解决一个问题",
        ):
            self.assertIn(required, self.text)

    def test_series_correction_has_human_readable_source(self) -> None:
        self.assertIn("1982-1989", self.text)
        self.assertIn("四部续集", self.text)
        self.assertIn(
            "https://www.fortunestarentertainment.com/acesgoplacesv.html",
            self.text,
        )

    def test_copy_has_no_unfinished_or_fake_locator(self) -> None:
        self.assertIsNone(
            re.search(r"\b(?:TO(?:DO)|T(?:BD))\b", self.text, re.IGNORECASE)
        )
        self.assertNotRegex(self.text, r"00:[xX]{2}")
        self.assertNotRegex(self.text, r"[\u2010-\u2015]")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the content test and verify it fails because the source is absent**

Run:

```powershell
& 'C:\Users\陈奕炜\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_playbook_content -v
```

Expected: error from `Path.read_text()` because `docs/playbooks/hk-film-rag-incremental-writing-playbook.md` does not exist.

- [ ] **Step 3: Write the four-page Markdown source**

Create `docs/playbooks/hk-film-rag-incremental-writing-playbook.md` with exactly three `<!-- PAGE_BREAK -->` markers and these page responsibilities:

1. `# 1｜什么样的电影分析对 RAG 有用`
   - State the outcome: short, factually correct, evidence-backed, independently retrievable analysis.
   - Give the three qualitative tests: cannot be derived from basic metadata; cannot be transferred unchanged to another film; answers a concrete question with checkable evidence.
   - Contrast basic metadata, plot repetition, and generic praise with film-specific detail, formal observation, grounded interpretation, and sourced production history.
   - Introduce only `可用` and `不可用`.
2. `# 2｜怎样快速写出可用知识块`
   - Explain the four actions: choose one concrete question; record evidence while viewing/researching; write one self-contained block; run the yes/no gate.
   - Explain fact, observation, and interpretation as writing boundaries, not grades.
   - Include the binary checklist from the approved spec without numeric thresholds.
3. `# 3｜《最佳拍档》：可用与不可用`
   - Mark year/director/cast/genre repetition as unusable analysis content.
   - Treat the buttock-tattoo clue as usable only after film-edition, scene, and actual-timecode verification; do not invent the locator.
   - Split the low-angle chase example into an observation and an interpretation.
   - Correct the original 1982-1989 run to the first film plus four sequels, define the scope, and cite the Fortune Star `Aces Go Places V` page as the source identifying Part V as the last of that series.
   - Say that unsupported budget, negotiation, and stunt-team claims are omitted, not assigned a grade.
4. `# 4｜直接套用：模板与交付检查`
   - Give the title/claim/evidence/analysis/tags template.
   - Give the document-level usable/unusable checklist.
   - State the Markdown/PDF/code boundary and that the playbook is not movie-fact ground truth.
   - End with: `短不是少做，短是只保留能够被检索、核查和复用的知识。`

Use compact paragraphs, bullets, and callouts. Do not use Markdown tables; cards and bullets are easier to fit and render legibly on A4.

- [ ] **Step 4: Run the content test and verify it passes**

Run the same unittest command from Step 2.

Expected: all content-contract tests pass.

- [ ] **Step 5: Review the factual wording against the source page**

Open `https://www.fortunestarentertainment.com/acesgoplacesv.html` and verify the final wording does not claim more than the page supports. The allowed inference is: the original title plus Parts II-V equals four sequels within the explicitly named 1982-1989 run. Do not make claims about whether the 1997 title is canon, a reboot, or part of the same run.

- [ ] **Step 6: Commit the content source and its contract test**

```powershell
git add -- 'docs/playbooks/hk-film-rag-incremental-writing-playbook.md' 'tests/test_playbook_content.py'
git commit -m "docs: write binary RAG analysis playbook"
```

### Task 2: Build a fail-fast four-page PDF generator

**Files:**
- Modify: `.gitignore`
- Create: `scripts/build_hk_film_rag_incremental_writing_playbook.py`
- Create: `tests/test_playbook_pdf.py`

**Interfaces:**
- Consumes: `docs/playbooks/hk-film-rag-incremental-writing-playbook.md`.
- Produces: `read_source_pages(source_path: Path) -> list[str]`.
- Produces: `register_fonts() -> None`.
- Produces: `make_styles() -> StyleSheet1`.
- Produces: `markdown_to_flowables(markdown: str, styles: StyleSheet1) -> list[Flowable]`.
- Produces: `draw_header_footer(canvas: Canvas, page_number: int) -> None`.
- Produces: `render_page(canvas: Canvas, page_markdown: str, page_number: int) -> None`.
- Produces: `build_pdf(source_path: Path, output_path: Path) -> Path`.
- Raises: `LayoutOverflowError` if any explicit source page does not fit one A4 page.
- Uses: `C:\Windows\Fonts\Deng.ttf` and `C:\Windows\Fonts\Dengb.ttf`.

- [ ] **Step 1: Add temporary render output to `.gitignore`**

Append this exact repository-relative rule, preserving the current `.worktrees/` and `.superpowers/` rules:

```gitignore
tmp/pdfs/
```

- [ ] **Step 2: Write the failing PDF contract test**

Create `tests/test_playbook_pdf.py`:

```python
from __future__ import annotations

import hashlib
import importlib.util
import tempfile
import unittest
from pathlib import Path

from pypdf import PdfReader


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts/build_hk_film_rag_incremental_writing_playbook.py"
SOURCE = REPO_ROOT / "docs/playbooks/hk-film-rag-incremental-writing-playbook.md"
ORIGINAL = REPO_ROOT / "Hong_Kong_Film_RAG_Writing_Playbook_Jimmy_Chen.pdf"
ORIGINAL_SHA256 = "8ce2e03268c976b49c87f0e6412a67ee6afd93156a0a097c3cfada9b9b17702f"
PAGE_TITLES = (
    "1｜什么样的电影分析对 RAG 有用",
    "2｜怎样快速写出可用知识块",
    "3｜《最佳拍档》：可用与不可用",
    "4｜直接套用：模板与交付检查",
)


def load_builder():
    spec = importlib.util.spec_from_file_location("playbook_builder", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PlaybookPdfContractTest(unittest.TestCase):
    def test_original_pdf_hash_is_unchanged(self) -> None:
        digest = hashlib.sha256(ORIGINAL.read_bytes()).hexdigest()
        self.assertEqual(digest, ORIGINAL_SHA256)

    def test_builder_produces_four_extractable_pages(self) -> None:
        builder = load_builder()
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "playbook.pdf"
            builder.build_pdf(SOURCE, output)
            reader = PdfReader(output)
            self.assertEqual(len(reader.pages), 4)
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
            for title in PAGE_TITLES:
                self.assertIn(title, text)
            self.assertIn("可用", text)
            self.assertIn("不可用", text)
            self.assertNotIn("N/U/E/A", text)
            self.assertNotIn("A 组", text)
            self.assertNotIn("B 组", text)

    def test_pdf_metadata_identifies_training_artifact(self) -> None:
        builder = load_builder()
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "playbook.pdf"
            builder.build_pdf(SOURCE, output)
            metadata = PdfReader(output).metadata
            self.assertEqual(metadata.title, "香港电影 RAG 增量写作手册")
            self.assertEqual(metadata.author, "Jimmy Chen")
            self.assertIn("作者培训", metadata.subject)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run the PDF test and verify it fails because the builder is absent**

```powershell
& 'C:\Users\陈奕炜\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest tests.test_playbook_pdf -v
```

Expected: failure because `scripts/build_hk_film_rag_incremental_writing_playbook.py` does not exist.

- [ ] **Step 4: Implement the Markdown reader and inline formatter**

Create `scripts/build_hk_film_rag_incremental_writing_playbook.py` with these exact public constants and interfaces:

```python
from __future__ import annotations

import argparse
import html
import re
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, StyleSheet1
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Flowable, Frame, HRFlowable, Paragraph, Spacer


PAGE_BREAK = "<!-- PAGE_BREAK -->"
FONT_REGULAR = Path(r"C:\Windows\Fonts\Deng.ttf")
FONT_BOLD = Path(r"C:\Windows\Fonts\Dengb.ttf")
FONT_NAME = "PlaybookDeng"
FONT_BOLD_NAME = "PlaybookDengBold"


class LayoutOverflowError(RuntimeError):
    pass


def read_source_pages(source_path: Path) -> list[str]:
    text = source_path.read_text(encoding="utf-8")
    pages = [page.strip() for page in text.split(PAGE_BREAK)]
    if len(pages) != 4 or any(not page for page in pages):
        raise ValueError("source must contain exactly four non-empty pages")
    return pages


def format_inline(text: str) -> str:
    escaped = html.escape(text, quote=True)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(
        r"\[([^\]]+)\]\((https?://[^)]+)\)",
        r'<link href="\2" color="#1E5A82"><u>\1</u></link>',
        escaped,
    )
    return escaped
```

Register the fonts with these exact helpers before creating styles or drawing a page:

```python
def register_fonts() -> None:
    for path in (FONT_REGULAR, FONT_BOLD):
        if not path.is_file():
            raise FileNotFoundError(f"required font not found: {path}")
    pdfmetrics.registerFont(TTFont(FONT_NAME, str(FONT_REGULAR)))
    pdfmetrics.registerFont(TTFont(FONT_BOLD_NAME, str(FONT_BOLD)))
    pdfmetrics.registerFontFamily(
        FONT_NAME,
        normal=FONT_NAME,
        bold=FONT_BOLD_NAME,
        italic=FONT_NAME,
        boldItalic=FONT_BOLD_NAME,
    )
```

The Markdown subset must recognize `#`, `##`, `###`, paragraphs, `- ` bullets, `> ` callouts, horizontal rules, and blank-line paragraph boundaries. It must reject an unclosed code fence or unsupported page directive with `ValueError`; it must not silently discard content.

- [ ] **Step 5: Implement styles and one-page-at-a-time rendering**

Create `make_styles() -> StyleSheet1` with named styles for `page_title`, `section`, `subsection`, `body`, `bullet`, `usable`, `unusable`, `template`, and `source_note`. Use:

- A4 portrait;
- 16 mm left/right margins;
- 15 mm top content margin below the header;
- 14 mm bottom content margin above the footer;
- navy `#102A43` for headings;
- red `#B53A3A` for unusable callouts;
- green `#287A55` for usable callouts;
- body text no smaller than 9 pt;
- 1.28-1.35 line spacing for Chinese body copy;
- pale fills with dark text instead of white text on dark body cells.

Render each source page independently:

```python
def render_page(canvas: Canvas, page_markdown: str, page_number: int) -> None:
    draw_header_footer(canvas, page_number)
    flowables = markdown_to_flowables(page_markdown, make_styles())
    frame = Frame(16 * mm, 14 * mm, A4[0] - 32 * mm, A4[1] - 29 * mm, showBoundary=0)
    frame.addFromList(flowables, canvas)
    if flowables:
        raise LayoutOverflowError(f"page {page_number} content overflowed")
```

`draw_header_footer()` must show `香港电影 RAG 增量写作手册 · v2.0` and `Jimmy Chen · 2026-07-29 · {page_number}/4` with the embedded regular font. Every callout must include the literal text `可用` or `不可用`, so meaning does not depend on color.

- [ ] **Step 6: Implement `build_pdf()` and CLI**

```python
def build_pdf(source_path: Path, output_path: Path) -> Path:
    register_fonts()
    pages = read_source_pages(source_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas = Canvas(str(output_path), pagesize=A4, pageCompression=1)
    canvas.setTitle("香港电影 RAG 增量写作手册")
    canvas.setAuthor("Jimmy Chen")
    canvas.setSubject("面向电影分析作者的 RAG 可用知识增量写作与二元准入培训手册")
    for page_number, page_markdown in enumerate(pages, 1):
        render_page(canvas, page_markdown, page_number)
        if page_number < len(pages):
            canvas.showPage()
    canvas.save()
    return output_path
```

The CLI must accept `--source` and `--output`, defaulting to the exact paths in Global Constraints, and print only the resolved output path after success.

- [ ] **Step 7: Run both contract suites**

```powershell
& 'C:\Users\陈奕炜\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest discover -s tests -p 'test_playbook_*.py' -v
```

Expected: all content and PDF tests pass. If a page overflows, shorten the Markdown copy first; do not reduce body text below 9 pt.

- [ ] **Step 8: Commit the generator and tests**

```powershell
git add -- '.gitignore' 'scripts/build_hk_film_rag_incremental_writing_playbook.py' 'tests/test_playbook_pdf.py'
git commit -m "feat: generate four-page RAG writing playbook"
```

### Task 3: Produce, inspect, and publish the verified PDF

**Files:**
- Create: `output/pdf/Hong_Kong_Film_RAG_Incremental_Writing_Playbook_Jimmy_Chen.pdf`
- Temporarily create and then remove: `tmp/pdfs/playbook-v2/page-1.png` through `page-4.png`
- Modify if inspection requires: `docs/playbooks/hk-film-rag-incremental-writing-playbook.md`
- Modify if inspection requires: `scripts/build_hk_film_rag_incremental_writing_playbook.py`
- Modify if a regression is found: `tests/test_playbook_content.py`, `tests/test_playbook_pdf.py`

**Interfaces:**
- Consumes: approved Markdown and generator from Tasks 1-2.
- Produces: the final stable-path PDF plus machine and visual verification evidence.
- Preserves: original PDF bytes and every unrelated untracked user asset.

- [ ] **Step 1: Build the final PDF**

```powershell
& 'C:\Users\陈奕炜\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' `
  'scripts/build_hk_film_rag_incremental_writing_playbook.py' `
  --source 'docs/playbooks/hk-film-rag-incremental-writing-playbook.md' `
  --output 'output/pdf/Hong_Kong_Film_RAG_Incremental_Writing_Playbook_Jimmy_Chen.pdf'
```

Expected: the command prints the resolved output path and exits 0.

- [ ] **Step 2: Run machine-readable PDF checks**

```powershell
& 'C:\Users\陈奕炜\.cache\codex-runtimes\codex-primary-runtime\dependencies\bin\override\pdfinfo.cmd' `
  'output/pdf/Hong_Kong_Film_RAG_Incremental_Writing_Playbook_Jimmy_Chen.pdf'

& 'C:\Users\陈奕炜\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' `
  -m unittest discover -s tests -p 'test_playbook_*.py' -v
```

Expected: `Pages: 4`, A4 page size, and all tests pass.

- [ ] **Step 3: Render all pages to PNG**

Create only the exact directory `tmp/pdfs/playbook-v2/`, then run:

```powershell
& 'C:\Users\陈奕炜\.cache\codex-runtimes\codex-primary-runtime\dependencies\bin\override\pdftoppm.cmd' `
  -png -r 160 `
  'output/pdf/Hong_Kong_Film_RAG_Incremental_Writing_Playbook_Jimmy_Chen.pdf' `
  'tmp/pdfs/playbook-v2/page'
```

Expected: exactly four PNG files.

- [ ] **Step 4: Visually inspect every rendered page**

Use the local image viewer on all four PNGs. Require:

- no clipped or overlapping text;
- no table/card overflow;
- no missing Chinese glyphs, black boxes, or substituted fonts;
- readable body copy at 100% zoom;
- consistent page title, footer, spacing, and alignment;
- usable/unusable callouts distinguishable by both label and color;
- no low-contrast white-on-light or dark-on-dark text;
- sources readable and not cut off;
- the four pages feel concise rather than crowded.

If a defect appears, update Markdown or layout code, add or refine a regression assertion where possible, rebuild, rerun all tests, and re-render every page before continuing.

- [ ] **Step 5: Verify source integrity and output identity**

```powershell
(Get-FileHash -Algorithm SHA256 -LiteralPath 'Hong_Kong_Film_RAG_Writing_Playbook_Jimmy_Chen.pdf').Hash.ToLowerInvariant()
(Get-FileHash -Algorithm SHA256 -LiteralPath 'output/pdf/Hong_Kong_Film_RAG_Incremental_Writing_Playbook_Jimmy_Chen.pdf').Hash.ToLowerInvariant()
```

Expected: original hash equals `8ce2e03268c976b49c87f0e6412a67ee6afd93156a0a097c3cfada9b9b17702f`; the new output hash is non-empty and different from the original.

- [ ] **Step 6: Remove only the generated render directory**

Resolve `tmp/pdfs/playbook-v2/` to an absolute path, verify it remains below `D:\VS_PROJECT\HK_Movie_KnowledgeGraph\RAG\tmp\pdfs\`, enumerate exactly the four generated PNGs, delete those files, and remove the directory only when empty. Do not use a recursive delete against `tmp`, `tmp/pdfs`, or the workspace root.

- [ ] **Step 7: Inspect Git scope and commit the final artifact**

```powershell
git status --short
git diff --check
git add -- 'output/pdf/Hong_Kong_Film_RAG_Incremental_Writing_Playbook_Jimmy_Chen.pdf'
git commit -m "docs: publish incremental RAG writing playbook"
```

If Markdown, generator, or tests changed during visual QA, stage those exact files in the same final commit. Do not stage any poster directory, delivery folder, original movie PDF, `.env`, or unrelated untracked asset.

- [ ] **Step 8: Final verification after commit**

Rerun the full unittest command, `pdfinfo`, original SHA-256 check, and `git status --short`. Completion requires passing tests, four PDF pages, unchanged original hash, no temporary PNGs, and only the pre-existing unrelated untracked user assets remaining.
