"""Command line interface; the same App backs the MCP server."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from . import __version__, execution, model
from .core import App


def default_workspace():
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return os.environ.get("PAPER2PROJECT_WORKSPACE") or str(Path(base) / "paper2project")


def parser():
    p = argparse.ArgumentParser(prog="paper2project", description=__doc__)
    p.add_argument("--workspace", default=None, help="state directory (default: $PAPER2PROJECT_WORKSPACE or XDG data dir)")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)

    pr = sub.add_parser("project").add_subparsers(dest="action", required=True)
    s = pr.add_parser("import")
    s.add_argument("path")
    s.add_argument("--layout", choices=["auto", "single", "catalog"], default="auto")
    s.add_argument("--name")
    s.add_argument("--id")
    pr.add_parser("list")
    s = pr.add_parser("attach")
    s.add_argument("project_id")
    s.add_argument("path")
    s.add_argument("--name")

    rs = sub.add_parser("research").add_subparsers(dest="action", required=True)
    s = rs.add_parser("import")
    s.add_argument("source", help="Markdown/text/PDF file, reviewed package directory, or https URL")
    s.add_argument("--title")
    s.add_argument("--id")
    s.add_argument("--origin")
    rs.add_parser("list")

    jb = sub.add_parser("job").add_subparsers(dest="action", required=True)
    s = jb.add_parser("create")
    s.add_argument("--research", required=True)
    s.add_argument("--project", action="append", required=True)
    for name in ("status", "context"):
        s = jb.add_parser(name)
        s.add_argument("job_id")
        if name == "context":
            s.add_argument("--query", default="")
    jb.add_parser("list")
    s = jb.add_parser("read")
    s.add_argument("job_id")
    s.add_argument("document_id")
    s.add_argument("--start", type=int, default=1)
    s.add_argument("--count", type=int, default=80)

    s = sub.add_parser("analyze", help="standalone: ask an OpenAI-compatible endpoint for a grounded analysis")
    s.add_argument("job_id")
    s.add_argument("--base-url", default=os.environ.get("PAPER2PROJECT_API_BASE"))
    s.add_argument("--model", default=os.environ.get("PAPER2PROJECT_MODEL"))
    s.add_argument("--key-env", default="PAPER2PROJECT_API_KEY", help="NAME of the environment variable holding the API key")
    s.add_argument("--query", default="")
    s.add_argument("--yes-send", action="store_true", help="confirm that project and research text is sent to the endpoint")
    s = sub.add_parser("submit-analysis", help="submit an analysis JSON file written by a human or agent")
    s.add_argument("job_id")
    s.add_argument("file")
    s = sub.add_parser("review")
    s.add_argument("job_id")
    s.add_argument("--decision", choices=["pass", "revise"], required=True)
    s.add_argument("--notes", required=True)
    s.add_argument("--reviewer", required=True)
    s.add_argument("--analysis", required=True, help="analysis hash shown by `report`/job status; the review covers that analysis and the experiments registered so far")

    ex = sub.add_parser("experiment").add_subparsers(dest="action", required=True)
    s = ex.add_parser("add")
    s.add_argument("job_id")
    s.add_argument("plan")
    s = ex.add_parser("run")
    s.add_argument("job_id")
    s.add_argument("experiment_id")
    s = ex.add_parser("list")
    s.add_argument("job_id")

    s = sub.add_parser("report")
    s.add_argument("job_id")
    s.add_argument("--format", choices=["markdown", "json"], default="markdown")
    s = sub.add_parser("decisions", help="record the current decisions and print the stored history of a job")
    s.add_argument("job_id")
    s.add_argument("--no-record", action="store_true", help="only print the history, do not record the current state")
    s = sub.add_parser("export")
    s.add_argument("job_id")
    s.add_argument("destination")
    s.add_argument("--include-memory", action="store_true", help="include full private project and research text")

    s = sub.add_parser("serve", help="stdio MCP server for a host agent")
    s.add_argument("--allow-root", action="append", default=[], help="directory MCP clients may import from (repeatable)")
    s.add_argument("--allow-execution", action="store_true", help="let clients run container experiments")
    s.add_argument("--allow-review", action="store_true", help="let clients record the independent analysis review (off: the analysing agent cannot approve itself)")
    s.add_argument("--allow-network", action="store_true", help="let clients import research from https URLs")
    sub.add_parser("selftest", help="offline end-to-end check of this installation")
    sub.add_parser("doctor", help="report optional runtime requirements")
    return p


def doctor():
    try:
        runtime = " ".join(execution.podman_command())
    except ValueError as exc:
        runtime = "UNAVAILABLE: " + str(exc)
    try:
        import pypdf  # noqa: F401
        pdf = True
    except ImportError:
        pdf = False
    return {"version": __version__, "python": sys.version.split()[0], "container_runtime": runtime, "pdf_import": pdf,
            "note": "A container runtime is needed only to run experiments; pypdf only to import PDFs."}


def run(args):
    if args.command == "doctor":
        return doctor()
    if args.command == "selftest":
        from .selftest import selftest
        return selftest()
    app = App(args.workspace or default_workspace())
    c, a = args.command, getattr(args, "action", None)
    if c == "project":
        if a == "import":
            return app.import_projects(args.path, args.layout, args.name, args.id)
        if a == "attach":
            return app.attach_asset(args.project_id, args.path, args.name)
        return app.store.list("project")
    if c == "research":
        return app.import_research(args.source, args.title, args.id, args.origin) if a == "import" else app.store.list("research")
    if c == "job":
        if a == "create":
            return app.create_job(args.research, args.project)
        if a == "status":
            return app.status(args.job_id)
        if a == "context":
            return app.context(args.job_id, args.query)
        if a == "read":
            return app.read(args.job_id, args.document_id, args.start, args.count)
        return [{k: j[k] for k in ("id", "created_at")} for j in app.store.list("job")]
    if c == "analyze":
        if not args.yes_send:
            raise ValueError("Analysis sends project and research text to the endpoint; repeat with --yes-send to confirm")
        endpoint = model.Endpoint(args.base_url, args.model, args.key_env)
        print(f"Sending snapshot text to {endpoint.host} (model {endpoint.model})", file=sys.stderr)
        return model.analyze(app, args.job_id, endpoint, args.query)
    if c == "submit-analysis":
        return app.submit_analysis(args.job_id, json.loads(Path(args.file).read_text()))
    if c == "review":
        return app.review_analysis(args.job_id, args.decision, args.notes, args.reviewer, args.analysis)
    if c == "experiment":
        if a == "add":
            return app.add_experiment(args.job_id, json.loads(Path(args.plan).read_text()))
        if a == "run":
            return app.run_experiment(args.job_id, args.experiment_id)
        return app.experiments(args.job_id)
    if c == "report":
        report = app.report(args.job_id)
        return report["json"] if args.format == "json" else report["markdown"]
    if c == "decisions":
        if not args.no_record:
            app.record_decisions(args.job_id)
        return app.decisions(args.job_id)
    if c == "export":
        return app.export(args.job_id, args.destination, args.include_memory)
    if c == "serve":
        from .mcp import serve
        return serve(app, args.allow_root, args.allow_execution, args.allow_network, args.allow_review)
    raise ValueError("Unknown command")


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = run(args)
    except Exception as exc:
        print("Error: " + (str(exc) if isinstance(exc, (OSError, ValueError, KeyError, TypeError)) else f"{type(exc).__name__}: {exc}"), file=sys.stderr)
        return 1
    if result is not None:
        print(result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
