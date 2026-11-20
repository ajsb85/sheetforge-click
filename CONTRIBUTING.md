# Contributing

Use Python 3.10 or newer on Linux or WSL. Install the system tools listed in
the [README](README.md). Create a virtual environment and install the
development dependencies.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest -q
```

Use small synthetic PDFs, archives, and HTML in tests. For parser bugs,
include the operating system, parser versions, the page number, and the
expected evidence. Share a reproducer you have permission to distribute.
Keep local source collections, credentials, and generated corpora outside Git.

## Changes and commits

- Keep changes focused and run the test suite before opening a pull request.
- Use Conventional Commits with a subject under 50 characters. Examples
  include `fix: retain empty table cells` and `docs: explain OCR settings`.
- Leave a blank line before an optional commit body and wrap it at 72 columns.
- Explain user-visible behavior and verification in the pull request.
