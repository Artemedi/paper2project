"""Copy project memories and research into immutable, line-addressable snapshots."""
from __future__ import annotations

from html.parser import HTMLParser
import http.client
import hashlib
from pathlib import Path
import ipaddress
import json
import socket
import time
import urllib.parse
import urllib.request

from .store import digest, identifier, now, regular_file, slug
from .pdf import extract as extract_pdf

MAX_FILE = 32 * 1024 * 1024
MAX_MEMORY = 16 * 1024 * 1024
MAX_DOCUMENTS = 200
MAX_DISCOVERY = 20000


def plain_path(value):
    path = Path(value).absolute()
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        # /home -> /var/home is normal on Bazzite; resolve trusted parent aliases,
        # but reject symlinks inside an imported tree separately below.
        if path.is_symlink():
            raise ValueError("Input symlink is not allowed")
    return path.resolve()


def _files(root):
    if root.is_file():
        return [root]
    found = []
    for count, path in enumerate(root.rglob("*"), 1):
        if count > MAX_DISCOVERY:
            raise ValueError("Input tree exceeds discovery limit")
        relative = path.relative_to(root)
        if len(relative.parts) > 32:
            raise ValueError("Input tree exceeds depth limit")
        if any(part.startswith(".") for part in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError("Symlink in input tree: " + str(relative))
        if path.is_file() and path.suffix.lower() in {".md", ".markdown", ".txt"}:
            found.append(path)
            if len(found) > MAX_DOCUMENTS:
                raise ValueError("Input needs 1..200 text documents")
    return sorted(found)


def document_payloads(root, max_bytes):
    paths = _files(root)
    if not paths or len(paths) > MAX_DOCUMENTS:
        raise ValueError("Input needs 1..200 text documents")
    total = 0
    for path in paths:
        with regular_file(path) as stream:
            payload = stream.read(max_bytes - total + 1)
        total += len(payload)
        if total > max_bytes:
            raise ValueError("Input document set exceeds byte limit")
        text = payload.decode("utf-8-sig")
        if "\0" in text:
            raise ValueError("Binary document is not supported")
        name = path.name if root.is_file() else path.relative_to(root).as_posix()
        yield name, payload, text


def documents(store, root, *, prefix, max_bytes=MAX_MEMORY):
    return [{"id": prefix + ":" + name, "path": name, "sha256": store.put(payload),
             "text": store.put(text.encode()), "lines": len(text.splitlines()), "kind": "text"}
            for name, payload, text in document_payloads(root, max_bytes)]


def validate_handoff_size(sources):
    if sum(plain_path(path).stat().st_size for path in sources) > MAX_FILE:
        raise ValueError("Qualified source bundle exceeds 32 MiB")


def validate_handoff_inputs(package, sources, inventory):
    validate_handoff_size(sources)
    package_root = plain_path(package)
    validate_package_inventory(package_root, inventory)
    if "references/paper.md" not in inventory:
        raise ValueError("Qualified package needs references/paper.md for research citations")
    for _ in document_payloads(package_root, MAX_FILE):
        pass


def import_projects(store, source, layout="auto", name=None, project_id=None):
    root = plain_path(source)
    if not root.exists():
        raise ValueError("Project memory path does not exist")
    if layout not in {"auto", "single", "catalog"}:
        raise ValueError("layout must be auto, single or catalog")
    single = root.is_file() or layout == "single" or (layout == "auto" and name is not None)
    if project_id and not single:
        raise ValueError("project_id requires single-project import")
    paths = [root] if single else sorted(p for p in root.iterdir() if not p.name.startswith(".") and (p.is_dir() or p.suffix.lower() in {".md", ".markdown", ".txt"}))
    if not paths or len(paths) > 100:
        raise ValueError("Catalogue needs 1..100 project memories")
    prepared = []
    seen = set()
    for path in paths:
        if path.is_symlink():
            raise ValueError("Project memory symlink is not allowed")
        title = name or (path.stem if path.is_file() else path.name)
        pid = identifier(project_id) if project_id else slug(title)
        if pid in seen:
            raise ValueError("Duplicate project identifiers; rename catalogue entries")
        seen.add(pid)
        docs = documents(store,path,prefix="project:"+pid)
        snap = {"kind":"project", "id":pid,"name":title,"documents":docs}
        old = next((p for p in store.list("project") if p["id"] == pid),None)
        if old and not project_id and old["name"] != title:
            raise ValueError(f"Project id {pid!r} already belongs to {old['name']!r}; names that differ only in case or punctuation collide, pass an explicit project id")
        if old and (assets := store.json(old["snapshot"]).get("assets")):
            snap["assets"] = assets
        revision = store.put_json(snap)
        prepared.append({"id":pid,"name":title,"snapshot":revision,"updated_at":now(),
                         "revision":(old["revision"] + (old["snapshot"] != revision)) if old else 1,
                         "documents":len(docs)})
    for row in prepared:
        store.save("project",row["id"],row)
    return prepared


def check_address(address,hostname):
    """Refuse non-public destinations; applied to every address actually used for a connection."""
    ip = ipaddress.ip_address(address.split("%")[0])
    fake_ip = ip in ipaddress.ip_network("198.18.0.0/15") and hostname in {
        "arxiv.org", "www.nature.com", "idp.nature.com", "static-content.springer-cdn.com",
        "raw.githubusercontent.com", "github.com"}
    embedded = None
    if ip.version == 6:
        embedded = ip.ipv4_mapped or (ipaddress.IPv4Address(ip.packed[-4:]) if any(
            ip in ipaddress.ip_network(net) for net in ("64:ff9b::/96", "::/96", "::ffff:0:0:0/96")) else None)
    if ip.version == 6 and ip in ipaddress.ip_network("fec0::/10"):
        raise ValueError("Private research network destination is not allowed")
    if (embedded is not None and not embedded.is_global) or (not ip.is_global and not fake_ip):
        raise ValueError("Private research network destination is not allowed")


def public_url(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None,443):
        raise ValueError("Research acquisition requires public HTTPS without credentials")
    for item in socket.getaddrinfo(parsed.hostname,443,type=socket.SOCK_STREAM):
        check_address(item[4][0],parsed.hostname)
    return parsed


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Resolve once, validate every address, then connect to a validated address: no second lookup can rebind it."""
    def connect(self):
        infos = socket.getaddrinfo(self.host,self.port or 443,type=socket.SOCK_STREAM)
        for info in infos:
            check_address(info[4][0],self.host)
        last = None
        for info in infos:  # every address was validated above; try them in resolver order like a normal client
            try:
                sock = socket.create_connection((info[4][0],self.port or 443),self.timeout,self.source_address)
                break
            except OSError as exc:
                last = exc
        else:
            raise last or OSError("no address to connect to")
        self.sock = self._context.wrap_socket(sock,server_hostname=self.host)


class PinnedHTTPS(urllib.request.HTTPSHandler):
    def https_open(self,req):
        return self.do_open(PinnedHTTPSConnection,req,context=self._context)


class Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        public_url(newurl)
        return super().redirect_request(req,fp,code,msg,headers,newurl)


def read_http_limited(response, max_bytes, deadline):
    content = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Research download exceeded total time limit")
        sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
        if sock is not None:
            sock.settimeout(min(30, remaining))
        block = (response.read1 if hasattr(response, "read1") else response.read)(
            min(65536, max_bytes + 1 - len(content)))
        if not block:
            return bytes(content)
        content.extend(block)
        if len(content) > max_bytes:
            raise ValueError("Research download exceeds32MiB")


class TextHTML(HTMLParser):
    def __init__(self):
        super().__init__(); self.skip=0; self.parts=[]
    def handle_starttag(self,tag,attrs):
        if tag in {"script","style"}: self.skip+=1
        if tag in {"p","div","br","h1","h2","h3","h4","h5","h6","li",
                   "tr","td","th","pre","blockquote","section"}: self.parts.append("\n")
    def handle_endtag(self,tag):
        if tag in {"script","style"}: self.skip=max(0,self.skip-1)
    def handle_data(self,data):
        if not self.skip: self.parts.append(data)


def import_research(store,source,title=None,research_id=None,origin=None):
    rid = identifier(research_id) if research_id else slug(title or Path(urllib.parse.urlsplit(str(source)).path).stem or "research")
    location = str(source)
    if location.startswith(("https://","http://")):
        deadline = time.monotonic() + 90
        public_url(location)
        request=urllib.request.Request(location,headers={"User-Agent":"Paper2Project/0.1"})
        with urllib.request.build_opener(urllib.request.ProxyHandler({}),Redirect(),PinnedHTTPS()).open(request,timeout=30) as response:
            public_url(response.url)
            raw=read_http_limited(response, MAX_FILE, deadline)
            content_type=response.headers.get_content_type()
            charset=response.headers.get_content_charset() or "utf-8"
        if len(raw)>MAX_FILE: raise ValueError("Research download exceeds32MiB")
        url_parts=urllib.parse.urlsplit(location)
        requested_pdf=(url_parts.path.lower().endswith(".pdf")
                       or (url_parts.hostname=="arxiv.org" and url_parts.path.startswith("/pdf/"))
                       or content_type=="application/pdf")
        if requested_pdf and not raw.startswith(b"%PDF-"):
            raise ValueError("PDF source returned non-PDF content; possible access or CAPTCHA page")
        filename=Path(url_parts.path).name or "research"
    else:
        path=plain_path(location)
        if path.is_dir():
            docs=documents(store,path,prefix="research:"+rid,max_bytes=MAX_FILE)
            return _research_record(store,rid,title or path.name,docs,origin or "local-reviewed-package",{"coverage":"IMPORTED_PACKAGE_REVIEW_NOT_ATTESTED"},bool(research_id))
        with regular_file(path) as stream:
            raw = stream.read(MAX_FILE + 1)
        if len(raw) > MAX_FILE: raise ValueError("Research file missing or exceeds32MiB")
        filename=path.name;content_type="";charset="utf-8"
    original=store.put(raw)
    limits=[]
    if raw.startswith(b"%PDF-"):
        text=extract_pdf(raw)
        coverage="TEXT_EXTRACTED_NOT_VISUALLY_REVIEWED"
        limits=["Formatting, figures, equations, tables and separate supplements require source review."]
    elif "html" in content_type or raw.lstrip().lower().startswith((b"<!doctype html",b"<html")):
        try:
            html=raw.decode(charset, errors="replace")
        except LookupError:
            html=raw.decode("utf-8", errors="replace")
        parser=TextHTML();parser.feed(html);text="".join(parser.parts)
        coverage="HTML_TEXT_ONLY";limits=["HTML text extraction may omit figures, tables and supplementary material."]
    else:
        text=raw.decode("utf-8-sig");coverage="PROVIDED_TEXT_COMPLETENESS_UNATTESTED"
    if len(text.encode())>MAX_FILE or "\0" in text: raise ValueError("Invalid or oversized research text")
    doc={"id":"research:"+rid+":"+filename,"path":filename,"sha256":original,"text":store.put(text.encode()),"lines":len(text.splitlines()),"kind":"research"}
    return _research_record(store,rid,title or filename,[doc],origin or (location if location.startswith("https://") else "local-file"),{"coverage":coverage,"limitations":limits},bool(research_id))



def validate_package_inventory(package_root, inventory):
    present = set()
    for count, path in enumerate(package_root.rglob("*"), 1):
        if count > MAX_DISCOVERY:
            raise ValueError("Qualified package exceeds inventory limit")
        if path.is_symlink():
            raise ValueError("Qualified package contains a symlink")
        if path.is_file():
            present.add(path.relative_to(package_root).as_posix())
        elif not path.is_dir():
            raise ValueError("Qualified package contains a special file")
    if present != set(inventory):
        raise ValueError("Qualified package inventory does not match its files")
    for name, expected in inventory.items():
        if not isinstance(name, str):
            raise ValueError("Qualified package contains an unsafe path")
        relative = Path(name)
        if (relative.is_absolute() or not relative.parts
                or any(part in {"", ".", ".."} for part in relative.parts)):
            raise ValueError("Qualified package contains an unsafe path")
        hasher = hashlib.sha256()
        with regular_file(package_root / relative) as stream:
            for block in iter(lambda: stream.read(1 << 20), b""):
                hasher.update(block)
        if hasher.hexdigest() != expected:
            raise ValueError("Qualified package file changed after verification: " + name)

def import_reviewed_package(store, package, research_id, title, origin, qualification, inventory, sources):
    """Accept artifacts from the local Paper2Agent verifier, checking their pinned bytes."""
    try:
        captured, records = qualification["captured"], qualification["records"]
        report = json.loads(captured["verification.json"])
        review = json.loads(captured["independent-review.json"])
        manifest = json.loads(captured["source-manifest"])
        build = json.loads(captured["build.json"])
        fingerprint = digest(json.dumps(records, sort_keys=True).encode())
        valid = (
            all(digest(captured[name]) == records[name] for name in ("verification.json", "source-manifest", "build.json"))
            and review["decision"] == "PASS" and isinstance(review["reviewer"], str) and review["reviewer"].strip()
            and qualification["fingerprint"] == review["fingerprint"] == fingerprint
            and report["status"] == qualification["verification_status"]
            and report["status"] in {"reviewed", "reviewed_with_limitations"}
            and report["mechanical_ok"] is True and report["all_sources_agent_reviewed"] is True
            and manifest["full_text_status"] == "COMPLETE"
            and build["files"] == inventory
            and [(role, key) for _, role, key in sources] == [(e["role"], e["sha256"]) for e in manifest["sources"]]
            and sum(role == "main" for _, role, _ in sources) == 1
            and all(role in {"main", "supplement"} for _, role, _ in sources)
            and isinstance(qualification["limitations"], list)
            and all(isinstance(limit, str) for limit in qualification["limitations"])
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("Invalid qualified Paper2Agent receipt or source inventory")
    limitations = report.get("limitations") or (["Paper2Agent review recorded limitations; consult the source review."]
                                                 if report["status"] == "reviewed_with_limitations" else [])
    if limitations != qualification["limitations"]:
        raise ValueError("Qualified Paper2Agent limitations disagree with the verification record")
    primary = next(e for e in manifest["sources"] if e["role"] == "main")
    origin = primary.get("resolved_url") or primary.get("url") or origin or "local-reviewed-package"
    rid = identifier(research_id)
    package_root = plain_path(package)
    validate_handoff_inputs(package_root, [path for path, _, _ in sources], inventory)
    docs = documents(store, package_root, prefix="research:" + rid, max_bytes=MAX_FILE)
    for doc in docs:
        doc["evidence_role"] = ("source_reference" if doc["path"] in
                                {"references/paper.md", "references/supplement.md", "references/index.md"}
                                else "package_scaffolding")
    expected_docs = {name: key for name, key in inventory.items()
                     if not any(part.startswith(".") for part in Path(name).parts)
                     and Path(name).suffix.lower() in {".md", ".markdown", ".txt"}}
    if expected_docs != {doc["path"]: doc["sha256"] for doc in docs}:
        raise ValueError("Paper2Agent package text changed after qualification")
    validate_handoff_size(path for path, _, _ in sources)
    raw_sources = []
    total = 0
    for path, role, expected in sources:
        source = plain_path(path)
        with regular_file(source) as stream:
            payload = stream.read(MAX_FILE - total + 1)
        total += len(payload)
        if total > MAX_FILE:
            raise ValueError("Qualified source bundle exceeds 32 MiB")
        actual = store.put(payload)
        if actual != expected:
            raise ValueError("Paper2Agent source changed after qualification")
        raw_sources.append({"role": role, "sha256": actual})
    metadata = {"coverage": "PAPER2AGENT_REVIEW_RECORDED", "limitations": qualification["limitations"],
                "qualification": {"fingerprint": qualification["fingerprint"],
                                  "verification_status": qualification["verification_status"],
                                  "review_digest": store.put(captured["independent-review.json"]),
                                  "manifest_digest": store.put(captured["source-manifest"]),
                                  "source_artifacts": raw_sources}}
    return _research_record(store, rid, title, docs, origin, metadata, explicit=True)


def research_metadata(store, research):
    snapshot = store.json(research["snapshot"])
    origin = snapshot.get("origin")
    if isinstance(origin, str):
        try:
            parsed = urllib.parse.urlsplit(origin)
        except ValueError:
            parsed = urllib.parse.urlsplit("")
        if parsed.scheme in {"http", "https"} and parsed.hostname and not (parsed.username or parsed.password):
            origin = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        elif origin not in {"local-file", "local-reviewed-package"}:
            origin = "local-file"
    coverage = snapshot.get("coverage", "UNATTESTED")
    qualification = snapshot.get("qualification")
    limits = snapshot.get("limitations", [])
    if coverage == "PAPER2AGENT_REVIEW_RECORDED" and not all(
            (qualification or {}).get(field) for field in ("review_digest", "manifest_digest")):
        coverage = "PAPER2AGENT_LEGACY_REVIEW_UNVERIFIED"
        limits = [*limits, "This handoff predates verified receipt binding; import it again to establish current coverage."]
    evidence = None
    if qualification:
        objects = [(source["role"], source["sha256"]) for source in qualification.get("source_artifacts", [])]
        objects.extend((role, qualification.get(field)) for role, field in
                       (("review", "review_digest"), ("manifest", "manifest_digest")))
        unavailable = []
        for role, key in objects:
            try:
                store.blob(key)
            except (ValueError, OSError):
                unavailable.append({"role": role, "sha256": key})
        evidence = {"status": "UNAVAILABLE" if unavailable else "AVAILABLE", "unavailable": unavailable}
    return {**research, "origin": origin, "coverage": coverage,
            "limitations": limits, "qualification": qualification, "evidence": evidence}


def _research_record(store,rid,title,docs,origin,metadata,explicit=False):
    old=next((r for r in store.list("research") if r["id"]==rid),None)
    if old and old["name"]!=title and not explicit:
        raise ValueError(f"Research id {rid!r} already belongs to {old['name']!r}; names that differ only in case or punctuation collide, pass an explicit research id")
    snap={"kind":"research","id":rid,"name":title,"documents":docs,"origin":origin,**metadata}
    revision=store.put_json(snap)
    row={"id":rid,"name":title,"snapshot":revision,"updated_at":now(),"documents":len(docs)}
    return store.save("research",rid,row)
