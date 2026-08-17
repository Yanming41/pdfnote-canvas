# pdfnote-canvas

Canvas-style PDF reading and AI annotation prototype.

The app renders PDF pages with PyMuPDF, serves a small browser UI from the local machine, lets the reader select text on the rendered page, and stores selection-rooted notes and Q/A turns in a local SQLite database under `~\.pdfnote`.

## Features

- Render PDF pages into a browser-based reading canvas.
- Preserve text selections as annotation blocks with page and rectangle metadata.
- Store multi-turn messages under the selected text block.
- Reuse full-PDF and page-local context when generating AI prompts.
- Run locally with Python standard library HTTP serving plus PyMuPDF.

## Install

```powershell
python -m pip install pymupdf
```

## Usage

```powershell
cd C:\Users\13116\pdfnote-canvas
python .\app.py "C:\path\paper.pdf"
```

Optional flags:

```powershell
python .\app.py "C:\path\paper.pdf" --port 8765 --scale 1.6 --no-browser
```

## Data

Runtime data is stored outside the repository:

```text
~\.pdfnote\
  pdfnote.sqlite3
```

This repository intentionally ignores PDFs, rendered images, local databases, logs, virtual environments, and cache directories.

## Project Notes

`PROJECT_REPORT.md` records the design rationale and interaction model behind the prototype.
