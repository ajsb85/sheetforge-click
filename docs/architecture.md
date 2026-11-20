# MikroE Click board corpus

Sheetforge Click builds a local corpus of board documentation and PDF evidence.
It combines the two saved product collections by board slug and deduplicates PDFs
by exact SHA-256. The document index retains every matching source path,
including archive member paths, aliases, and board associations. Different
revisions remain separate documents even when their filenames match.

## Read and ingest

- Start with `out-click/boards/<board-slug>.md` for the board overview, supported
  interfaces, operating limits, mikroBUS pinout, product-page code, and links
  to PDF transcriptions, saved pages, images, and CAD archives. Distinct
  technical product-page captures have their own sections.
- Follow a PDF link to its `document.md` for source metadata, bookmarks, text,
  tables, visual evidence, and exact page citations. The matching
  `metadata.json` records extraction coverage and notes for each page.
  `layout.md` preserves an independent column-aligned transcription where
  Poppler can read the native PDF text.
- Read `out-click/retrieval.jsonl.gz` for a single UTF-8 JSONL stream for LLM
  retrieval. Each record has a stable `id`, `kind`, `board_slugs`, `artifact`,
  and `text`. PDF records also include `document_id`, `page` (one-based),
  `part`, source path, and preview filename where available. Resolve
  `artifact` and `metadata` relative to the corpus root. Resolve Markdown
  links and preview filenames relative to the corresponding artifact.
  Records of kind `pdf_layout` retain a second reading of pages where table
  structure depends on the semantic parser.
- Use `documents.jsonl` to join a document ID to every source occurrence,
  `boards.jsonl` to join board slugs to profiles and documents, `catalog.json`
  for counts, and `issues.jsonl` for source discovery errors. Per-PDF
  `chunks.jsonl` files provide a smaller input for a board-specific import.

```bash
gzip -dc out-click/retrieval.jsonl.gz |
  jq -c 'select(.board_slugs | index("batt-boost-click"))'
```

Keep the original collection paths available when browsing source links. The
export copies a PDF into its artifact directory when the only local source
is an archive member, so those documents also have page links.

## Rebuild

```bash
sheetforge-click --collection primary=/path/to/products -o out-click -j 8
```

Repeat `--collection LABEL=/path/to/products` to combine roots. To export a
subset, repeat `--board SLUG` and set a separate output folder.

The OCR stage uses Tesseract and defaults to the installed English model.
Use `--ocr eng+deu` for a multilingual pass with both language models
installed, or `--ocr off` to skip OCR. Install 7-Zip to read RAR and 7z
members if those archive formats are present. Poppler's `pdftotext` provides
column-aligned text and a fallback when a PDF or page fails to open with PyMuPDF.

Re-running the same command checks the source fingerprints and reuses
completed artifacts. Incomplete documents are retried. Check
`failed_documents`, `partial_documents`, and `page_failures` in the
catalog after a run. The command exits nonzero for failed document or page
extraction.

## Extraction and fidelity

The original sheetforge conversion is designed for selected component
datasheets. It renders positioned HTML using pdf2htmlEX, rebuilds the layout,
and sends the extracted content through a model to generate Markdown and
schema-validated data. Its character and numeric checks can catch invented
values, while scrambled cells can still contain valid source values.

The Click corpus uses LiteParse/PDFium for semantic Markdown and PyMuPDF for
independent page text and ruled table detection. Page-text checks bring in a
second transcription when tokens appear to be missing. Table candidates
carry page bounds and distinguish blank or merged positions in metadata.
Local OCR handles sparse or badly decoded pages. Schematic and sparse pages
include rendered previews. Other diagrams, plots, and drawings can be opened
at their original page through the source links. Text, tables, OCR, and
interactive 3D previews vary with what the PDF exposes. Verify disputed
values or geometry on the original page.

Some Altium exports carry a bookmark tree with explicit components, nets,
and pin membership. Those documents include a structured `schematic.json`
and page-scoped connectivity passages. Each record traces to the PDF's
bookmark-tree position. Schematic PDFs without that structure retain their
visible labels and drawing.

## Parser selection

The parser review covered the local LiteParse, Marker, and MinerU checkouts. Marker provides
model-based layout, table, and OCR extraction on CPU and GPU. MinerU offers
model backends with structured layout and OCR. Both require a model setup.
The corpus uses the lighter PDFium and MuPDF path for native documents and
limits OCR to pages that need it.

The evaluation compared the existing pdf2htmlEX extraction and LiteParse on the Batt Boost
schematic and its 58-page NBM5100 datasheet. LiteParse produced a clearer
pin-description table. Its Table 1 omitted the pulse-current column.
The independent Poppler layout retained both values of `≥ 150 mA` in that
column, and the retrieval corpus includes that evidence. Extraction runs
locally with no API credentials or model service.
