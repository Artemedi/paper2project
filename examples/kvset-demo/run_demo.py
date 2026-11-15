#!/usr/bin/env python3
"""Synthetic end-to-end demo: README of the KVSET author repository vs a toy project.

Usage: run_demo.py --source CHECKOUT_AT_PINNED_REVISION --image IMAGE [--workspace DIR]
The 'host agent' step (analysis) is written here by hand; it is grounded but NOT independently
reviewed, so the report says BENEFIT_PENDING_REVIEW. The traces are synthetic, so a benefit means: on THESE declared inputs only.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))
from paper2project.core import App  # noqa: E402

REVISION = "fab8ac99017e4ca6f22c811ee874b72a2e1a7296"


def locate(app, job, document_id, quote):
    for number, line in enumerate(app.store.blob(app.docs(job)[document_id]["text"]).decode().splitlines(), 1):
        if quote in line:
            return {"document_id": document_id, "start": number, "end": number, "quote": quote}
    raise SystemExit("quote not found: " + quote)


def command(trace):
    return ["sh", "-c", f"python3 src/token_ids_replay.py /input/{trace}.jsonl --kv-bytes-per-token 10 --page-size 1 "
                        "--capacities 60B --warm-up-percent 0 --out-format json > /output/result.json"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--image", required=True)
    ap.add_argument("--workspace")
    args = ap.parse_args()
    head = subprocess.run(["git", "-C", args.source, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    if head != REVISION:
        raise SystemExit(f"source is at {head}, expected {REVISION}")
    app = App(args.workspace or tempfile.mkdtemp(prefix="p2p-demo-"))
    project = app.import_projects(HERE / "cache-service.md", name="Cache service")[0]
    research = app.import_research(Path(args.source) / "README.md", title="KV cache capacity simulator README",
                                   research_id="kvset-readme", origin="https://github.com/llc-kc/kv_cache_capacity_estimator@" + REVISION)
    job = app.create_job(research["id"], [project["id"]])["id"]
    pdoc, rdoc = f"project:{project['id']}:cache-service.md", "research:kvset-readme:README.md"
    app.submit_analysis(job, {"summary": "The simulator can test a prefix-ordering change on a token-ID trace.", "assessments": [{
        "project_id": project["id"], "relevance": "candidate",
        "rationale": "The project asks whether prefix ordering changes reuse; the tool replays token-ID traces through a page-granular LRU cache.",
        "citations": [locate(app, job, pdoc, "page-granular LRU prefix cache"), locate(app, job, rdoc, "Offline Token ID Replay")],
        "missing_evidence": ["Real de-identified token traces and the real KV bytes per token are needed before any production claim."]}]})
    plan = {"kind": "project_benefit", "project_id": project["id"], "image": args.image,
            "source": {"path": args.source, "revision": REVISION, "license": "MIT root LICENSE; Apache-2.0 source notices"},
            "input": {"path": str(HERE / "traces")},
            "arms": {"baseline": command("baseline"), "candidate": command("candidate")},
            "metrics": [{"name": "prefix_page_hits", "path": "results.0.page_hits", "expected": {"baseline": 0, "candidate": 2}},
                        {"name": "page_misses", "path": "results.0.page_misses", "expected": {"baseline": 6, "candidate": 4}}],
            "benefit": {"metric": "prefix_page_hits", "direction": "increase", "min_delta": 2,
                        "guards": [{"metric": "page_misses", "op": "le", "value": 4}]}}
    experiment = app.add_experiment(job, plan)
    run = app.run_experiment(job, experiment["id"])
    print(json.dumps({"job": job, "run": run["id"], "status": run["status"], "benefit": run["benefit"]}, indent=2))
    print(app.report(job)["markdown"])
    return int(run["status"] != "PASS")


if __name__ == "__main__":
    raise SystemExit(main())
