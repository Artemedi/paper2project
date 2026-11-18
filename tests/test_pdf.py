"""Real PDF-byte fixtures and Linux parser resource-limit regressions."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import zlib

from paper2project import pdf
from paper2project.core import App


def fixture(streams, *, compressed=False):
    """Build an original minimal PDF with correct xref offsets, without a parser."""
    kids = " ".join(f"{4 + 2*i} 0 R" for i in range(len(streams)))
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               f"<< /Type /Pages /Kids [{kids}] /Count {len(streams)} >>".encode(),
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    for i, stream in enumerate(streams):
        objects.append((f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] "
                        f"/Resources << /Font << /F1 3 0 R >> >> /Contents {5+2*i} 0 R >>").encode())
        filter_spec = b" /Filter /FlateDecode" if compressed else b""
        objects.append(b"<< /Length " + str(len(stream)).encode() + filter_spec + b" >>\nstream\n" + stream + b"\nendstream")
    data, offsets = bytearray(b"%PDF-1.4\n"), [0]
    for i, value in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{i} 0 obj\n".encode() + value + b"\nendobj\n")
    start = len(data)
    data.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        data.extend(f"{offset:010} 00000 n \n".encode())
    data.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode())
    return bytes(data)


LINUX = sys.platform == "linux"
HAS_PDF = importlib.util.find_spec("pypdf") is not None


@unittest.skipUnless(LINUX and HAS_PDF, "requires Linux and the pinned pdf extra")
class PdfFixtures(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp = Path(directory.name)
        self.app = App(self.tmp / "workspace")

    def test_nul_in_extracted_text_is_replaced(self):
        class Page:
            def extract_text(self):
                return "alpha\0beta"

        fake = types.ModuleType("pypdf")
        fake.PdfReader = lambda stream: types.SimpleNamespace(is_encrypted=False, pages=[Page()])
        with patch.dict(sys.modules, {"pypdf": fake}):
            self.assertEqual(pdf._text(b"fixture"), "# PDF page 1\nalpha\ufffdbeta")

    def test_real_two_page_import_preserves_source_and_coverage(self):
        raw = fixture([b"BT /F1 12 Tf 20 200 Td (alpha) Tj ET",
                       b"BT /F1 12 Tf 20 200 Td (beta) Tj ET"])
        source = self.tmp / "real.pdf"
        source.write_bytes(raw)
        record = self.app.import_research(source)
        snapshot = self.app.store.json(record["snapshot"])
        doc, = snapshot["documents"]
        self.assertEqual(self.app.store.blob(doc["sha256"]), raw)
        self.assertEqual(self.app.store.blob(doc["text"]).decode(), "# PDF page 1\nalpha\n\n# PDF page 2\nbeta")
        self.assertEqual(snapshot["coverage"], "TEXT_EXTRACTED_NOT_VISUALLY_REVIEWED")

    def test_real_flate_stream(self):
        raw = fixture([zlib.compress(b"BT /F1 12 Tf 20 200 Td (compressed) Tj ET")], compressed=True)
        self.assertEqual(pdf.extract(raw), "# PDF page 1\ncompressed")

    def test_malformed_pdf_does_not_create_research_record(self):
        source = self.tmp / "broken.pdf"
        source.write_bytes(b"%PDF-1.4\nnot a document")
        before = self.app.store.list("research")
        with self.assertRaisesRegex(ValueError, "PDF extraction failed"):
            self.app.import_research(source)
        self.assertEqual(self.app.store.list("research"), before)

    def test_blank_pdf_is_actionable(self):
        with self.assertRaisesRegex(ValueError, "no extractable text"):
            pdf.extract(fixture([b""]))

    def test_page_limit(self):
        with self.assertRaisesRegex(ValueError, "300 pages"):
            pdf.extract(fixture([b""] * 301))

    def test_encrypted_pdf(self):
        from pypdf import PdfWriter
        import io

        writer = PdfWriter()
        writer.add_blank_page(width=300, height=300)
        writer.encrypt("fixture-password")
        buffer = io.BytesIO()
        writer.write(buffer)
        with self.assertRaisesRegex(ValueError, "Encrypted"):
            pdf.extract(buffer.getvalue())

    def test_high_expansion_flate_stream_is_bounded(self):
        # 600 MiB of compressed PDF comments, constructed with <=1 MiB chunks.
        compressor = zlib.compressobj()
        chunks = [compressor.compress(b"%" + b"x" * (1024 * 1024 - 2) + b"\n") for _ in range(600)]
        stream = b"".join(chunks) + compressor.flush()
        self.assertLess(len(stream), 1024 * 1024)
        with self.assertRaisesRegex(ValueError, "PDF extraction failed|no extractable text"):
            pdf.extract(fixture([stream], compressed=True))

    def test_accumulated_text_limit_includes_page_markers(self):
        raw = fixture([b"BT /F1 12 Tf 20 200 Td (alpha) Tj ET"] * 3)
        with patch.object(pdf, "MAX_BYTES", 40):
            with self.assertRaisesRegex(ValueError, "extracted text exceeds"):
                pdf._text(raw)


@unittest.skipUnless(LINUX, "requires Linux resource limits")
class PdfBoundary(unittest.TestCase):
    def test_timeout_kills_and_reaps_worker(self):
        processes = []
        popen = subprocess.Popen

        def launch(*args, **kwargs):
            # A sleeping real child makes timeout cleanup independent of startup speed.
            process = popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"], **kwargs)
            processes.append(process)
            return process

        with patch.object(pdf, "WALL_SECONDS", 0.05), patch.object(pdf.subprocess, "Popen", side_effect=launch):
            with self.assertRaisesRegex(ValueError, "wall-clock"):
                pdf.extract(b"%PDF-1.4")
        self.assertIsNotNone(processes[0].poll())
        self.assertEqual(processes[0].returncode, -9)

    def test_interrupt_kills_and_reaps_worker(self):
        processes = []
        popen = subprocess.Popen

        def launch(*args, **kwargs):
            process = popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"], **kwargs)
            processes.append(process)
            wait = process.wait
            interrupted = False

            def interrupt_once(*args, **kwargs):
                nonlocal interrupted
                if not interrupted:
                    interrupted = True
                    raise KeyboardInterrupt
                return wait(*args, **kwargs)

            process.wait = interrupt_once
            return process

        with patch.object(pdf.subprocess, "Popen", side_effect=launch):
            with self.assertRaises(KeyboardInterrupt):
                pdf.extract(b"%PDF-1.4")
        self.assertEqual(processes[0].poll(), -9)

    def test_worker_has_no_inherited_environment_or_extra_descriptors(self):
        popen = subprocess.Popen
        with patch.object(pdf.subprocess, "Popen", wraps=popen) as launch:
            with self.assertRaises(ValueError):
                pdf.extract(b"%PDF-1.4 invalid")
        self.assertEqual(launch.call_args.kwargs["env"], {})
        self.assertTrue(launch.call_args.kwargs["close_fds"])
        self.assertIn("-I", launch.call_args.args[0])

    def limited(self, code, stdout=subprocess.DEVNULL):
        setup = ("import runpy; m=runpy.run_path(" + repr(str(Path(pdf.__file__).resolve())) + "); "
                 "g=m['_limits'].__globals__; g.update(MEMORY_BYTES=64*1024*1024, CPU_SECONDS=1, MAX_BYTES=4096); "
                 "m['_limits'](); ")
        return subprocess.run([sys.executable, "-I", "-c", setup + code], stdout=stdout,
                              stderr=subprocess.DEVNULL, timeout=5, env={})

    def test_memory_limit_is_enforced_by_kernel(self):
        self.assertNotEqual(self.limited("bytearray(128*1024*1024)").returncode, 0)

    def test_cpu_limit_is_enforced_by_kernel(self):
        self.assertEqual(self.limited("exec('while True: pass')").returncode, -9)

    def test_file_output_limit_is_enforced_by_kernel(self):
        with tempfile.TemporaryFile() as output:
            result = self.limited("import os; os.write(1, b'x'*8192); os.write(1,b'x')", stdout=output)
            self.assertNotEqual(result.returncode, 0)
            self.assertLessEqual(os.fstat(output.fileno()).st_size, 4096)

    def test_missing_dependency_has_actionable_error(self):
        # -S deliberately excludes site-packages in this subprocess only.
        command = [sys.executable, "-I", "-S", str(Path(pdf.__file__).resolve())]
        result = subprocess.run(command, input=b"%PDF-1.4", capture_output=True, timeout=5, env={})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"pypdf dependency", result.stderr)
