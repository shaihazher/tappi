"""PDF tool — read and create PDFs.

Read: PyMuPDF (pymupdf) — fast, no external deps.
Create: WeasyPrint — HTML/CSS to PDF, handles layouts properly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tappi.agent.config import get_workspace

TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "pdf",
        "description": (
            "Read and create PDF files. Can extract text from existing PDFs, "
            "get page count and metadata, or create new PDFs from HTML/Markdown content."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["read", "create", "info"],
                    "description": (
                        "PDF action:\n"
                        "- read: Extract text from a PDF (requires 'path', optional 'pages')\n"
                        "- create: Create PDF from HTML content (requires 'path' and 'html')\n"
                        "- info: Get page count, title, author (requires 'path')"
                    ),
                },
                "path": {"type": "string", "description": "PDF file path (relative to workspace)"},
                "pages": {"type": "string", "description": "Page range for read, e.g. '1-5' or '1,3,7' (default: all)"},
                "html": {"type": "string", "description": "HTML content to convert to PDF"},
            },
            "required": ["action", "path"],
        },
    },
}


class PDFTool:
    """PDF read/create operations, sandboxed to workspace."""

    def __init__(self, workspace: Path | None = None) -> None:
        self._workspace = workspace

    @property
    def workspace(self) -> Path:
        if self._workspace is None:
            self._workspace = get_workspace()
        self._workspace = self._workspace.resolve()
        self._workspace.mkdir(parents=True, exist_ok=True)
        return self._workspace

    def _resolve(self, path: str) -> Path:
        resolved = (self.workspace / path).resolve()
        ws_resolved = self.workspace.resolve()
        if not str(resolved).startswith(str(ws_resolved)):
            raise PermissionError(f"Access denied: path escapes workspace — {path}")
        return resolved

    def _parse_pages(self, pages_str: str, total: int) -> list[int]:
        """Parse page range string into list of 0-indexed page numbers."""
        result = []
        for part in pages_str.split(","):
            part = part.strip()
            if "-" in part:
                start, end = part.split("-", 1)
                s = max(0, int(start) - 1)
                e = min(total, int(end))
                result.extend(range(s, e))
            else:
                idx = int(part) - 1
                if 0 <= idx < total:
                    result.append(idx)
        return sorted(set(result))

    def execute(self, **params: Any) -> str:
        action = params.get("action", "")
        try:
            if action == "read":
                return self._read(params)
            elif action == "create":
                return self._create(params)
            elif action == "info":
                return self._info(params)
            else:
                return f"Unknown action: {action}"
        except ImportError as e:
            pkg = "pymupdf" if "fitz" in str(e) else "weasyprint"
            return f"Missing dependency: pip install {pkg}"
        except Exception as e:
            return f"Error: {e}"

    def _read(self, params: dict) -> str:
        path = params.get("path", "")
        resolved = self._resolve(path)
        if not resolved.exists():
            return f"File not found: {path}"

        import fitz  # pymupdf

        doc = fitz.open(str(resolved))
        total = len(doc)
        pages_str = params.get("pages")
        page_nums = self._parse_pages(pages_str, total) if pages_str else list(range(total))

        text_parts = []
        for i in page_nums:
            page = doc[i]
            text = page.get_text()
            if text.strip():
                text_parts.append(f"--- Page {i + 1} ---\n{text}")

        doc.close()

        if not text_parts:
            return f"No text extracted from {path} (might be a scanned/image PDF)."

        result = "\n".join(text_parts)
        # Cap at 50KB
        if len(result) > 50_000:
            result = result[:50_000] + f"\n\n... (truncated, {total} pages total)"
        return result

    def _create(self, params: dict) -> str:
        path = params.get("path", "")
        html = params.get("html", "")
        if not html:
            return "Error: 'html' content required for create action."

        resolved = self._resolve(path)
        resolved.parent.mkdir(parents=True, exist_ok=True)

        from weasyprint import HTML

        HTML(string=html).write_pdf(str(resolved))
        size = resolved.stat().st_size
        return f"PDF created: {path} ({size} bytes)"

    def _info(self, params: dict) -> str:
        path = params.get("path", "")
        resolved = self._resolve(path)
        if not resolved.exists():
            return f"File not found: {path}"

        import fitz

        doc = fitz.open(str(resolved))
        meta = doc.metadata or {}
        pages = len(doc)
        doc.close()

        lines = [
            f"File: {path}",
            f"Pages: {pages}",
            f"Size: {resolved.stat().st_size} bytes",
        ]
        if meta.get("title"):
            lines.append(f"Title: {meta['title']}")
        if meta.get("author"):
            lines.append(f"Author: {meta['author']}")
        if meta.get("subject"):
            lines.append(f"Subject: {meta['subject']}")
        return "\n".join(lines)
