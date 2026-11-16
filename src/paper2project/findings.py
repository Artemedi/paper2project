"""Optional, scoped hypotheses for analyses with several distinct application targets."""
from .store import digest, dumps, identifier

PROPOSALS = ("candidate", "not_applicable", "already_implemented", "needs_evidence")


def test_binding(experiment):
    return {"experiment_id": experiment["id"], "plan_hash": experiment["plan"],
            "source_tree": experiment["source_tree"], "input_tree": experiment["input_tree"]}


def evidence_key(job_id, project, analysis, finding_id, bindings, author):
    tests = sorted(bindings, key=lambda binding: binding["experiment_id"])
    return digest(dumps([job_id, project["id"], project["snapshot"], analysis, finding_id, tests, author]).encode())


def quote_matches(lines, start, end, quote):
    return (type(start) is int and type(end) is int and 1 <= start <= end <= len(lines)
            and isinstance(quote, str) and 8 <= len(quote.strip()) <= 2000 and end - start < 12
            and quote.count("\n") == end - start and quote in "\n".join(lines[start - 1:end]))


def artifact_anchor(ref, assets, blob):
    if not isinstance(ref, dict) or not isinstance(ref.get("name"), str):
        raise ValueError("An artifact reference needs a name and SHA-256")
    asset = assets.get(ref["name"])
    if asset is None or ref.get("sha256") != asset["sha256"]:
        raise ValueError("Finding artifact must match an attachment in this project's frozen snapshot")
    try:
        lines = blob(asset["sha256"]).decode("utf-8-sig").splitlines()
    except UnicodeDecodeError as exc:
        raise ValueError("Finding artifact must be UTF-8 text") from exc
    if not quote_matches(lines, ref.get("start"), ref.get("end"), ref.get("quote")):
        raise ValueError("Finding artifact needs an exact quote and its precise line range")
    return {k: ref[k] for k in ("name", "sha256", "start", "end", "quote")}


def normalize_findings(rows, assets, blob, research_citation):
    """Validate source and project anchors. All findings still await semantic judgment."""
    if not isinstance(rows, list) or not 1 <= len(rows) <= 25:
        raise ValueError("findings must contain 1..25 scoped hypotheses")
    seen, normalized = set(), []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("A finding must be an object")
        fid = identifier(row.get("id"))
        if fid in seen:
            raise ValueError("Finding ids must be unique within a project")
        seen.add(fid)
        body = {}
        for field in ("target", "claim", "premise", "falsifier"):
            value = row.get(field)
            if not isinstance(value, str) or not 1 <= len(value.strip()) <= 2000:
                raise ValueError(f"Finding {fid} needs non-empty {field}")
            body[field] = value.strip()
        proposed = row.get("proposed")
        if proposed not in PROPOSALS:
            raise ValueError("Finding proposed must be candidate, not_applicable, already_implemented or needs_evidence")
        citations = row.get("research_citations")
        if not isinstance(citations, list) or not 1 <= len(citations) <= 5:
            raise ValueError("A finding needs 1..5 research citations")
        for citation in citations:
            if research_citation(citation)["owner_kind"] != "research":
                raise ValueError("Finding research_citations must cite the research snapshot")
        citations = [{k: c[k] for k in ("document_id", "start", "end", "quote")} for c in citations]
        refs = row.get("project_artifacts", [])
        if not isinstance(refs, list) or len(refs) > 5:
            raise ValueError("Finding project_artifacts must contain at most five references")
        bound = [artifact_anchor(ref, assets, blob) for ref in refs]
        missing = row.get("missing_evidence", [])
        if not isinstance(missing, list) or len(missing) > 10 or not all(
                isinstance(x, str) and 1 <= len(x.strip()) <= 2000 for x in missing):
            raise ValueError("Finding missing_evidence must contain up to ten non-empty strings")
        if (not bound or proposed == "needs_evidence") and not missing:
            raise ValueError("Describe the missing evidence for a deferred or description-only finding")
        normalized.append({"id": fid, **body, "proposed": proposed, "research_citations": citations,
                           "project_artifacts": bound, "missing_evidence": missing})
    return normalized


def supported_tests(row, review):
    if review and review.get("second_observation") and row["project_artifacts"] and review["premise"] == "supported":
        return {eid for eid, item in review.get("test_reviews", {}).items()
                if item["test_link"] == "relevant" and item["second_observation"]}
    return set()


def finding_status(row, review=None):
    if review and review.get("second_observation") and row["project_artifacts"]:
        if review["premise"] == "contradicted":
            return "REVIEWED_PREMISE_CONTRADICTED"
        links = {item["test_link"] for item in review.get("test_reviews", {}).values() if item["second_observation"]}
        if supported_tests(row, review):
            return "REVIEWED_APPLICABILITY_SUPPORTED"
        if links == {"irrelevant"}:
            return "REVIEWED_TEST_LINK_REJECTED"
        if review["premise"] == "supported":
            return "REVIEWED_PREMISE_SUPPORTED"
    if row["proposed"] == "needs_evidence" or not row["project_artifacts"]:
        return "NEEDS_EVIDENCE"
    return "PENDING_SEMANTIC_REVIEW"
