"""Offline installation check: no network, no container runtime, no model, no real memory."""
from __future__ import annotations

from pathlib import Path
import tempfile
import zipfile

from .core import App

PROJECT = "# Cache service\n\nWe serve 40 req/s with an LRU prefix cache.\nCache misses dominate cost.\n"
RESEARCH = "# Capacity paper\n\nAn LRU simulator predicts cache hit rate from token traces.\n"


def selftest():
    steps = []

    def step(name, ok, detail=""):
        steps.append({"step": name, "passed": bool(ok), "detail": detail})
        if not ok:
            raise AssertionError(f"selftest step failed: {name} {detail}")

    with tempfile.TemporaryDirectory(prefix="paper2project-selftest-") as tmp:
        tmp = Path(tmp)
        (tmp / "project.md").write_text(PROJECT)
        (tmp / "paper.md").write_text(RESEARCH)
        app = App(tmp / "workspace")
        project = app.import_projects(tmp / "project.md", name="Cache service")[0]
        research = app.import_research(tmp / "paper.md", title="Capacity paper")
        step("import", project["revision"] == 1 and research["documents"] == 1)
        job = app.create_job(research["id"], [project["id"]])
        step("job", job["state"] == "CREATED" and not job["stale"])
        pdoc, rdoc = f"project:{project['id']}:project.md", "research:capacity-paper:paper.md"
        analysis = {"summary": "Candidate for a trace study.", "assessments": [{
            "project_id": project["id"], "relevance": "candidate", "rationale": "Both concern LRU prefix caches.",
            "citations": [{"document_id": pdoc, "start": 3, "end": 3, "quote": "LRU prefix cache"},
                          {"document_id": rdoc, "start": 3, "end": 3, "quote": "LRU simulator"}], "missing_evidence": ["a real token trace"]}]}
        step("grounded analysis", app.submit_analysis(job["id"], analysis)["state"] == "ANALYZED")
        bad = {**analysis, "assessments": [{**analysis["assessments"][0], "citations": [{**analysis["assessments"][0]["citations"][0], "quote": "invented"}, analysis["assessments"][0]["citations"][1]]}]}
        try:
            app.submit_analysis(job["id"], bad)
            step("ungrounded quote rejected", False)
        except ValueError:
            step("ungrounded quote rejected", True)
        report = app.report(job["id"])
        decision = report["json"]["projects"][0]["decision"]
        step("text-only analysis is not upgraded", decision == "ANALYSIS_ONLY", decision)
        (tmp / "project.md").write_text(PROJECT + "Changed.\n")
        app.import_projects(tmp / "project.md", name="Cache service")
        step("stale evidence detected", app.status(job["id"])["state"] == "STALE")
        export = app.export(job["id"], tmp / "report.zip")
        with zipfile.ZipFile(export["path"]) as archive:
            step("export omits private memory", not any(n.startswith("memory/") for n in archive.namelist()))
    return {"status": "PASS", "steps": steps}
