# Sheetforge Click

Sheetforge Click converts local MikroE Click board documentation into
page-cited Markdown and a single JSONL retrieval corpus. It combines
collections by board slug, deduplicates exact PDF copies by SHA-256, and
retains every source occurrence, including PDFs inside archives.

The extractor combines LiteParse, PyMuPDF, Poppler, and targeted Tesseract OCR.
Board profiles add context from saved product pages, including specifications,
mikroBUS pinouts, software examples, and CAD archive listings. PDF evidence
includes page links, table candidates, schematic previews, and structured
connectivity where Altium PDF bookmarks provide explicit net membership.

## Install

Use Python 3.10 or newer on Linux or WSL. Install Poppler for independent
column-aligned extraction, Tesseract with English language data for OCR, and
7-Zip if your inputs contain RAR or 7z archives. The required executable names
are `pdftotext`, `tesseract`, and either `7z` or `7zz`.

```bash
git clone https://github.com/ajsb85/sheetforge-click.git
cd sheetforge-click
python -m venv .venv
source .venv/bin/activate
python -m pip install .
```

## Build a corpus

Each collection root contains one directory per board slug. A board folder
can hold PDFs, archives, images, and an optional saved `index.html`.
Provide one or more local collections.

```bash
sheetforge-click \
  --collection primary=/path/to/primary/products \
  --collection secondary=/path/to/secondary/products \
  --output out-click --jobs 8
```

Repeat the same command to resume a run. Use `--board batt-boost-click` with a
separate output folder for a board-specific build. Use `--ocr off` to run
without Tesseract, or choose installed languages with `--ocr eng+deu`.

| Output | Contents |
| --- | --- |
| `retrieval.jsonl.gz` | Streamable board, PDF-page, and column-aligned evidence passages |
| `boards/<slug>.md` | Board profile, local assets, and links to document transcriptions |
| `documents/<prefix>/<sha256>/` | PDF Markdown, chunks, metadata, and available previews |
| `boards.jsonl` and `documents.jsonl` | Board and document indexes with provenance |
| `catalog.json` and `issues.jsonl` | Coverage counts and input discovery issues |

For example, filter the retrieval stream by board with `jq`.

```bash
gzip -dc out-click/retrieval.jsonl.gz |
  jq -c 'select(.board_slugs | index("batt-boost-click"))'
```

A full local validation run processed 4,294 distinct PDFs and 93,818 pages
across 2,002 boards. The corpus and source files live outside this repository.
Keep your inputs available for original-page links. Use the cited PDF page
to resolve ambiguous OCR, table boundaries, or drawing geometry.

See the [architecture guide](docs/architecture.md) for extraction and output
details, and the [contribution guide](CONTRIBUTING.md) for development setup.
The [MIT license](LICENSE) covers this repository's code. Dependencies have
their own licenses, including PyMuPDF's AGPL and commercial licensing options.

Use [the driver workflow](docs/driver-workflow.md) to generate mikroSDK 2.0
packages from corpus evidence with Xiaomi MiMo.
