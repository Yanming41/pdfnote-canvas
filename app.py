from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import urllib.parse
import webbrowser
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
APP_DIR = Path.home() / ".pdfnote"
DB_PATH = APP_DIR / "pdfnote.sqlite3"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return value.encode("utf-8", "replace").decode("utf-8")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class Store:
    def __init__(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.conn.executescript(
            """
            create table if not exists pdfs (
                id integer primary key autoincrement,
                title text not null,
                path text not null,
                fingerprint text not null unique,
                created_at text not null
            );
            create table if not exists canvas_annotations (
                id integer primary key autoincrement,
                pdf_id integer not null references pdfs(id),
                page integer not null,
                selected_text text not null,
                surrounding_text text not null,
                rects_json text not null,
                question text,
                answer text,
                note text,
                created_at text not null
            );
            create table if not exists canvas_blocks (
                id integer primary key autoincrement,
                pdf_id integer not null references pdfs(id),
                page integer not null,
                selected_text text not null,
                surrounding_text text not null,
                rects_json text not null,
                created_at text not null
            );
            create table if not exists canvas_messages (
                id integer primary key autoincrement,
                block_id integer not null references canvas_blocks(id),
                role text not null,
                content text not null,
                created_at text not null
            );
            """
        )
        self.conn.commit()

    def upsert_pdf(self, path: Path, title: str, fingerprint: str) -> int:
        with self.lock:
            row = self.conn.execute("select id from pdfs where fingerprint=?", (fingerprint,)).fetchone()
            if row:
                self.conn.execute("update pdfs set title=?, path=? where id=?", (title, str(path), row["id"]))
                self.conn.commit()
                return int(row["id"])
            cur = self.conn.execute(
                "insert into pdfs(title,path,fingerprint,created_at) values(?,?,?,?)",
                (title, str(path), fingerprint, utc_now()),
            )
            self.conn.commit()
            return int(cur.lastrowid)

    def migrate_annotations(self, pdf_id: int) -> None:
        marker = self.conn.execute(
            "select count(*) as n from canvas_blocks where pdf_id=?", (pdf_id,)
        ).fetchone()["n"]
        if marker:
            return
        rows = self.conn.execute(
            "select * from canvas_annotations where pdf_id=? order by page,id", (pdf_id,)
        ).fetchall()
        for row in rows:
            block_id = self.create_block(
                pdf_id,
                {
                    "page": row["page"],
                    "selected_text": row["selected_text"],
                    "surrounding_text": row["surrounding_text"],
                    "rects": json.loads(row["rects_json"] or "[]"),
                },
                commit=False,
            )
            if row["question"]:
                self.add_message(block_id, "user", row["question"], commit=False)
            if row["answer"]:
                self.add_message(block_id, "assistant", row["answer"], commit=False)
            if row["note"]:
                self.add_message(block_id, "note", row["note"], commit=False)
        self.conn.commit()

    def create_block(self, pdf_id: int, payload: dict, commit: bool = True) -> int:
        cur = self.conn.execute(
            """
            insert into canvas_blocks(pdf_id,page,selected_text,surrounding_text,rects_json,created_at)
            values(?,?,?,?,?,?)
            """,
            (
                pdf_id,
                int(payload.get("page") or 1),
                clean_text(payload.get("selected_text")),
                clean_text(payload.get("surrounding_text")),
                json.dumps(payload.get("rects") or [], ensure_ascii=False),
                utc_now(),
            ),
        )
        if commit:
            self.conn.commit()
        return int(cur.lastrowid)

    def add_message(self, block_id: int, role: str, content: str, commit: bool = True) -> int:
        cur = self.conn.execute(
            "insert into canvas_messages(block_id,role,content,created_at) values(?,?,?,?)",
            (block_id, role, clean_text(content), utc_now()),
        )
        if commit:
            self.conn.commit()
        return int(cur.lastrowid)

    def block(self, block_id: int) -> dict:
        row = self.conn.execute("select * from canvas_blocks where id=?", (block_id,)).fetchone()
        return row_to_block(row, self.messages(block_id))

    def messages(self, block_id: int) -> list[dict]:
        rows = self.conn.execute(
            "select * from canvas_messages where block_id=? order by id", (block_id,)
        ).fetchall()
        return [row_to_message(row) for row in rows]

    def blocks(self, pdf_id: int) -> list[dict]:
        with self.lock:
            self.migrate_annotations(pdf_id)
            rows = self.conn.execute(
                "select * from canvas_blocks where pdf_id=? order by page,id", (pdf_id,)
            ).fetchall()
            return [row_to_block(row, self.messages(int(row["id"]))) for row in rows]

    def add_turn(self, pdf_id: int, payload: dict, answer: str) -> dict:
        with self.lock:
            block_id = payload.get("block_id")
            if block_id:
                block_id = int(block_id)
            else:
                block_id = self.create_block(pdf_id, payload, commit=False)
            question = clean_text(payload.get("question") or "解释这段内容")
            self.add_message(block_id, "user", question, commit=False)
            self.add_message(block_id, "assistant", answer, commit=False)
            note = clean_text(payload.get("note"))
            if note:
                self.add_message(block_id, "note", note, commit=False)
            self.conn.commit()
            return self.block(block_id)

    def export_markdown(self, pdf_id: int, title: str, pdf_path: Path) -> Path:
        out_dir = APP_DIR / "exports"
        out_dir.mkdir(parents=True, exist_ok=True)
        safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in title)[:80] or "pdf"
        out = out_dir / f"{safe}-canvas-{pdf_id}.md"
        blocks = self.blocks(pdf_id)
        lines = [f"# {title} 阅读笔记", "", f"- PDF: `{pdf_path}`", f"- Exported: {utc_now()}", ""]
        current_page = None
        for block in blocks:
            if block["page"] != current_page:
                current_page = block["page"]
                lines += ["", f"## Page {current_page}", ""]
            lines += [f"### Text Block #{block['id']}", "", "> " + block["selected_text"].replace("\n", "\n> "), ""]
            for msg in block["messages"]:
                if msg["role"] == "user":
                    lines += [f"**Q:** {msg['content']}", ""]
                elif msg["role"] == "assistant":
                    lines += [msg["content"], ""]
                elif msg["role"] == "note":
                    lines += [f"**Note:** {msg['content']}", ""]
        out.write_text("\n".join(lines), encoding="utf-8")
        return out


def row_to_message(row: sqlite3.Row) -> dict:
    return {
        "id": int(row["id"]),
        "role": row["role"],
        "content": row["content"],
        "created_at": row["created_at"],
    }


def row_to_block(row: sqlite3.Row, messages: list[dict]) -> dict:
    return {
        "id": int(row["id"]),
        "page": int(row["page"]),
        "selected_text": row["selected_text"],
        "surrounding_text": row["surrounding_text"],
        "rects": json.loads(row["rects_json"] or "[]"),
        "messages": messages,
        "created_at": row["created_at"],
    }


class PdfState:
    def __init__(self, path: Path, scale: float) -> None:
        self.path = path.resolve()
        self.scale = scale
        self.doc = fitz.open(self.path)
        self.title = (self.doc.metadata or {}).get("title") or self.path.stem
        self.fingerprint = sha256(self.path)
        self.store = Store()
        self.pdf_id = self.store.upsert_pdf(self.path, self.title, self.fingerprint)
        self._full_text_cache: str | None = None

    def meta(self) -> dict:
        pages = []
        for idx in range(self.doc.page_count):
            page = self.doc.load_page(idx)
            rect = page.rect
            pages.append({"page": idx + 1, "width": rect.width, "height": rect.height})
        return {
            "id": self.pdf_id,
            "title": self.title,
            "path": str(self.path),
            "scale": self.scale,
            "page_count": self.doc.page_count,
            "pages": pages,
            "blocks": self.store.blocks(self.pdf_id),
            "annotations": self.store.blocks(self.pdf_id),
        }

    def page_png(self, page_no: int) -> bytes:
        page = self.doc.load_page(page_no - 1)
        pix = page.get_pixmap(matrix=fitz.Matrix(self.scale, self.scale), alpha=False)
        return pix.tobytes("png")

    def full_text(self, max_chars: int = 40000) -> str:
        if self._full_text_cache is None:
            pages: list[str] = []
            for idx in range(self.doc.page_count):
                page_text = clean_text(self.doc.load_page(idx).get_text("text"))
                if page_text.strip():
                    pages.append(f"--- Page {idx + 1} ---\n{page_text.strip()}")
            self._full_text_cache = "\n\n".join(pages)
        if len(self._full_text_cache) <= max_chars:
            return self._full_text_cache
        return self._full_text_cache[:max_chars] + "\n\n[TRUNCATED: full PDF text is longer than prompt budget]"

    def text_layer(self, page_no: int) -> dict:
        page = self.doc.load_page(page_no - 1)
        blocks = page.get_text("dict").get("blocks", [])
        spans = []
        full_text = []
        for block in blocks:
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                line_text = []
                for span in line.get("spans", []):
                    text = clean_text(span.get("text", ""))
                    if not text.strip():
                        continue
                    x0, y0, x1, y1 = span["bbox"]
                    spans.append({
                        "text": text,
                        "x": x0,
                        "y": y0,
                        "w": x1 - x0,
                        "h": y1 - y0,
                        "size": span.get("size", 10),
                    })
                    line_text.append(text)
                if line_text:
                    full_text.append(" ".join(line_text))
        return {"page": page_no, "spans": spans, "text": "\n".join(full_text)}


def build_prompt(state: PdfState, payload: dict, history: list[dict] | None = None) -> str:
    history_text = "\n".join(
        f"{msg['role']}: {msg['content']}" for msg in (history or [])
    ) or "<no previous conversation for this selected text block>"
    selected_text = clean_text(payload.get("selected_text"))
    page_context = clean_text(payload.get("surrounding_text"))
    return f"""You are answering a question about ONE selected PDF text block.

Critical instructions:
1. Treat SELECTED_TEXT as the root object of the answer.
2. Answer the user's question about SELECTED_TEXT, not about the whole PDF.
3. Use PAGE_CONTEXT and FULL_PDF_REFERENCE only to disambiguate terms, notation, or references inside SELECTED_TEXT.
4. Do not summarize the whole PDF unless the user explicitly asks for a whole-document summary.
5. If SELECTED_TEXT is too short or ambiguous, say that directly and explain what additional surrounding text is needed.

PDF metadata:
- title: {state.title}
- file_path: {state.path}
- sha256: {state.fingerprint}
- page_count: {state.doc.page_count}
- selected_page: {payload.get('page')}

SELECTED_TEXT:
<<<SELECTED_TEXT
{selected_text}
SELECTED_TEXT>>>

PAGE_CONTEXT:
<<<PAGE_CONTEXT
{page_context}
PAGE_CONTEXT>>>

Conversation history for this selected text block:
<<<HISTORY
{history_text}
HISTORY>>>

FULL_PDF_REFERENCE, for background only, page-delimited and truncated if needed:
<<<FULL_PDF_REFERENCE
{state.full_text()}
FULL_PDF_REFERENCE>>>

User question:
{payload.get('question') or 'Explain the selected text.'}

Answer in concise Chinese. Start from the selected text. If you use broader PDF context, explicitly connect it back to the selected text.
"""


def run_ai(prompt: str) -> str:
    command = os.environ.get("PDFNOTE_AI_CMD")
    if not command:
        return "[offline draft] 还没有配置 AI。设置 PDFNOTE_AI_CMD 后，这里会保存真实回答。当前已保存选区、问题和上下文。"
    try:
        result = subprocess.run(command, input=prompt, text=True, encoding="utf-8", errors="replace", shell=True, capture_output=True, timeout=120)
    except Exception as exc:
        return f"[ai command failed] {exc}"
    if result.returncode != 0:
        return f"[ai command exited {result.returncode}] {result.stderr[:1000]}"
    stdout = result.stdout.strip()
    if stdout:
        return stdout
    stderr = result.stderr.strip()
    return f"[ai command returned empty output] stderr={stderr[:1000]}"


class Handler(SimpleHTTPRequestHandler):
    state: PdfState

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/":
            self.serve_file(STATIC / "index.html", "text/html; charset=utf-8")
        elif path == "/static/app.js":
            self.serve_file(STATIC / "app.js", "application/javascript; charset=utf-8")
        elif path == "/static/style.css":
            self.serve_file(STATIC / "style.css", "text/css; charset=utf-8")
        elif path == "/api/doc":
            self.send_json(self.state.meta())
        elif path.startswith("/api/page/") and path.endswith("/text"):
            page_no = int(path.split("/")[3])
            self.send_json(self.state.text_layer(page_no))
        elif path.startswith("/api/page/") and path.endswith(".png"):
            page_no = int(path.split("/")[3].split(".")[0])
            data = self.state.page_png(page_no)
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif path == "/api/export":
            out = self.state.store.export_markdown(self.state.pdf_id, self.state.title, self.state.path)
            self.send_json({"path": str(out)})
        elif path == "/api/prompt-preview":
            sample = {
                "page": 1,
                "selected_text": "<selected text from the PDF>",
                "surrounding_text": "<current page context>",
                "question": "<user question>",
            }
            prompt = build_prompt(self.state, sample)
            self.send_json({"chars": len(prompt), "prompt": prompt[:6000]})
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path not in {"/api/annotation", "/api/chat"}:
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        history = []
        if payload.get("block_id"):
            existing_block = self.state.store.block(int(payload["block_id"]))
            history = existing_block["messages"]
            payload = {
                **existing_block,
                "block_id": existing_block["id"],
                "question": payload.get("question") or "解释这段内容",
                "note": payload.get("note"),
            }
        answer = run_ai(build_prompt(self.state, payload, history))
        block = self.state.store.add_turn(self.state.pdf_id, payload, answer)
        self.send_json(block)

    def serve_file(self, path: Path, content_type: str) -> None:
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, payload: dict | list) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> int:
    parser = argparse.ArgumentParser(prog="pdfnote-canvas")
    parser.add_argument("pdf")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--scale", type=float, default=1.6)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    state = PdfState(Path(args.pdf), args.scale)
    Handler.state = state
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"pdfnote-canvas serving {state.path}")
    print(url)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
