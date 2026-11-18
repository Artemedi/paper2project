"""Resource-bounded PDF text extraction; not a filesystem/network sandbox."""
from __future__ import annotations

import io
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile

MAX_BYTES = 32 * 1024 * 1024
MEMORY_BYTES = 512 * 1024 * 1024
CPU_SECONDS = 15
WALL_SECONDS = 30


def extract(raw):
    """Pass only source bytes to a fresh interpreter; never parse in the caller."""
    if sys.platform != "linux":
        raise ValueError("Resource-limited PDF import requires Linux")
    if len(raw) > MAX_BYTES:
        raise ValueError("PDF exceeds32MiB")
    # Regular temporary files let RLIMIT_FSIZE bound output before the parent reads it.
    with tempfile.TemporaryFile() as source, tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        source.write(raw)
        source.seek(0)
        with subprocess.Popen([sys.executable, "-I", str(Path(__file__).resolve())],
                              stdin=source, stdout=output, stderr=errors,
                              env={}, close_fds=True) as process:
            try:
                process.wait(timeout=WALL_SECONDS)
            except subprocess.TimeoutExpired as exc:
                process.kill()
                process.wait()
                raise ValueError("PDF extraction exceeded wall-clock limit") from exc
            except BaseException:
                process.kill()
                process.wait()
                raise
        if process.returncode:
            errors.seek(0)
            # Only the worker's fixed public errors are exposed, never parser diagnostics.
            error = errors.read(256).decode("utf-8", errors="replace").strip()
            if error not in PUBLIC_ERRORS:
                error = "PDF extraction failed or exceeded resource limits"
            raise ValueError(error)
        output.seek(0)
        text = output.read(MAX_BYTES + 1)
        if len(text) > MAX_BYTES:
            raise ValueError("PDF extracted text exceeds32MiB")
        try:
            return text.decode("utf-8")
        except UnicodeError as exc:
            raise ValueError("Invalid PDF extraction output") from exc


PUBLIC_ERRORS = {
    "Install the distribution's pypdf dependency to import PDFs",
    "Encrypted research PDF is not supported",
    "PDF exceeds300 pages",
    "PDF has no extractable text; supply reviewed Markdown/OCR",
    "PDF extracted text exceeds32MiB",
}


def _limits():
    import resource

    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_BYTES, MEMORY_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS, CPU_SECONDS))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_BYTES, MAX_BYTES))


def _text(raw):
    # Imported only after the child has installed its resource limits.
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ValueError("Install the distribution's pypdf dependency to import PDFs") from exc
    reader = PdfReader(io.BytesIO(raw))
    if reader.is_encrypted:
        raise ValueError("Encrypted research PDF is not supported")
    if len(reader.pages) > 300:
        raise ValueError("PDF exceeds300 pages")
    parts, size, has_text = [], 0, False
    for i, page in enumerate(reader.pages):
        text = (page.extract_text() or "").replace("\0", "\ufffd")  # extractors can emit NUL
        has_text |= bool(text.strip())
        part = ("\n\n" if i else "") + f"# PDF page {i+1}\n{text}"
        size += len(part.encode("utf-8"))
        if size > MAX_BYTES:
            raise ValueError("PDF extracted text exceeds32MiB")
        parts.append(part)
    if not has_text:
        raise ValueError("PDF has no extractable text; supply reviewed Markdown/OCR")
    return "".join(parts)


def _main():
    try:
        _limits()
        logging.disable(logging.CRITICAL)
        raw = sys.stdin.buffer.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("PDF exceeds32MiB")
        sys.stdout.buffer.write(_text(raw).encode("utf-8"))
        sys.stdout.buffer.flush()
    except Exception as exc:
        message = str(exc) if isinstance(exc, ValueError) and str(exc) in PUBLIC_ERRORS else "PDF extraction failed or exceeded resource limits"
        os.write(2, message.encode("utf-8"))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
