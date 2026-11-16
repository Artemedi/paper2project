"""Focused offline regressions for the public Paper2Project security backport."""
import io
from email.message import Message
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from paper2project import core, execution, ingest, model
from paper2project.core import App
from paper2project.store import regular_file
import package_release


class PublicSecurityPort(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_input_fd_rejects_symlinked_parent_and_fifo(self):
        safe = self.root / "safe"
        safe.mkdir()
        (safe / "paper.md").write_text("verified")
        (self.root / "link").symlink_to(safe, target_is_directory=True)
        with self.assertRaises(OSError):
            with regular_file(self.root / "link" / "paper.md"):
                pass
        os.mkfifo(self.root / "fifo")
        with self.assertRaisesRegex(ValueError, "regular file"):
            with regular_file(self.root / "fifo"):
                pass

    def test_import_document_is_bounded_and_rejects_symlink(self):
        source = self.root / "source"
        source.mkdir()
        (source / "paper.md").write_text("x" * 16)
        app = App(self.root / "db")
        with self.assertRaisesRegex(ValueError, "byte limit"):
            ingest.documents(app.store, source, prefix="test", max_bytes=8)
        (source / "link.md").symlink_to(source / "paper.md")
        with self.assertRaisesRegex(ValueError, "Symlink"):
            ingest.documents(app.store, source, prefix="test")

    def test_catalog_import_accepts_txt(self):
        source = self.root / "catalog"
        source.mkdir()
        (source / "memo.txt").write_text("A project memory")
        names = [r["name"] for r in App(self.root / "db").import_projects(source, layout="catalog")]
        self.assertEqual(names, ["memo"])

    def test_synthetic_nature_hosts_and_private_addresses(self):
        for hostname in ("idp.nature.com", "static-content.springer-cdn.com"):
            self.assertIsNone(ingest.check_address("198.18.0.8", hostname))
        for private in ("::ffff:127.0.0.1", "64:ff9b::7f00:1", "fec0::1"):
            with self.subTest(private=private), self.assertRaises(ValueError):
                ingest.check_address(private, "arxiv.org")

    def _response(self, body, content_type, url):
        message = Message()
        message["Content-Type"] = content_type
        response = io.BytesIO(body)
        response.headers = message
        response.url = url
        return response

    def test_pdf_url_rejects_html_challenge(self):
        url = "https://arxiv.org/pdf/2501.01234"
        response = self._response(b"<html>CAPTCHA</html>", "text/html", url)
        app = App(self.root / "db")
        opener = Mock(open=Mock(return_value=response))
        with patch.object(ingest, "public_url"), patch.object(ingest.urllib.request, "build_opener",
                                                              return_value=opener):
            with self.assertRaisesRegex(ValueError, "non-PDF"):
                app.import_research(url)
        self.assertEqual(app.store.list("research"), [])

    def test_html_non_utf8_table_columns_are_separated(self):
        url = "https://example.org/summary"
        response = self._response(b"<html><td>caf\xe9</td><td>42</td></html>",
                                  "text/html; charset=windows-1252", url)
        app = App(self.root / "db")
        with patch.object(ingest, "public_url"), patch.object(ingest.urllib.request, "build_opener",
                                                              return_value=Mock(open=Mock(return_value=response))):
            paper = app.import_research(url)
        doc = app.store.json(paper["snapshot"])["documents"][0]
        text = app.store.blob(doc["text"]).decode()
        self.assertIn("café", text)
        self.assertNotIn("café42", text)

    def test_redaction_masks_asset_names_without_collapsing_artifact_keys(self):
        redactor = core.export_redactor(self.root, [], [{
            "documents": [
                {"id": "project:test:result.json", "path": "result.json"},
                {"id": "project:test:metrics.json", "path": "metrics.json"}],
            "assets": {"internal-prod-dump.sql": {"sha256": "a" * 64}}}])
        value = {"summary": "Investigated internal-prod-dump.sql",
                 "output_files": {"result.json": "1" * 64, "metrics.json": "2" * 64}}
        output = redactor(value)
        self.assertNotIn("internal-prod-dump.sql", output["summary"])
        self.assertEqual(set(output["output_files"]), {"result.json", "metrics.json"})

    def test_line_numbers_do_not_influence_relevance(self):
        project = self.root / "project.md"
        project.write_text("# Project\n\nA cache service handles demand.\n")
        article = self.root / "article.md"
        article.write_text("# Article\n\nA new cache algorithm.\n")
        app = App(self.root / "db")
        p, = app.import_projects(project)
        r = app.import_research(article)
        job = app.create_job(r["id"], [p["id"]])
        context = app.context(job["id"], "2")
        self.assertTrue(all(chunk["score"] == 0 for chunk in context["chunks"]))

    def test_model_streaming_deadline_prevents_endless_responses(self):
        endpoint = model.Endpoint("https://api.example.invalid/v1", "fixture", timeout=3)
        class EndlessResponse:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read1(self, limit):
                return b"x"
        with patch.object(model.time, "monotonic", side_effect=[0, 1, 4]), \
             patch.object(model.urllib.request, "build_opener",
                          return_value=Mock(open=Mock(return_value=EndlessResponse()))):
            with self.assertRaisesRegex(ValueError, "total time limit"):
                endpoint.complete([{"role": "user", "content": "fixture"}])

    def test_watchdog_detects_output_root_swapped_during_scan(self):
        out = self.root / "output"
        out.mkdir()
        (out / "result.json").write_text("{}")
        external = self.root / "elsewhere"
        external.mkdir()
        stdout = self.root / "stdout"
        stderr = self.root / "stderr"
        stdout.touch()
        stderr.touch()
        original = os.fwalk

        def swap_root(*args, **kwargs):
            out.rename(self.root / "old-output")
            out.symlink_to(external, target_is_directory=True)
            yield from original(*args, **kwargs)

        with patch.object(execution.os, "fwalk", side_effect=swap_root):
            self.assertEqual(execution.output_breach(stdout, stderr, out),
                             "output directory changed during scan")

    def test_zero_baseline_requires_no_positive_relative_gain(self):
        guard = {"unit": "relative_fraction", "direction": "higher_is_better",
                 "min_gain": 1.0, "zero_baseline_policy": "require_non_worse"}
        self.assertFalse(execution.paired_guard(guard, 0, 0)["passed"])
        guard["min_gain"] = 0
        self.assertTrue(execution.paired_guard(guard, 0, 0)["passed"])

    def test_blank_plan_paths_are_refused(self):
        plan = {"kind": "reproduction", "project_id": "p", "image": "test/image",
                "source": {"path": " ", "revision": "a" * 40, "license": "MIT"},
                "arms": {"baseline": ["true"], "candidate": ["false"]},
                "metrics": [{"name": "value", "path": "value", "expected": {"candidate": 1}}]}
        with self.assertRaisesRegex(ValueError, "source"):
            execution.validate_plan(plan, {"p"})
        plan["source"]["path"] = "/tmp/repo"
        plan["input"] = {"path": ""}
        with self.assertRaisesRegex(ValueError, "input"):
            execution.validate_plan(plan, {"p"})

    def test_watchdog_rejects_symlinked_output_root(self):
        source = self.root / "source"
        source.mkdir()
        out = self.root / "output"
        out.symlink_to(source, target_is_directory=True)
        stdout = self.root / "stdout"
        stderr = self.root / "stderr"
        stdout.touch()
        stderr.touch()
        self.assertEqual(execution.output_breach(stdout, stderr, out),
                         "output directory is missing or unsafe")

    def test_collect_refuses_special_files(self):
        out = self.root / "out"
        out.mkdir()
        os.mkfifo(out / "fifo")
        stdout = self.root / "stdout"
        stderr = self.root / "stderr"
        stdout.touch()
        stderr.touch()
        result = {"exit_code": 0, "error": None, "result": None}
        execution.collect(result, out, stdout, stderr)
        self.assertIn("special file", result["error"])

    def test_stop_container_cleanup_is_bounded(self):
        proc = Mock()
        with patch.object(execution.os, "close") as close, \
             patch.object(execution, "remove_container") as remove:
            execution.stop_container(proc, "test-container", 42)
        close.assert_called_once_with(42)
        proc.wait.assert_called_once_with(timeout=35)
        remove.assert_called_once_with("test-container")

    def test_release_never_archives_untracked_paths(self):
        repo = self.root / "release"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "NOTICE.md").write_text("public")
        (repo / ".env").write_text("private fixture")
        (repo / "src").mkdir()
        (repo / "src" / "entry.py").write_text("print('public')")
        subprocess.run(["git", "-C", str(repo), "add", "NOTICE.md", "src/entry.py"], check=True)
        with patch.object(package_release, "ROOT", repo), \
             patch.object(package_release, "FILES", ["NOTICE.md"]), \
             patch.object(package_release, "TREES", ["src"]):
            relative = [p.relative_to(repo).as_posix() for p in package_release.collect()]
        self.assertEqual(relative, ["NOTICE.md", "src/entry.py"])

    def test_release_mandatory_files_must_be_tracked(self):
        repo = self.root / "release"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "NOTICE.md").write_text("untracked")
        with patch.object(package_release, "ROOT", repo), \
             patch.object(package_release, "FILES", ["NOTICE.md"]), \
             patch.object(package_release, "TREES", []):
            with self.assertRaisesRegex(ValueError, "untracked"):
                package_release.collect()

    def test_completed_baseline_survives_candidate_interruption(self):
        class FakeStore:
            def __init__(self, root):
                self.root, self.saved = root, {}
                self.plan = {"source": {"path": "unused"}, "limits": {"timeout": 1},
                             "image": "local/test"}
                self.exp = {"job_id": "job", "analysis": "analysis", "plan": "plan",
                            "source_tree": {"sha256": "abc"}, "input_tree": None,
                            "source_path": "/source", "input_path": None}
            def get(self, kind, key):
                return self.exp if kind == "experiment" else self.saved[key]
            def json(self, key):
                return self.plan
            def save(self, kind, key, row):
                self.saved[key] = row
                return row
            def put_json(self, value):
                return "immutable-result"
            def list(self, kind, **kwargs):
                return []
        class FakeApp:
            def __init__(self, store):
                self.store = store
            def status(self, job_id):
                return {"stale": [], "analysis": "analysis"}
        (self.root / "runs").mkdir()
        store = FakeStore(self.root)
        def execute_arm(app, plan, image, experiment, run_dir, arm):
            if arm == "candidate":
                raise KeyboardInterrupt()
            return {"result": {"metric": 1}, "exit_code": 0, "error": None,
                    "timed_out": False}
        with patch.object(execution, "tree_hash", return_value=store.exp["source_tree"]), \
             patch.object(execution, "image_id", return_value="image-id"), \
             patch.object(execution, "run_arm", side_effect=execute_arm):
            with self.assertRaises(KeyboardInterrupt):
                execution.run_experiment(FakeApp(store), "job", "experiment")
        stored = next(iter(store.saved.values()))
        self.assertEqual(stored["status"], "INTERRUPTED")
        self.assertIn("baseline", stored["arms"])
        self.assertNotIn("candidate", stored["arms"])


if __name__ == "__main__":
    unittest.main()
