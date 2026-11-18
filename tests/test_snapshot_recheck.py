"""Candidate admission checks real registered snapshots between mocked arms."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paper2project import execution
from paper2project.core import App


class SnapshotRecheck(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.app = App(self.root / "workspace")
        project = self.root / "project.md"
        paper = self.root / "paper.md"
        project.write_text("# Project\n\nA cache service.\n")
        paper.write_text("# Paper\n\nA cache method.\n")
        p, = self.app.import_projects(project)
        r = self.app.import_research(paper)
        self.job = self.app.create_job(r["id"], [p["id"]])["id"]
        self.app.submit_analysis(self.job, {"summary": "fixture", "assessments": [{
            "project_id": p["id"], "relevance": "candidate", "rationale": "fixture",
            "citations": [{"document_id": f"project:{p['id']}:project.md", "start": 3,
                           "end": 3, "quote": "A cache service."},
                          {"document_id": f"research:{r['id']}:paper.md", "start": 3,
                           "end": 3, "quote": "A cache method."}]}]})
        source, inputs = self.root / "source", self.root / "inputs"
        source.mkdir()
        inputs.mkdir()
        (source / "code.py").write_text("# fixture, never executed\n")
        (inputs / "data.txt").write_text("fixture input\n")
        self.experiment = self.app.add_experiment(self.job, {
            "kind": "reproduction", "project_id": p["id"], "image": "local/fixture",
            "source": {"path": str(source), "revision": "a" * 40, "license": "MIT"},
            "input": {"path": str(inputs)},
            "arms": {"baseline": ["fixture", "baseline"], "candidate": ["fixture", "candidate"]},
            "metrics": [{"name": "metric", "path": "metric", "expected": {"baseline": 1, "candidate": 2}}]})

    def run_fixture(self, mutation):
        launched = []

        def emit(app, plan, image, registered, directory, arm):
            launched.append(arm)
            result = {"metric": 1 if arm == "baseline" else 2}
            if arm == "baseline":
                mutation(registered)
            return {"result": result, "result_sha256": app.store.put_json(result),
                    "exit_code": 0, "error": None, "timed_out": False}

        with patch.object(execution, "image_id", return_value="fixture-image"), \
                patch.object(execution, "run_arm", side_effect=emit):
            run = self.app.run_experiment(self.job, self.experiment["id"])
        return run, launched

    def check_change(self, field, filename, reason, *, remove=False):
        def change(registered):
            path = Path(registered[field]) / filename
            if remove:
                path.unlink()
            else:
                path.write_text("changed after baseline")

        run, launched = self.run_fixture(change)
        self.assertEqual(launched, ["baseline"])
        self.assertEqual(run["status"], "BLOCKED")
        self.assertIn(reason, run["reason"])
        self.assertFalse(run["reproduced"])
        self.assertIsNone(run["benefit"])
        self.assertIn("finished_at", run)
        stored = self.app.store.get("run", run["id"])
        self.assertEqual(set(stored["arms"]), {"baseline"})
        self.assertEqual(self.app.store.json(stored["arms"]["baseline"]["result"]), {"metric": 1})

    def test_source_change_blocks_candidate(self):
        self.check_change("source_path", "code.py", "Source tree changed")

    def test_input_change_blocks_candidate(self):
        self.check_change("input_path", "data.txt", "Input tree changed")

    def test_removed_snapshot_file_blocks_candidate(self):
        self.check_change("source_path", "code.py", "Source tree changed", remove=True)

    def test_unchanged_snapshots_allow_both_arms(self):
        run, launched = self.run_fixture(lambda registered: None)
        self.assertEqual(launched, ["baseline", "candidate"])
        self.assertEqual(run["status"], "PASS")
