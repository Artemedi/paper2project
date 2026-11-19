"""Offline regression tests covering all security and correctness audit fixes."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from email.message import Message
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import package_release  # noqa: E402
from paper2project import cli, core, execution, ingest, mcp, model, pdf, store  # noqa: E402
from paper2project.core import App  # noqa: E402


def _http_response(body, content_type, url):
    headers = Message()
    headers["Content-Type"] = content_type
    response = io.BytesIO(body)
    response.headers = headers
    response.url = url
    return response


def _setup_findings_workspace(root, *, with_git_and_ref=False, asset_names=("asset1.txt", "asset2.txt")):
    """Build a project + research + findings_only job fixture ready for experiments/reviews."""
    app = App(root / "workspace")
    project_md = root / "project.md"
    project_md.write_text("# Project\n\nWe serve requests with an LRU prefix cache.\n", encoding="utf-8")
    paper_md = root / "paper.md"
    paper_md.write_text("# Paper\n\nA fast cache index improves prefix hit rate.\n", encoding="utf-8")

    asset1 = root / asset_names[0]
    asset1.write_text("Primary observation of cache eviction behavior.\n", encoding="utf-8")
    asset2 = root / asset_names[1]
    asset2.write_text("Independent benchmark confirming cache latency.\n", encoding="utf-8")

    source = root / "source"
    source.mkdir()
    (source / "code.py").write_text("# fixture\n", encoding="utf-8")
    if with_git_and_ref:
        (source / ".git").mkdir()
        (source / ".git" / "HEAD").write_text("a" * 40 + "\n", encoding="utf-8")

    inputs = root / "inputs"
    inputs.mkdir()
    (inputs / "trace.csv").write_text("req,hit\n1,1\n", encoding="utf-8")

    p, = app.import_projects(project_md)
    p = app.attach_asset(p["id"], asset1, name=asset_names[0])
    p = app.attach_asset(p["id"], asset2, name=asset_names[1])
    if with_git_and_ref:
        source_tree = execution.tree_hash(source)
        input_tree = execution.tree_hash(inputs)
        ref_file = root / "ref.json"
        ref_file.write_text(json.dumps({
            "schema": 1,
            "method": "independent offline trace replay",
            "source_tree": source_tree["sha256"],
            "input_tree": input_tree["sha256"],
            "expected": {
                "throughput": {"baseline": 10, "candidate": 20},
                "errors": {"baseline": 0, "candidate": 0},
            },
        }), encoding="utf-8")
        p = app.attach_asset(p["id"], ref_file, name="ref.json")

    assets = app.store.json(p["snapshot"])["assets"]
    r = app.import_research(paper_md)
    job = app.create_job(r["id"], [p["id"]])
    analysis = {
        "summary": "Scoped applicability findings.",
        "assessments": [{
            "project_id": p["id"],
            "finding_schema": 1,
            "rationale": "Only the prefix cache path applies.",
            "citations": [
                {"document_id": f"project:{p['id']}:project.md", "start": 3, "end": 3,
                 "quote": "We serve requests with an LRU prefix cache."},
                {"document_id": f"research:{r['id']}:paper.md", "start": 3, "end": 3,
                 "quote": "A fast cache index improves prefix hit rate."},
            ],
            "findings": [{
                "id": "f1",
                "target": f"prefix cache in {asset_names[0]}",
                "claim": "index speeds up lookups",
                "premise": f"project uses LRU cache per {asset_names[0]}",
                "falsifier": f"throughput does not improve for {asset_names[0]}",
                "proposed": "candidate",
                "research_citations": [
                    {"document_id": f"research:{r['id']}:paper.md", "start": 3, "end": 3,
                     "quote": "A fast cache index improves prefix hit rate."},
                ],
                "project_artifacts": [{
                    "name": asset_names[0],
                    "sha256": assets[asset_names[0]]["sha256"],
                    "start": 1,
                    "end": 1,
                    "quote": "Primary observation of cache eviction behavior.",
                }],
            }],
        }],
    }
    app.submit_analysis(job["id"], analysis, author="analyst-alice")
    return app, p, r, job, assets, source, inputs


class StoreAuditTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_snapshots_symlink_rejected_on_store_init(self):
        ws = self.root / "ws"
        ws.mkdir()
        target = self.root / "target"
        target.mkdir()
        (ws / "snapshots").symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            store.Store(ws)

    def test_blob_rejects_symlink_and_fifo(self):
        st = store.Store(self.root / "ws")
        payload = b"trusted payload"
        key = store.digest(payload)
        outside = self.root / "outside"
        outside.write_bytes(payload)
        (st.root / "objects" / key).symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "Missing or unsafe object"):
            st.blob(key)

        fifo_key = store.digest(b"fifo-object")
        os.mkfifo(st.root / "objects" / fifo_key)
        with self.assertRaisesRegex(ValueError, "Missing or unsafe object"):
            st.blob(fifo_key)

    def test_get_and_list_enforce_id_match_for_entities_only(self):
        for kind in ("project", "research", "job", "experiment", "run"):
            with self.subTest(kind=kind):
                st = store.Store(self.root / f"ws-{kind}")
                st.save(kind, "rec-1", {"id": "rec-other"})
                with self.assertRaisesRegex(ValueError, f"{kind.capitalize()} record index disagrees with its body: rec-1"):
                    st.get(kind, "rec-1")
                with self.assertRaisesRegex(ValueError, f"{kind.capitalize()} record index disagrees with its body: rec-1"):
                    st.list(kind)

        st = store.Store(self.root / "ws-events")
        st.save("event", "evt-1", {"job_id": "job-1", "kind": "note"})
        st.save("decision", "dec-1", {"job_id": "job-1", "project_id": "proj-1"})
        self.assertEqual(st.get("event", "evt-1")["kind"], "note")
        self.assertEqual(len(st.list("event")), 1)
        self.assertEqual(st.get("decision", "dec-1")["project_id"], "proj-1")
        self.assertEqual(len(st.list("decision")), 1)


class PdfIngestAuditTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_pdf_main_suppresses_warnings_preserving_public_errors(self):
        src_dir = str(Path(pdf.__file__).resolve().parents[1])
        script = (
            "import sys, types, warnings\n"
            f"sys.path.insert(0, {src_dir!r})\n"
            "from paper2project import pdf\n"
            "fake = types.ModuleType('pypdf')\n"
            "def fake_reader(_stream):\n"
            "    warnings.warn('noisy pypdf deprecation warning')\n"
            "    raise ValueError('Encrypted research PDF is not supported')\n"
            "fake.PdfReader = fake_reader\n"
            "sys.modules['pypdf'] = fake\n"
            "raise SystemExit(pdf._main())\n"
        )
        result = subprocess.run(
            [sys.executable, "-I", "-Wdefault", "-c", script],
            input=b"%PDF-1.4\n",
            capture_output=True,
            timeout=10,
            env={},
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, b"Encrypted research PDF is not supported")

    def test_import_research_leaves_no_orphan_blob_on_failure(self):
        app = App(self.root / "ws-pdf")
        bad_pdf = self.root / "bad.pdf"
        bad_pdf.write_bytes(b"%PDF-1.4\nbroken")
        with patch.object(ingest, "extract_pdf", side_effect=ValueError("PDF extraction failed")):
            with self.assertRaisesRegex(ValueError, "PDF extraction failed"):
                app.import_research(bad_pdf)
        self.assertEqual(list((app.store.root / "objects").iterdir()), [])
        self.assertEqual(app.store.list("research"), [])

        app2 = App(self.root / "ws-utf8")
        bad_utf8 = self.root / "bad.md"
        bad_utf8.write_bytes(b"\xff\xfe\x80not-utf8")
        with self.assertRaises(UnicodeDecodeError):
            app2.import_research(bad_utf8)
        self.assertEqual(list((app2.store.root / "objects").iterdir()), [])
        self.assertEqual(app2.store.list("research"), [])

    def test_check_address_accepts_uppercase_allowlisted_hostnames(self):
        self.assertIsNone(ingest.check_address("198.18.0.1", "ARXIV.ORG"))
        self.assertIsNone(ingest.check_address("198.18.0.1", "GitHub.COM"))
        with self.assertRaisesRegex(ValueError, "Private research network"):
            ingest.check_address("198.18.0.1", "EVIL.EXAMPLE")

    def test_redirected_pdf_url_returning_html_is_rejected(self):
        app = App(self.root / "ws")
        response = _http_response(b"<html>CAPTCHA</html>", "text/html", "https://example.org/final.pdf")
        opener = Mock(open=Mock(return_value=response))
        with patch.object(ingest, "public_url"), \
             patch.object(ingest.urllib.request, "build_opener", return_value=opener):
            with self.assertRaisesRegex(ValueError, "non-PDF"):
                app.import_research("https://example.org/paper")
        self.assertEqual(app.store.list("research"), [])
        self.assertEqual(list((app.store.root / "objects").iterdir()), [])

    def test_import_reviewed_package_rejects_nan_in_receipt(self):
        package = self.root / "pkg"
        (package / "references").mkdir(parents=True)
        paper_bytes = b"# Paper\n\nComplete reviewed text.\n"
        (package / "references" / "paper.md").write_bytes(paper_bytes)
        source_file = self.root / "source.pdf"
        source_bytes = b"%PDF-1.4\nsource"
        source_file.write_bytes(source_bytes)

        inventory = {"references/paper.md": store.digest(paper_bytes)}
        sources = [(str(source_file), "main", store.digest(source_bytes))]

        def build_qualification(verification_obj):
            captured = {
                "verification.json": json.dumps(verification_obj, allow_nan=True).encode(),
                "source-manifest": json.dumps({
                    "full_text_status": "COMPLETE",
                    "sources": [{"role": "main", "sha256": store.digest(source_bytes)}],
                }).encode(),
                "build.json": json.dumps({"files": inventory}).encode(),
            }
            records = {name: store.digest(captured[name]) for name in ("verification.json", "source-manifest", "build.json")}
            fingerprint = store.digest(json.dumps(records, sort_keys=True).encode())
            captured["independent-review.json"] = json.dumps({
                "decision": "PASS",
                "reviewer": "alice",
                "fingerprint": fingerprint,
            }).encode()
            return {
                "captured": captured,
                "records": records,
                "fingerprint": fingerprint,
                "verification_status": "reviewed",
                "limitations": [],
            }

        app = App(self.root / "ws-pkg")
        bad_qual = build_qualification({
            "status": "reviewed",
            "mechanical_ok": True,
            "all_sources_agent_reviewed": True,
            "score": float("nan"),
        })
        with self.assertRaisesRegex(ValueError, "Invalid qualified Paper2Agent receipt"):
            app.import_reviewed_package(package, "paper-1", "Paper 1", None, bad_qual, inventory, sources)

        good_qual = build_qualification({
            "status": "reviewed",
            "mechanical_ok": True,
            "all_sources_agent_reviewed": True,
        })
        rec = app.import_reviewed_package(package, "paper-1", "Paper 1", None, good_qual, inventory, sources)
        self.assertEqual(rec["id"], "paper-1")


class ModelCliAuditTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def _stale_job_app(self):
        app = App(self.root / "ws")
        proj = self.root / "proj.md"
        paper = self.root / "paper.md"
        proj.write_text("# Project\n\nOriginal cache service.\n", encoding="utf-8")
        paper.write_text("# Paper\n\nNew cache method.\n", encoding="utf-8")
        p, = app.import_projects(proj)
        r = app.import_research(paper)
        job = app.create_job(r["id"], [p["id"]])
        proj.write_text("# Project\n\nUpdated cache service.\n", encoding="utf-8")
        app.import_projects(proj)
        return app, p, r, job

    def test_endpoint_rejects_query_fragment_and_whitespace_fields(self):
        for bad_url in (
            "https://api.example.invalid/v1?key=1",
            "https://api.example.invalid/v1#frag",
            "https://api.example.invalid/v1?",
            "https://api.example.invalid/v1#",
        ):
            with self.subTest(url=bad_url), self.assertRaisesRegex(ValueError, "query or fragment"):
                model.Endpoint(bad_url, "test-model")
        for bad_model in ("", "   ", "\t\n"):
            with self.subTest(model=bad_model), self.assertRaisesRegex(ValueError, "model name"):
                model.Endpoint("https://api.example.invalid/v1", bad_model)
        for bad_key_env in ("", "   ", "\n"):
            with self.subTest(key_env=bad_key_env), self.assertRaisesRegex(ValueError, "key environment"):
                model.Endpoint("https://api.example.invalid/v1", "test-model", key_env=bad_key_env)

    def test_endpoint_complete_preserves_redirect_refused(self):
        endpoint = model.Endpoint("https://api.example.invalid/v1", "test-model")

        def open_redirect(request, timeout=None):
            return model.NoRedirect().redirect_request(
                request, None, 302, "Found", Message(), "https://other.invalid/v1"
            )

        with patch.object(model.urllib.request, "build_opener", return_value=Mock(open=open_redirect)):
            with self.assertRaisesRegex(ValueError, r"HTTP 302 \(redirect refused\)") as ctx:
                endpoint.complete([{"role": "user", "content": "hi"}])
        if ctx.exception.__cause__ is not None:
            ctx.exception.__cause__.close()

    def test_parse_json_strips_uppercase_fence(self):
        self.assertEqual(model.parse_json('```JSON\n{"summary": "ok"}\n```'), {"summary": "ok"})

    def test_analyze_refuses_stale_job_before_complete_and_on_send(self):
        app, _, _, job = self._stale_job_app()
        endpoint = Mock()
        endpoint.complete = Mock(side_effect=AssertionError("complete must not be called"))
        on_send = Mock(side_effect=AssertionError("on_send must not be called"))
        with self.assertRaisesRegex(ValueError, "Job is stale"):
            model.analyze(app, job["id"], endpoint, on_send=on_send)
        endpoint.complete.assert_not_called()
        on_send.assert_not_called()

    def test_analyze_handles_none_reply_on_retry(self):
        app = App(self.root / "ws")
        proj = self.root / "proj.md"
        paper = self.root / "paper.md"
        proj.write_text("# Project\n\nA cache service.\n", encoding="utf-8")
        paper.write_text("# Paper\n\nA cache method.\n", encoding="utf-8")
        p, = app.import_projects(proj)
        r = app.import_research(paper)
        job = app.create_job(r["id"], [p["id"]])

        valid_reply = json.dumps({
            "summary": "Valid after None retry.",
            "assessments": [{
                "project_id": p["id"],
                "relevance": "candidate",
                "rationale": "Both involve caches.",
                "citations": [
                    {"document_id": f"project:{p['id']}:proj.md", "start": 3, "end": 3, "quote": "A cache service."},
                    {"document_id": f"research:{r['id']}:paper.md", "start": 3, "end": 3, "quote": "A cache method."},
                ],
            }],
        })
        seen_messages = []

        class RetryEndpoint:
            host = "api.example.invalid"
            model = "test-model"

            def __init__(self):
                self.calls = 0

            def complete(self, messages):
                seen_messages.append([dict(m) for m in messages])
                self.calls += 1
                return None if self.calls == 1 else valid_reply

        endpoint = RetryEndpoint()
        status = model.analyze(app, job["id"], endpoint)
        self.assertEqual(status["state"], "ANALYZED")
        self.assertEqual(endpoint.calls, 2)
        self.assertEqual(seen_messages[1][-2], {"role": "assistant", "content": ""})
        self.assertIn("Model reply has no text content", seen_messages[1][-1]["content"])

        exhausted = Mock(host="api.example.invalid", model="test-model", complete=Mock(return_value=None))
        with self.assertRaisesRegex(ValueError, "Model reply has no text content"):
            model.analyze(app, job["id"], exhausted, attempts=2)

    def test_cli_refuses_nan_json_files_and_stale_analyze_is_silent_on_send(self):
        app, _, _, stale_job = self._stale_job_app()
        nan_file = self.root / "nan.json"
        nan_file.write_text('{"summary": "bad", "score": NaN}', encoding="utf-8")

        for argv in (
            ["--workspace", str(app.store.root), "submit-analysis", stale_job["id"], str(nan_file)],
            ["--workspace", str(app.store.root), "review-finding", stale_job["id"], str(nan_file)],
            ["--workspace", str(app.store.root), "experiment", "add", stale_job["id"], str(nan_file)],
        ):
            with self.subTest(command=argv[2]):
                err = io.StringIO()
                with redirect_stderr(err):
                    rc = cli.main(argv)
                self.assertEqual(rc, 1)
                self.assertIn("Nonfinite JSON value", err.getvalue())

        err = io.StringIO()
        with patch.object(model.Endpoint, "complete", side_effect=AssertionError("must not call")), \
             redirect_stderr(err):
            rc = cli.main([
                "--workspace", str(app.store.root),
                "analyze", stale_job["id"],
                "--base-url", "https://api.example.invalid/v1",
                "--model", "test-model",
                "--yes-send",
            ])
        self.assertEqual(rc, 1)
        self.assertIn("Job is stale", err.getvalue())
        self.assertNotIn("Sending snapshot text", err.getvalue())


class ExecutionAuditTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_tree_hash_and_copy_tree_allow_symlinked_parent_only(self):
        real_parent = self.root / "var_home"
        real_parent.mkdir()
        src = real_parent / "repo"
        src.mkdir()
        (src / "file.txt").write_text("content", encoding="utf-8")

        parent_link = self.root / "home_alias"
        parent_link.symlink_to(real_parent, target_is_directory=True)
        aliased_src = parent_link / "repo"

        expected = execution.tree_hash(aliased_src)
        copied = execution.copy_tree(aliased_src, parent_link / "copied", expected)
        self.assertEqual(execution.tree_hash(copied), expected)

        root_link = self.root / "root_link"
        root_link.symlink_to(src, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "real directory"):
            execution.tree_hash(root_link)
        with self.assertRaisesRegex(ValueError, "real directory"):
            execution.copy_tree(root_link, self.root / "copied_root_link", expected)

        (src / "inner_link").symlink_to(src / "file.txt")
        with self.assertRaisesRegex(ValueError, "Symlink in experiment tree"):
            execution.tree_hash(src)
        with self.assertRaisesRegex(ValueError, "Symlink or special file"):
            execution.copy_tree(src, self.root / "copied_inner", expected)
        self.assertFalse((self.root / "copied_inner").exists())

    def test_matches_and_validate_plan_exact_tolerance_boundary(self):
        self.assertTrue(execution.matches(0.4, 0.1, "abs", 0.3))
        self.assertFalse(execution.matches(0.41, 0.1, "abs", 0.3))

        def make_plan(comparison, tolerance, *, baseline=0.1, candidate=0.4, min_delta=0.3):
            return {
                "kind": "project_benefit",
                "project_id": "p",
                "image": "local/test",
                "source": {"path": "/tmp/src", "revision": "a" * 40, "license": "MIT"},
                "input": {"path": "/tmp/in"},
                "arms": {"baseline": ["base"], "candidate": ["cand"]},
                "metrics": [
                    {"name": "score", "path": "score", "comparison": comparison, "tolerance": tolerance,
                     "expected": {"baseline": baseline, "candidate": candidate}},
                    {"name": "errors", "path": "errors", "expected": {"candidate": 0}},
                ],
                "benefit": {
                    "metric": "score",
                    "direction": "increase",
                    "min_delta": min_delta,
                    "guards": [{"metric": "errors", "op": "le", "value": 0}],
                },
            }

        with self.assertRaisesRegex(ValueError, "below half of min_delta"):
            execution.validate_plan(make_plan("abs", 0.15, min_delta=0.3), {"p"})
        validated = execution.validate_plan(make_plan("abs", 0.1, min_delta=0.3), {"p"})
        self.assertEqual(validated["metrics"][0]["tolerance"], 0.1)

        with self.assertRaisesRegex(ValueError, "below half of min_delta"):
            execution.validate_plan(make_plan("rel", 0.375, baseline=0.1, candidate=0.4, min_delta=0.3), {"p"})
        validated_rel = execution.validate_plan(make_plan("rel", 0.37, baseline=0.1, candidate=0.4, min_delta=0.3), {"p"})
        self.assertEqual(validated_rel["metrics"][0]["tolerance"], 0.37)

    def test_add_experiment_cleans_up_readonly_snapshot_dir_on_failure(self):
        app = App(self.root / "ws")
        proj = self.root / "proj.md"
        paper = self.root / "paper.md"
        proj.write_text("# Project\n\nA cache service.\n", encoding="utf-8")
        paper.write_text("# Paper\n\nA cache method.\n", encoding="utf-8")
        p, = app.import_projects(proj)
        r = app.import_research(paper)
        job = app.create_job(r["id"], [p["id"]])
        app.submit_analysis(job["id"], {
            "summary": "fixture",
            "assessments": [{
                "project_id": p["id"],
                "relevance": "candidate",
                "rationale": "fixture",
                "citations": [
                    {"document_id": f"project:{p['id']}:proj.md", "start": 3, "end": 3, "quote": "A cache service."},
                    {"document_id": f"research:{r['id']}:paper.md", "start": 3, "end": 3, "quote": "A cache method."},
                ],
            }],
        })

        source = self.root / "source"
        ro_dir = source / "ro_sub"
        ro_dir.mkdir(parents=True)
        (ro_dir / "code.py").write_text("print(1)\n", encoding="utf-8")
        inputs = self.root / "inputs"
        inputs.mkdir()
        (inputs / "data.txt").write_text("x\n", encoding="utf-8")

        os.chmod(ro_dir, 0o555)
        self.addCleanup(lambda: ro_dir.exists() and os.chmod(ro_dir, 0o755))

        plan = {
            "kind": "reproduction",
            "project_id": p["id"],
            "image": "local/fixture",
            "source": {"path": str(source), "revision": "a" * 40, "license": "MIT"},
            "input": {"path": str(inputs)},
            "arms": {"baseline": ["true"], "candidate": ["false"]},
            "metrics": [{"name": "m", "path": "m", "expected": {"candidate": 1}}],
        }

        with patch.object(execution, "git_head", side_effect=["a" * 40, "b" * 40]):
            with self.assertRaisesRegex(ValueError, "Source Git revision changed"):
                app.add_experiment(job["id"], plan)
        self.assertEqual(list((app.store.root / "snapshots").iterdir()), [])

        real_copy = execution.copy_tree

        def fail_second_copy(src_path, dst_path, expected):
            if dst_path.name == "input":
                raise OSError("disk error during input copy")
            return real_copy(src_path, dst_path, expected)

        with patch.object(execution, "git_head", return_value="a" * 40), \
             patch.object(execution, "copy_tree", side_effect=fail_second_copy):
            with self.assertRaisesRegex(OSError, "disk error during input copy"):
                app.add_experiment(job["id"], plan)
        self.assertEqual(list((app.store.root / "snapshots").iterdir()), [])

    def test_git_head_ignores_comments_peeled_lines_and_suffix_collisions(self):
        repo = self.root / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (repo / ".git" / "packed-refs").write_text(
            "\n".join([
                "# pack-refs with: peeled fully-peeled sorted",
                "# " + ("9" * 40) + " refs/heads/main",
                "^" + ("8" * 40) + " refs/heads/main",
                ("1" * 40) + " refs/heads/prefix/refs/heads/main",
                ("2" * 40) + " refs/heads/main",
                "^" + ("3" * 40),
                ("4" * 40) + " refs/heads/main-extra",
            ]) + "\n",
            encoding="utf-8",
        )
        self.assertEqual(execution.git_head(repo), "2" * 40)

        (repo / ".git" / "packed-refs").write_text(
            "\n".join([
                "# " + ("9" * 40) + " refs/heads/main",
                "^" + ("8" * 40) + " refs/heads/main",
                ("1" * 40) + " refs/heads/prefix/refs/heads/main",
                ("4" * 40) + " refs/heads/main-extra",
            ]) + "\n",
            encoding="utf-8",
        )
        self.assertIsNone(execution.git_head(repo))

    def test_run_arm_closes_write_fd_before_remove_container(self):
        app = App(self.root / "ws")
        source = self.root / "source"
        source.mkdir()
        run_id = "run-cleanup"
        run_dir = app.store.root / "runs" / run_id
        run_dir.mkdir(parents=True)
        app.store.save("run", run_id, {"id": run_id})

        real_pipe = os.pipe
        real_close = os.close
        tracked_write_fds = []
        events = []

        def track_pipe():
            read_fd, write_fd = real_pipe()
            tracked_write_fds.append(write_fd)
            return read_fd, write_fd

        def track_close(fd):
            if fd in tracked_write_fds:
                events.append("close_write_fd")
            return real_close(fd)

        def fail_remove(name):
            events.append("remove_container")
            raise execution.CleanupPending("Unable to confirm container removal")

        fake_proc = Mock(wait=Mock(return_value=1), returncode=1)
        plan = {"limits": execution.LIMITS, "arms": {"baseline": ["false"]}}
        exp = {"source_path": str(source), "input_path": None}

        with app.store.writer(), \
             patch.object(execution, "podman_command", return_value=["podman"]), \
             patch.object(execution.subprocess, "Popen", return_value=fake_proc), \
             patch.object(execution, "remove_container", side_effect=fail_remove), \
             patch.object(execution, "stop_container") as stop_mock, \
             patch.object(execution.os, "pipe", side_effect=track_pipe), \
             patch.object(execution.os, "close", side_effect=track_close):
            with self.assertRaises(execution.CleanupPending):
                execution.run_arm(app, plan, "img-id", exp, run_dir, "baseline")

        stop_mock.assert_not_called()
        self.assertEqual(events, ["close_write_fd", "remove_container"])

    def test_collect_preserves_first_error_across_breaches(self):
        stdout = self.root / "stdout"
        stderr = self.root / "stderr"
        stdout.touch()
        stderr.touch()

        # Case 1: special file in root before oversized (>64 MiB) file in sub/
        out1 = self.root / "out1"
        (out1 / "sub").mkdir(parents=True)
        os.mkfifo(out1 / "fifo")
        with open(out1 / "sub" / "big.bin", "wb") as fh:
            fh.seek(execution.MAX_OUTPUT_FILE)
            fh.write(b"x")
        rec1 = {"exit_code": 0, "error": None, "result": None}
        execution.collect(rec1, out1, stdout, stderr)
        self.assertIn("symlink or special file: fifo", rec1["error"])
        self.assertNotIn("exceeds", rec1["error"])

        # Case 2: special file in root before >1000 entry breach in sub/
        out2 = self.root / "out2"
        (out2 / "sub").mkdir(parents=True)
        os.mkfifo(out2 / "fifo")
        for idx in range(execution.MAX_OUTPUT_ENTRIES + 5):
            (out2 / "sub" / f"f{idx}").write_bytes(b"")
        rec2 = {"exit_code": 0, "error": None, "result": None}
        execution.collect(rec2, out2, stdout, stderr)
        self.assertIn("symlink or special file: fifo", rec2["error"])
        self.assertNotIn("more than 1000", rec2["error"])

        # Case 3: oversized (>64 MiB) file in root before special file in sub/
        out3 = self.root / "out3"
        (out3 / "sub").mkdir(parents=True)
        with open(out3 / "big.bin", "wb") as fh:
            fh.seek(execution.MAX_OUTPUT_FILE)
            fh.write(b"x")
        os.mkfifo(out3 / "sub" / "fifo")
        rec3 = {"exit_code": 0, "error": None, "result": None}
        execution.collect(rec3, out3, stdout, stderr)
        self.assertIn("Output file big.bin exceeds", rec3["error"])
        self.assertNotIn("symlink or special file", rec3["error"])

    def test_outcome_for_job_findings_only_lifecycle_progression(self):
        app, p, _, job, assets, source, inputs = _setup_findings_workspace(self.root, with_git_and_ref=True)
        plan = {
            "kind": "project_benefit",
            "project_id": p["id"],
            "image": "local/fixture",
            "finding_id": "f1",
            "reference": "ref.json",
            "provenance": {
                "oracle": {
                    "name": "asset1.txt",
                    "sha256": assets["asset1.txt"]["sha256"],
                    "start": 1,
                    "end": 1,
                    "quote": "Primary observation of cache eviction behavior.",
                },
                "data": "offline trace",
                "comparison": "paired throughput",
            },
            "source": {"path": str(source), "revision": "a" * 40, "license": "MIT"},
            "input": {"path": str(inputs)},
            "arms": {"baseline": ["run", "base"], "candidate": ["run", "cand"]},
            "metrics": [
                {"name": "throughput", "path": "throughput", "expected": {"baseline": 10, "candidate": 20}},
                {"name": "errors", "path": "errors", "expected": {"baseline": 0, "candidate": 0}},
            ],
            "benefit": {
                "metric": "throughput",
                "direction": "increase",
                "min_delta": 5,
                "guards": [{"metric": "errors", "op": "le", "value": 0}],
            },
        }
        exp = app.add_experiment(job["id"], plan)

        def fake_arm(app_obj, _plan, _image, _exp, _dir, arm):
            res = {"throughput": 10 if arm == "baseline" else 20, "errors": 0}
            return {"result": res, "result_sha256": app_obj.store.put_json(res),
                    "exit_code": 0, "error": None, "timed_out": False}

        with patch.object(execution, "image_id", return_value="img-123"), \
             patch.object(execution, "run_arm", side_effect=fake_arm):
            run = app.run_experiment(job["id"], exp["id"])
        self.assertEqual(run["status"], "PASS")
        self.assertTrue(run["benefit"]["met"])

        # Step 1: Experiment passes, but finding lacks scoped review
        out1 = execution.outcome_for_job(app, app.status(job["id"]))[0]
        self.assertEqual(out1["decision"], "FINDINGS_PENDING_REVIEW")

        # Step 2: Scoped finding review supports premise and test link -> BENEFIT_PENDING_REVIEW
        blind_pkt = app.review_packet(job["id"], p["id"], blind=True)
        review_res = app.review_finding(job["id"], p["id"], {
            "finding_id": "f1",
            "evidence": blind_pkt["finding_evidence"]["f1"],
            "reviewer": "reviewer-bob",
            "premise": "supported",
            "test_link": "relevant",
            "experiment_id": exp["id"],
            "notes": "Premise and test link independently confirmed.",
            "second_observation": {
                "name": "asset2.txt",
                "sha256": assets["asset2.txt"]["sha256"],
                "start": 1,
                "end": 1,
                "quote": "Independent benchmark confirming cache latency.",
            },
            "independence_basis": "Separate benchmark measurement.",
        })
        self.assertEqual(review_res["status"], "REVIEWED_APPLICABILITY_SUPPORTED")
        out2 = execution.outcome_for_job(app, app.status(job["id"]))[0]
        self.assertEqual(out2["decision"], "BENEFIT_PENDING_REVIEW")

        # Step 3: Passing project review without verified_experiments -> BENEFIT_CRITERIA_MET
        pkt = app.review_packet(job["id"], p["id"])
        app.review_analysis(job["id"], "pass", "Criteria met.", "reviewer-bob", pkt["analysis"],
                            project_id=p["id"], evidence=pkt["evidence"])
        out3 = execution.outcome_for_job(app, app.status(job["id"]))[0]
        self.assertEqual(out3["decision"], "BENEFIT_CRITERIA_MET")

        # Step 4: Passing project review with verified_experiments -> BENEFIT_DEMONSTRATED
        app.review_analysis(job["id"], "pass", "Reference attested.", "reviewer-bob", pkt["analysis"],
                            project_id=p["id"], evidence=pkt["evidence"], verified_experiments=[exp["id"]])
        out4 = execution.outcome_for_job(app, app.status(job["id"]))[0]
        self.assertEqual(out4["decision"], "BENEFIT_DEMONSTRATED")


class CoreAuditTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_export_redactor_longest_first_and_scoped_fields_in_export(self):
        redactor = core.export_redactor(self.root, [], [{
            "documents": [],
            "assets": {"alpha": {"sha256": "a" * 64}, "alpha+beta": {"sha256": "b" * 64}},
        }])
        redacted = redactor({"summary": "Used alpha+beta and alpha"})
        self.assertEqual(redacted["summary"], "Used local-name:redacted and local-name:redacted")

        secret_oracle = "secret-oracle-artifact.txt"
        app, p, _, job, assets, source, inputs = _setup_findings_workspace(
            self.root, with_git_and_ref=False, asset_names=(secret_oracle, "secret-second.txt")
        )
        secret_input = "secret-input-trace.csv"
        (inputs / secret_input).write_text("row\n1\n", encoding="utf-8")

        exp = app.add_experiment(job["id"], {
            "kind": "reproduction",
            "project_id": p["id"],
            "image": "local/fixture",
            "finding_id": "f1",
            "provenance": {
                "oracle": {
                    "name": secret_oracle,
                    "sha256": assets[secret_oracle]["sha256"],
                    "start": 1,
                    "end": 1,
                    "quote": "Primary observation of cache eviction behavior.",
                },
                "data": f"Derived from {secret_input}",
                "comparison": f"Compared against {secret_oracle}",
            },
            "source": {"path": str(source), "revision": "a" * 40, "license": "MIT"},
            "input": {"path": str(inputs)},
            "arms": {"baseline": ["base"], "candidate": ["cand"]},
            "metrics": [{"name": "m", "path": "m", "expected": {"candidate": 1}}],
        })

        dest = self.root / "export.zip"
        app.export(job["id"], dest)
        with zipfile.ZipFile(dest) as archive:
            report = json.loads(archive.read("report.json"))
            decisions = json.loads(archive.read("decisions.json"))
            exp_export = json.loads(archive.read(f"experiments/{exp['id']}.json"))

        finding = report["analysis"]["assessments"][0]["findings"][0]
        for field in ("target", "premise", "falsifier"):
            self.assertNotIn(secret_oracle, finding[field])
            self.assertIn("local-name:redacted", finding[field])
        self.assertNotIn(secret_oracle, report["projects"][0]["findings"][0]["target"])
        self.assertNotIn(secret_oracle, decisions[0]["findings"][0]["target"])
        self.assertIn("local-name:redacted", decisions[0]["findings"][0]["target"])
        provenance = exp_export["plan"]["provenance"]
        self.assertNotIn(secret_input, provenance["data"])
        self.assertIn("local-name:redacted", provenance["data"])
        self.assertNotIn(secret_oracle, provenance["comparison"])
        self.assertIn("local-name:redacted", provenance["comparison"])

    def test_review_finding_populates_plan_hash_and_exports_in_finding_reviews(self):
        app, p, _, job, assets, source, inputs = _setup_findings_workspace(self.root)
        exp = app.add_experiment(job["id"], {
            "kind": "reproduction",
            "project_id": p["id"],
            "image": "local/fixture",
            "finding_id": "f1",
            "provenance": {
                "oracle": {
                    "name": "asset1.txt",
                    "sha256": assets["asset1.txt"]["sha256"],
                    "start": 1,
                    "end": 1,
                    "quote": "Primary observation of cache eviction behavior.",
                },
                "data": "trace",
                "comparison": "baseline vs candidate",
            },
            "source": {"path": str(source), "revision": "a" * 40, "license": "MIT"},
            "input": {"path": str(inputs)},
            "arms": {"baseline": ["base"], "candidate": ["cand"]},
            "metrics": [{"name": "m", "path": "m", "expected": {"candidate": 1}}],
        })

        pkt = app.review_packet(job["id"], p["id"], blind=True)
        res = app.review_finding(job["id"], p["id"], {
            "finding_id": "f1",
            "evidence": pkt["finding_evidence"]["f1"],
            "reviewer": "reviewer-bob",
            "premise": "supported",
            "test_link": "relevant",
            "experiment_id": exp["id"],
            "notes": "Confirmed with experiment_id only.",
        })
        stored = app.store.json(res["review_hash"])
        self.assertEqual(stored["experiment_id"], exp["id"])
        self.assertEqual(stored["plan_hash"], exp["plan"])

        dest = self.root / "export.zip"
        app.export(job["id"], dest)
        with zipfile.ZipFile(dest) as archive:
            reviews = json.loads(archive.read("finding-reviews.json"))
        exported = reviews[res["review_hash"]]
        self.assertEqual(exported["experiment_id"], exp["id"])
        self.assertEqual(exported["plan_hash"], exp["plan"])

    def test_review_finding_rejects_whitespace_padded_duplicate_quote(self):
        app = App(self.root / "ws")
        proj = self.root / "project.md"
        paper = self.root / "paper.md"
        asset1 = self.root / "asset1.txt"
        padded_file = self.root / "padded.txt"
        proj.write_text("# Project\n\nWe serve requests with an LRU prefix cache.\n", encoding="utf-8")
        paper.write_text("# Paper\n\nA fast cache index improves prefix hit rate.\n", encoding="utf-8")
        asset1.write_text("Primary observation of cache eviction behavior.\n", encoding="utf-8")
        padded_file.write_text("  Primary observation of cache eviction behavior.  \n", encoding="utf-8")

        p, = app.import_projects(proj)
        p = app.attach_asset(p["id"], asset1, name="asset1.txt")
        p = app.attach_asset(p["id"], padded_file, name="padded.txt")
        assets = app.store.json(p["snapshot"])["assets"]
        r = app.import_research(paper)
        job = app.create_job(r["id"], [p["id"]])
        app.submit_analysis(job["id"], {
            "summary": "Scoped.",
            "assessments": [{
                "project_id": p["id"],
                "finding_schema": 1,
                "rationale": "Scoped.",
                "citations": [
                    {"document_id": f"project:{p['id']}:project.md", "start": 3, "end": 3,
                     "quote": "We serve requests with an LRU prefix cache."},
                    {"document_id": f"research:{r['id']}:paper.md", "start": 3, "end": 3,
                     "quote": "A fast cache index improves prefix hit rate."},
                ],
                "findings": [{
                    "id": "f1",
                    "target": "prefix cache",
                    "claim": "index helps",
                    "premise": "LRU used",
                    "falsifier": "no gain",
                    "proposed": "candidate",
                    "research_citations": [
                        {"document_id": f"research:{r['id']}:paper.md", "start": 3, "end": 3,
                         "quote": "A fast cache index improves prefix hit rate."},
                    ],
                    "project_artifacts": [{
                        "name": "asset1.txt",
                        "sha256": assets["asset1.txt"]["sha256"],
                        "start": 1,
                        "end": 1,
                        "quote": "Primary observation of cache eviction behavior.",
                    }],
                }],
            }],
        }, author="analyst-alice")

        pkt = app.review_packet(job["id"], p["id"], blind=True)
        with self.assertRaisesRegex(ValueError, "another attached artifact and different quoted evidence"):
            app.review_finding(job["id"], p["id"], {
                "finding_id": "f1",
                "evidence": pkt["finding_evidence"]["f1"],
                "reviewer": "reviewer-bob",
                "premise": "supported",
                "test_link": "unknown",
                "notes": "Trying padded duplicate quote.",
                "second_observation": {
                    "name": "padded.txt",
                    "sha256": assets["padded.txt"]["sha256"],
                    "start": 1,
                    "end": 1,
                    "quote": "  Primary observation of cache eviction behavior.  ",
                },
                "independence_basis": "Separate file.",
            })

    def test_status_detects_corrupted_and_deleted_finding_reviews_object(self):
        app, p, _, job, _, _, _ = _setup_findings_workspace(self.root)
        pkt = app.review_packet(job["id"], p["id"], blind=True)
        res = app.review_finding(job["id"], p["id"], {
            "finding_id": "f1",
            "evidence": pkt["finding_evidence"]["f1"],
            "reviewer": "reviewer-bob",
            "premise": "supported",
            "test_link": "unknown",
            "notes": "Premise holds.",
        })
        obj_path = app.store.root / "objects" / res["review_hash"]
        obj_path.write_bytes(b"{}")
        with self.assertRaisesRegex(ValueError, "Object integrity check failed"):
            app.status(job["id"])

        obj_path.unlink()
        with self.assertRaisesRegex(ValueError, "Missing or unsafe object"):
            app.status(job["id"])

    def test_attach_asset_idempotent_revision_and_rejects_non_string_name(self):
        app = App(self.root / "ws")
        proj = self.root / "proj.md"
        proj.write_text("# Project\n\nDetails.\n", encoding="utf-8")
        p, = app.import_projects(proj)
        asset = self.root / "data.txt"
        asset.write_text("alpha\n", encoding="utf-8")

        first = app.attach_asset(p["id"], asset, name="data.txt")
        self.assertEqual(first["revision"], 2)
        second = app.attach_asset(p["id"], asset, name="data.txt")
        self.assertEqual(second["revision"], 2)
        self.assertEqual(second["snapshot"], first["snapshot"])

        for bad_name in (123, ["data.txt"], {"name": "data.txt"}):
            with self.subTest(bad_name=bad_name), self.assertRaisesRegex(ValueError, "Unsafe asset name"):
                app.attach_asset(p["id"], asset, name=bad_name)

        asset.write_text("beta\n", encoding="utf-8")
        third = app.attach_asset(p["id"], asset, name="data.txt")
        self.assertEqual(third["revision"], 3)

    def test_create_job_raises_value_error_on_unhashable_project_ids(self):
        app = App(self.root / "ws")
        proj = self.root / "proj.md"
        paper = self.root / "paper.md"
        proj.write_text("# Project\n\nDetails.\n", encoding="utf-8")
        paper.write_text("# Paper\n\nMethod.\n", encoding="utf-8")
        app.import_projects(proj)
        r = app.import_research(paper)

        for bad_ids in ([{"id": "proj"}], [["proj"]]):
            with self.subTest(project_ids=bad_ids), self.assertRaisesRegex(ValueError, "Select 1..100 distinct projects"):
                app.create_job(r["id"], bad_ids)


class McpAuditTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_tools_list_schemas_hints_and_null_arguments(self):
        allowed = self.root / "allowed"
        allowed.mkdir()
        (allowed / "proj.md").write_text("# Project\n\nCache details.\n", encoding="utf-8")
        (allowed / "paper.md").write_text("# Paper\n\nCache method.\n", encoding="utf-8")
        app = App(self.root / "ws", allowed_roots=[allowed])
        p, = app.import_projects(allowed / "proj.md")
        r = app.import_research(allowed / "paper.md")
        job = app.create_job(r["id"], [p["id"]])
        listing = mcp.tools(app)

        listed_no_review = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                      app, listing, False, False, allow_review=False)["result"]["tools"]
        names_no_review = {t["name"]: t for t in listed_no_review}
        self.assertIn("project_attach", names_no_review)
        self.assertIn("jobs", names_no_review)
        self.assertNotIn("finding_review", names_no_review)
        self.assertNotIn("analysis_review", names_no_review)
        self.assertTrue(names_no_review["research_import"]["annotations"]["openWorldHint"])

        listed_with_review = mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                                        app, listing, False, False, allow_review=True)["result"]["tools"]
        names_with_review = {t["name"]: t for t in listed_with_review}
        self.assertIn("finding_review", names_with_review)
        self.assertIn("analysis_review", names_with_review)

        for entry in listing:
            for prop_name, prop_schema in entry["inputSchema"]["properties"].items():
                if prop_schema["type"] == "array":
                    self.assertEqual(prop_schema.get("items"), {"type": "string"},
                                     f"{entry['name']}.{prop_name} missing string items")

        job_create_schema = next(t for t in listing if t["name"] == "job_create")["inputSchema"]
        with self.assertRaisesRegex(ValueError, "Argument project_ids items must be string"):
            mcp.check(job_create_schema, {"research_id": r["id"], "project_ids": [123]})

        for tool_name, expected_id in (("projects", p["id"]), ("jobs", job["id"])):
            res = mcp.handle(
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": tool_name, "arguments": None}},
                app, listing, False, False,
            )["result"]
            self.assertFalse(res["isError"])
            payload = json.loads(res["content"][0]["text"])
            self.assertEqual([item["id"] for item in payload], [expected_id])

    def test_project_attach_allow_root_and_finding_review_gating(self):
        allowed = self.root / "allowed"
        outside = self.root / "outside"
        allowed.mkdir()
        outside.mkdir()
        (outside / "secret.txt").write_text("outside root\n", encoding="utf-8")

        app, p, _, job, assets, _, _ = _setup_findings_workspace(allowed)
        app.allowed_roots = [allowed.resolve()]
        listing = mcp.tools(app)

        pkt = app.review_packet(job["id"], p["id"], blind=True)
        review_args = {
            "job_id": job["id"],
            "project_id": p["id"],
            "review": {
                "finding_id": "f1",
                "evidence": pkt["finding_evidence"]["f1"],
                "reviewer": "reviewer-bob",
                "premise": "supported",
                "test_link": "unknown",
                "notes": "Premise holds under MCP review.",
                "second_observation": {
                    "name": "asset2.txt",
                    "sha256": assets["asset2.txt"]["sha256"],
                    "start": 1,
                    "end": 1,
                    "quote": "Independent benchmark confirming cache latency.",
                },
                "independence_basis": "Separate benchmark measurement.",
            },
        }
        refused = mcp.handle(
            {"jsonrpc": "2.0", "id": 10, "method": "tools/call",
             "params": {"name": "finding_review", "arguments": review_args}},
            app, listing, False, False, allow_review=False,
        )["result"]
        self.assertTrue(refused["isError"])
        self.assertIn("Review over MCP is disabled", refused["content"][0]["text"])

        accepted = mcp.handle(
            {"jsonrpc": "2.0", "id": 11, "method": "tools/call",
             "params": {"name": "finding_review", "arguments": review_args}},
            app, listing, False, False, allow_review=True,
        )["result"]
        self.assertFalse(accepted["isError"])
        self.assertEqual(json.loads(accepted["content"][0]["text"])["status"], "REVIEWED_PREMISE_SUPPORTED")

        blocked = mcp.handle(
            {"jsonrpc": "2.0", "id": 12, "method": "tools/call",
             "params": {"name": "project_attach", "arguments": {
                 "project_id": p["id"], "path": str(outside / "secret.txt")}}},
            app, listing, False, False,
        )["result"]
        self.assertTrue(blocked["isError"])
        self.assertIn("outside configured allow roots", blocked["content"][0]["text"])

        ok_file = allowed / "extra.txt"
        ok_file.write_text("allowed attachment\n", encoding="utf-8")
        allowed_res = mcp.handle(
            {"jsonrpc": "2.0", "id": 13, "method": "tools/call",
             "params": {"name": "project_attach", "arguments": {
                 "project_id": p["id"], "path": str(ok_file)}}},
            app, listing, False, False,
        )["result"]
        self.assertFalse(allowed_res["isError"])

    def test_serve_rejects_nan_with_parse_error(self):
        app = App(self.root / "ws")
        fake_stdin = type("Stdin", (), {"buffer": io.BytesIO(b'{"jsonrpc":"2.0","id":1,"method":"ping","bad":NaN}\n')})()
        out = io.StringIO()
        with patch.object(mcp.sys, "stdin", fake_stdin), redirect_stdout(out):
            mcp.serve(app)
        response = json.loads(out.getvalue().strip())
        self.assertEqual(response, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})


class PackageReleaseAuditTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_release_zip_uses_deflated_and_manifest_permissions(self):
        repo = self.root / "release"
        (repo / "src" / "paper2project").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "NOTICE.md").write_text("public notice\n", encoding="utf-8")
        (repo / "src" / "paper2project" / "__init__.py").write_text('__version__ = "1.0.2"\n', encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "NOTICE.md", "src/paper2project/__init__.py"], check=True)

        out = io.StringIO()
        with patch.object(package_release, "ROOT", repo), \
             patch.object(package_release, "FILES", ["NOTICE.md"]), \
             patch.object(package_release, "TREES", ["src/paper2project"]), \
             redirect_stdout(out):
            package_release.main()

        summary = json.loads(out.getvalue())
        zip_path = Path(summary["path"])
        self.assertTrue(zip_path.is_file())
        prefix = f"paper2project-{package_release.__version__}/"
        with zipfile.ZipFile(zip_path) as archive:
            infos = archive.infolist()
            self.assertTrue(infos)
            for info in infos:
                self.assertEqual(info.compress_type, zipfile.ZIP_DEFLATED, info.filename)
            manifest_info = archive.getinfo(prefix + "MANIFEST.json")
            self.assertEqual(manifest_info.external_attr, 0o644 << 16)


if __name__ == "__main__":
    unittest.main()
