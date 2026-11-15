"""Copy project memories and research into immutable, line-addressable snapshots."""
from __future__ import annotations

from html.parser import HTMLParser
import http.client
import io
from pathlib import Path
import ipaddress
import socket
import urllib.parse
import urllib.request

from .store import identifier, now, slug

MAX_FILE = 32 * 1024 * 1024
MAX_MEMORY = 16 * 1024 * 1024
MAX_DOCUMENTS = 200


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
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part.startswith(".") for part in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError("Symlink in input tree: " + str(relative))
        if path.is_file() and path.suffix.lower() in {".md", ".markdown", ".txt"}:
            found.append(path)
    return found


def documents(store, root, *, prefix, max_bytes=MAX_MEMORY):
    paths = _files(root)
    if not paths or len(paths) > MAX_DOCUMENTS:
        raise ValueError("Input needs 1..200 text documents")
    total, docs = 0, []
    for path in paths:
        if path.stat().st_size > max_bytes:
            raise ValueError("Input document exceeds byte limit")
        payload = path.read_bytes()
        total += len(payload)
        if total > max_bytes:
            raise ValueError("Input document set exceeds byte limit")
        text = payload.decode("utf-8-sig")
        if "\0" in text:
            raise ValueError("Binary document is not supported")
        name = path.name if root.is_file() else path.relative_to(root).as_posix()
        docs.append({"id":prefix + ":" + name, "path":name, "sha256":store.put(payload),
                     "text":store.put(text.encode()), "lines":len(text.splitlines()), "kind":"text"})
    return docs


def import_projects(store, source, layout="auto", name=None, project_id=None):
    root = plain_path(source)
    if not root.exists():
        raise ValueError("Project memory path does not exist")
    if layout not in {"auto", "single", "catalog"}:
        raise ValueError("layout must be auto, single or catalog")
    single = root.is_file() or layout == "single" or (layout == "auto" and name is not None)
    if project_id and not single:
        raise ValueError("project_id requires single-project import")
    paths = [root] if single else sorted(p for p in root.iterdir() if not p.name.startswith(".") and (p.is_dir() or p.suffix.lower() in {".md", ".markdown"}))
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
        revision = store.put_json(snap)
        old = next((p for p in store.list("project") if p["id"] == pid),None)
        if old and not project_id and old["name"] != title:
            raise ValueError(f"Project id {pid!r} already belongs to {old['name']!r}; names that differ only in case or punctuation collide, pass an explicit project id")
        prepared.append({"id":pid,"name":title,"snapshot":revision,"updated_at":now(),
                         "revision":(old["revision"] + (old["snapshot"] != revision)) if old else 1,
                         "documents":len(docs)})
    for row in prepared:
        store.save("project",row["id"],row)
    return prepared


def check_address(address,hostname):
    """Refuse non-public destinations; applied to every address actually used for a connection."""
    ip = ipaddress.ip_address(address.split("%")[0])
    fake_ip = ip in ipaddress.ip_network("198.18.0.0/15") and hostname in {"arxiv.org", "www.nature.com", "raw.githubusercontent.com", "github.com"}
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


class TextHTML(HTMLParser):
    def __init__(self):
        super().__init__(); self.skip=0; self.parts=[]
    def handle_starttag(self,tag,attrs):
        if tag in {"script","style"}: self.skip+=1
        if tag in {"p","div","br","h1","h2","h3","li"}: self.parts.append("\n")
    def handle_endtag(self,tag):
        if tag in {"script","style"}: self.skip=max(0,self.skip-1)
    def handle_data(self,data):
        if not self.skip: self.parts.append(data)


def import_research(store,source,title=None,research_id=None,origin=None):
    rid = identifier(research_id) if research_id else slug(title or Path(urllib.parse.urlsplit(str(source)).path).stem or "research")
    location = str(source)
    if location.startswith(("https://","http://")):
        public_url(location)
        request=urllib.request.Request(location,headers={"User-Agent":"Paper2Project/0.1"})
        with urllib.request.build_opener(urllib.request.ProxyHandler({}),Redirect(),PinnedHTTPS()).open(request,timeout=30) as response:
            public_url(response.url)
            raw=response.read(MAX_FILE+1)
            content_type=response.headers.get_content_type()
        if len(raw)>MAX_FILE: raise ValueError("Research download exceeds32MiB")
        filename=Path(urllib.parse.urlsplit(location).path).name or "research"
    else:
        path=plain_path(location)
        if path.is_dir():
            docs=documents(store,path,prefix="research:"+rid,max_bytes=MAX_FILE)
            return _research_record(store,rid,title or path.name,docs,origin or "local-reviewed-package",{"coverage":"IMPORTED_PACKAGE_REVIEW_NOT_ATTESTED"},bool(research_id))
        if not path.is_file() or path.stat().st_size>MAX_FILE: raise ValueError("Research file missing or exceeds32MiB")
        raw=path.read_bytes();filename=path.name;content_type=""
    original=store.put(raw)
    limits=[]
    if raw.startswith(b"%PDF-"):
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise ValueError("Install the distribution's pypdf dependency to import PDFs") from exc
        reader=PdfReader(io.BytesIO(raw))
        if reader.is_encrypted: raise ValueError("Encrypted research PDF is not supported")
        if len(reader.pages)>300: raise ValueError("PDF exceeds300 pages")
        pages=[(page.extract_text() or "").replace("\0","\ufffd") for page in reader.pages]  # extractors can emit NUL
        if not any(p.strip() for p in pages): raise ValueError("PDF has no extractable text; supply reviewed Markdown/OCR")
        text="\n\n".join(f"# PDF page {i+1}\n{page}" for i,page in enumerate(pages))
        coverage="TEXT_EXTRACTED_NOT_VISUALLY_REVIEWED"
        limits=["Formatting, figures, equations, tables and separate supplements require source review."]
    elif "html" in content_type or raw.lstrip().lower().startswith((b"<!doctype html",b"<html")):
        parser=TextHTML();parser.feed(raw.decode("utf-8"));text="".join(parser.parts)
        coverage="HTML_TEXT_ONLY";limits=["HTML text extraction may omit figures, tables and supplementary material."]
    else:
        text=raw.decode("utf-8-sig");coverage="PROVIDED_TEXT_COMPLETENESS_UNATTESTED"
    if len(text.encode())>MAX_FILE or "\0" in text: raise ValueError("Invalid or oversized research text")
    doc={"id":"research:"+rid+":"+filename,"path":filename,"sha256":original,"text":store.put(text.encode()),"lines":len(text.splitlines()),"kind":"research"}
    return _research_record(store,rid,title or filename,[doc],origin or (location if location.startswith("https://") else "local-file"),{"coverage":coverage,"limitations":limits},bool(research_id))


def _research_record(store,rid,title,docs,origin,metadata,explicit=False):
    old=next((r for r in store.list("research") if r["id"]==rid),None)
    if old and old["name"]!=title and not explicit:
        raise ValueError(f"Research id {rid!r} already belongs to {old['name']!r}; names that differ only in case or punctuation collide, pass an explicit research id")
    snap={"kind":"research","id":rid,"name":title,"documents":docs,"origin":origin,**metadata}
    revision=store.put_json(snap)
    row={"id":rid,"name":title,"snapshot":revision,"updated_at":now(),"documents":len(docs)}
    return store.save("research",rid,row)
