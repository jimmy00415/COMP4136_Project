# Offline report build

The report is separate from the frozen application runtime. Building it makes no network or model calls and does not change evaluation files. It verifies byte hashes of the published cases, answer projections, explicit AI audit and reviewed summary, then independently reproduces totals, family outcomes, paired counts and the descriptive cluster-bootstrap interval.

Use a separate Python 3.11+ document environment with these already verified authoring versions:

```shell
python -m pip install reportlab==4.4.9 matplotlib==3.11.2 Pillow==12.3.0 pypdf==6.10.0
python report/build_report.py --font-directory C:/Windows/Fonts
```

The pypdf version above must match the authoring environment receipt; it is used for QA, not by the builder. Do not change the application's `uv.lock` for document-only dependencies. On this workstation the existing external document environment was used; no dependency was installed into the application.

The builder embeds locally installed Arial/Courier and Microsoft JhengHei (`arial.ttf`, `arialbd.ttf`, `ariali.ttf`, `cour.ttf`, `msjh.ttc`) for English, identifiers and actual Chinese case text. Fonts are not distributed in this repository. Supply a compatible licensed local font directory; a different font/runtime can change layout and output hashes, requiring fresh visual review.

Outputs are one report PDF, four scientific PNG/SVG figures and a build manifest. The SVG/PNG figures are static export artifacts built with matplotlib from real reviewed evidence; they do not use an image-generation model or a dashboard runtime. The build manifest always records visual review pending at build time; the separate, hash-bound visual review records the later actual inspection.

Render all pages with Poppler, for example:

```shell
pdftoppm -r 120 -png report/COMP4136_HK_Movie_RAG_Report.pdf /path/to/review/page
```

Read `visual-review.json` for the recorded Root AI page/figure inspection and exact reviewed output hashes. That inspection is AI review, not human approval. Rebuilding the PDF invalidates its prior visual-review hash unless the output is byte-identical.
