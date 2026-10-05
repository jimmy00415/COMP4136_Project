# Offline final-report build

The report describes the repaired deployment and a fresh paired comparison against BM25 + LLM: 60/60 versus 50/60 known development turns. It uses 120 retained responses from one run, without pooling historical scores. No population confidence interval or unseen-test claim is presented.

The builder runs offline. `paired_evidence.py` verifies exact hashes of cases, paired answers, Root AI review, reviewed summary, freeze and measured runner. It independently recounts paired outcomes, task families, decisions, cards, citations and timings. The 784 + 680 field agreement is a retained full-catalog audit result, not a fresh catalog audit inside the document builder. Both original mechanical judgments and seven reasoned corrections are available.

Use a separate document environment, preserving the application dependency lock:

```shell
python -m pip install reportlab==4.4.9 matplotlib==3.11.2 Pillow==12.3.0 pypdf==6.10.0
python report/build_report.py --font-directory C:/Windows/Fonts
```

On this workstation the existing external document environment was used. No application dependency or cloud resource was changed. The pypdf package is for QA, not PDF generation. Fonts are local licensed Arial/Courier and Microsoft JhengHei: `arial.ttf`, `arialbd.ttf`, `ariali.ttf`, `cour.ttf`, `msjh.ttc`. They are not distributed. Changing fonts/runtime can change layout and hashes.

Outputs: one final PDF, three PNG/SVG figures, and an evidence-bound build manifest. Figures use matplotlib, including two architecture/process diagrams and one side-by-side paired chart derived from observed per-family counts. The old baseline equation and paired-difference figures are removed from the current report directory.

```shell
pdftoppm -r 120 -png report/COMP4136_HK_Movie_RAG_Report.pdf /path/to/review/page
```

The build manifest records visual review pending at build time. `visual-review.json` binds the subsequent Root AI inspection of every final page to exact PDF and image hashes. This is AI review, not human approval. A changed PDF invalidates an older visual-review receipt.
