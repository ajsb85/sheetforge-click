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

## Generate a mikroSDK 2.0 driver

`sheetforge-driver` makes one package per board slug. It combines the board
pinout, schematic, and component datasheets into a cited hardware plan. MiMo
generates the package from that plan and unrelated SDK examples. The
validator checks the package and feeds failures back into a bounded retry loop.

1. Build a corpus as shown above. A single-board run also accepts
   `out-click/retrieval.jsonl.gz` directly.
2. Clone or locate the mikroSDK Click repository. Pass the checkout itself
   as `--sdk`, or its parent if it contains both `mikrosdk_click_v2` and
   `mikrosdk_v2`.
3. Create a local `.env` from the sample and set `MIMO_PAYG_API_KEY` for the
   standard MiMo API. Calls can incur charges.

```bash
test -f .env || cp .env.sample .env
# Edit .env to set MIMO_PAYG_API_KEY before running.
sheetforge-driver \
  --corpus out-click \
  --sdk /path/to/mikrosdk \
  --board batt-boost-click \
  --model mimo-v2.6-pro \
  --output out-drivers
```

The package is written to `out-drivers/batt-boost-click/driver/battboost/`.
It includes a C library and header, example, CMake files, NECTO manifests,
and a README. The board directory also contains `plan.json`,
`citations.json`, `trace.json`, and `report.json`. Run the same command
again to resume from an accepted plan or reuse a validated package.

- **Interactive MiMo Token Plan.** Use `--provider exchange` to write a
  request under `out-drivers/exchange/`. Open it in a compatible MiMo coding
  tool, save the plain JSON response to the indicated reply path, and rerun.
- **Small batch.** Repeat `--board`, or replace it with `--all --limit 5`
  when using an indexed corpus directory.
- **Compare and build.** Use `--compare-reference` for a blind comparison
  against a matching SDK driver. Use `--reference-api` in a separate output
  folder to supply its public declarations and check that they match.
  Use `--build-harness` with a trusted local builder to cross-compile.
  Review the cited settings and test on hardware before deployment.

See the [driver workflow](docs/driver-workflow.md) for the generation stages
and the [architecture guide](docs/architecture.md) for corpus extraction.
The [contribution guide](CONTRIBUTING.md) covers development setup.

The [MIT license](LICENSE) covers this repository's code. Dependencies have
their own licenses, including PyMuPDF's AGPL and commercial licensing options.
