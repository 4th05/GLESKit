#!/usr/bin/env python3
"""Opt-in local ARM64 Docker validation with synthetic data; never publishes.

Only this run's uniquely named containers, image, volume and scratch files are
removed. A passing functional report is NOT licensing/content/network approval.
Host dependencies: Python 3.11+ and a local Docker CLI/Compose/Buildx installation.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

if __package__:
    from .asset_metadata import has_private_metadata
    from .publication import classification
    from .privacy_gate import inspect, private_filename
    from .runtime_privacy_gate import policy_identity
else:
    from asset_metadata import has_private_metadata
    from publication import classification
    from privacy_gate import inspect, private_filename
    from runtime_privacy_gate import policy_identity

ORIGIN = "http://127.0.0.1:8123"


class PrefixReader:
    """Replay a sniffed prefix without seeking/spooling a multi-GB layer."""
    def __init__(self, prefix, source):
        self.prefix, self.source = prefix, source

    def read(self, length=-1):
        if length < 0:
            value, self.prefix = self.prefix, b""
            return value + self.source.read()
        value, self.prefix = self.prefix[:length], self.prefix[length:]
        return value + self.source.read(length - len(value))


def scan_image_archive(stream, needles):
    """Scan Docker/OCI layer streams without duplicating the image on disk."""
    files, layers, brands = 0, 0, 0
    deadline = time.monotonic() + 300
    with tarfile.open(fileobj=stream, mode="r|*") as saved:
        for outer in saved:
            if not outer.isfile(): continue
            raw = saved.extractfile(outer)
            prefix = raw.read(512)
            is_layer = prefix.startswith(b"\x1f\x8b") or prefix[257:262] == b"ustar"
            if not is_layer:
                data = prefix
                while data:
                    if any(needle in data for needle in needles): raise RuntimeError("Private marker in image metadata")
                    following = raw.read(2 * 1024 * 1024)
                    if not following: break
                    data = data[-512:] + following
                continue
            layers += 1
            with tarfile.open(fileobj=PrefixReader(prefix, raw), mode="r|*") as layer:
                for entry in layer:
                    if time.monotonic() > deadline: raise RuntimeError("Image layer audit exceeded its bounded deadline")
                    if any(needle in entry.name.encode() for needle in needles): raise RuntimeError("Private path in image layer")
                    if not entry.isfile(): continue
                    files += 1
                    body = layer.extractfile(entry)
                    if "GESKit_icon" in entry.name and Path(entry.name).suffix in {".png", ".svg"}:
                        data = body.read(16 * 1024 * 1024 + 1)
                        if len(data) > 16 * 1024 * 1024 or has_private_metadata(data):
                            raise RuntimeError("Private brand metadata in an image layer")
                        if any(needle in data for needle in needles): raise RuntimeError("Private marker in brand bytes")
                        brands += 1
                        continue
                    tail = b""
                    while chunk := body.read(2 * 1024 * 1024):
                        data = tail + chunk
                        if any(needle in data for needle in needles): raise RuntimeError("Private build marker in image layer bytes")
                        tail = data[-512:]
    if not layers: raise RuntimeError("No auditable image layers were present")
    return {"layer_files_scanned": files, "image_layers_scanned": layers, "brand_layer_files_audited": brands}

# Reuses the existing protocol fixture rather than maintaining another model server.
PROBE = r'''
import asyncio, json, os, sys, threading, time, unittest
from pathlib import Path
sys.path.insert(0, "/tmp/fixtures")
import tests.test_web_engine as fixture
fixture.ROOT = Path("/app")
fixture.ENGINE = Path("/opt/gleskit/bin/opencode")
if sys.argv[1] == "protocols":
    suite = unittest.TestSuite(fixture.RealWebEngineTests(name) for name in (
        "test_real_tool_round_trip_has_one_compact_timeline_and_preserves_instructions",
        "test_provider_protocols_tools_recovery_and_credential_boundary",
        "test_real_engine_receives_preloaded_skills_and_completes_once"))
    fixture.RealWebEngineTests.__unittest_skip__ = False
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    raise SystemExit(0 if result.wasSuccessful() and not result.skipped else 1)
if sys.argv[1] == "figure-server":
    from gleskit_mcp.config import load_settings
    from gleskit_mcp.services import GLESKitService
    from gleskit_mcp.mcp_server import build_server
    service = GLESKitService(settings=load_settings(Path("/tmp/synthetic-mcp")))
    source = service.register_figure_source("enrich_gene_set", {"summary":"Synthetic enrichment fixture", "provider":"synthetic",
        "structured_data":{"species":"human", "genes":["G1","G2","G3"], "terms":[
        {"term_id":"synthetic:1", "term_name":"Synthetic term A", "source":"synthetic", "p_value":1e-6,"intersection_size":2,"matched_genes":["G1","G2"]},
        {"term_id":"synthetic:2", "term_name":"Synthetic term B", "source":"synthetic", "p_value":1e-4,"intersection_size":2,"matched_genes":["G2","G3"]}]}})
    assert source
    Path("/tmp/mcp-figure-source").write_text(source)
    build_server(service=service, start_local_download_server=True).run(transport="stdio")
elif sys.argv[1] == "provider":
    with fixture.RealWebEngineTests().protocol_fixture() as (_, _, state, port, _):
        Path("/tmp/provider-port").write_text(str(port))
        threading.Event().wait()
elif sys.argv[1] == "figure":
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    async def figure():
        env = {name: os.environ[name] for name in ("PATH", "HOME", "R_LIBS_USER", "GLESKIT_R_LIB_HOME", "GLESKIT_RSCRIPT_BIN") if name in os.environ}
        env.update(PYTHONPATH="/app/src", GLESKIT_WEB_PROFILE="/app/var/local-web/profile", GLESKIT_WEB_ORIGIN="http://127.0.0.1:8123")
        params = StdioServerParameters(command=sys.executable, args=["/tmp/docker_probe.py", "figure-server"], env=env)
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                result = await session.call_tool("run_expression_pca", {"table_text": "gene\tS1\tS2\tS3\tS4\nG1\t1\t3\t7\t4\nG2\t5\t2\t1\t6\nG3\t2\t4\t8\t1\nG4\t8\t1\t3\t2\nG5\t4\t6\t1\t3\n", "row_id_column":"gene", "label":"Synthetic release PCA"})
                assert not result.isError, "Synthetic PCA failed"
                figure = await session.call_tool("generate_figure", {"figure_type":"enrichment_barplot", "figure_source_id":Path("/tmp/mcp-figure-source").read_text(), "title":"Synthetic release figure"})
                assert not figure.isError, "Synthetic figure failed"
                data = figure.structuredContent
                if data is None:
                    data = json.loads(next(block.text for block in figure.content if getattr(block,"type",None)=="text"))
                urls = [row["http_url"] for row in data["download_links"]]
                assert all(url.startswith("http://127.0.0.1:8123/api/figures/") for url in urls)
                Path("/tmp/figure-urls.json").write_text(json.dumps(urls))
    asyncio.run(figure())
'''

R_PROBE = r'''
options(warn=1)
lock <- jsonlite::fromJSON("/app/docker/r-packages.lock.json", simplifyVector=FALSE)
for (pkg in lock$packages) {
  stopifnot(requireNamespace(pkg$name, quietly=TRUE))
  stopifnot(identical(unname(packageDescription(pkg$name, fields="Version")), pkg$version))
}
set.seed(19)
stats <- sort(setNames(rnorm(100), paste0("G",1:100)), decreasing=TRUE)
paths <- list(A=paste0("G",1:25), B=paste0("G",26:55))
gsea <- fgsea::fgseaMultilevel(paths, stats, minSize=1, maxSize=100, nPermSimple=1000)
stopifnot(nrow(gsea)==2L)
terms <- data.frame(term=rep(c("A","B"),each=30),gene=paste0("G",1:60))
ora <- clusterProfiler::enricher(paste0("G",1:20), TERM2GENE=terms, minGSSize=1, pvalueCutoff=1, qvalueCutoff=1)
stopifnot(inherits(ora,"enrichResult"))
# compareCluster resolves the supplied function name in its caller's environment.
enricher <- clusterProfiler::enricher
comp <- clusterProfiler::compareCluster(list(first=paste0("G",1:20),second=paste0("G",35:55)),
  fun="enricher",TERM2GENE=terms,minGSSize=1,pvalueCutoff=1,qvalueCutoff=1)
stopifnot(inherits(comp,"compareClusterResult"))
counts <- matrix(rnbinom(200*6,mu=100,size=10),nrow=200,dimnames=list(paste0("G",1:200),paste0("S",1:6)))
design <- data.frame(condition=factor(rep(c("A","B"),each=3)),row.names=colnames(counts))
dds <- DESeq2::DESeqDataSetFromMatrix(counts,design,~condition)
dds <- DESeq2::DESeq(dds,fitType="mean",quiet=TRUE)
shrunk <- DESeq2::lfcShrink(dds,coef=2,type="apeglm")
stopifnot(nrow(shrunk)==200L)
png("/tmp/synthetic.png"); plot(1:4); dev.off()
svg("/tmp/synthetic.svg"); plot(1:4); dev.off()
stopifnot(file.info("/tmp/synthetic.png")$size>100,file.info("/tmp/synthetic.svg")$size>100)
cat("Locked R imports, ORA, ranked GSEA, program comparison, DESeq2/apeglm and headless graphics: PASS\n")
'''


def snapshot(root, target):
    """Development build input only: no history/profile; no approval flags set."""
    root, target = root.resolve(), target.resolve()
    names = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root).decode().split("\0")
    selected = {}
    for name in sorted(set(filter(None, names))):
        decision = classification(name)[0]
        if decision != "INCLUDE" and name != "README.md":
            continue
        source = root / name
        if source.is_symlink() or not source.is_file() or not source.resolve().is_relative_to(root) or private_filename(name):
            raise RuntimeError("Non-regular or private source input rejected")
        data = source.read_bytes()
        if inspect(data, name):
            raise RuntimeError("A source input failed privacy scanning; inspect the source audit")
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        selected[name] = hashlib.sha256(data).hexdigest()
    if not {"Dockerfile", ".dockerignore", "compose.yaml", "docker/third-party-inventory.json"}.issubset(selected):
        raise RuntimeError("Docker build inputs are incomplete")
    return selected


class Probe:
    def __init__(self, root, context, image, report):
        self.root, self.context, self.image, self.report_path = root, context, image, report
        self.id = uuid.uuid4().hex[:12]
        self.project = "gleskit-validation-" + self.id
        self.test_image = "gleskit-validation:" + self.id
        self.state_volume = self.project + "_state"
        self.report = {"schema": 1, "ok": False, "release_approved": False,
                       "network_observation_gate_passed": False,
                       "network_observation_note": "Isolation probes do not certify full syscall-level network observation.",
                       "policy_identity": policy_identity(root)}
        # The Docker client needs its local context/TLS settings, not editor/model credentials.
        self.env = {name: os.environ[name] for name in
                    ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "DOCKER_CONFIG", "SSL_CERT_FILE", "SSL_CERT_DIR")
                    if name in os.environ}
        self.env.update(COMPOSE_DISABLE_ENV_FILE="1", COMPOSE_REMOVE_ORPHANS="0", COMPOSE_BAKE="false",
                        DOCKER_BUILDKIT="1", BUILDX_GIT_INFO="0", BUILDX_GIT_LABELS="0", BUILDX_NO_DEFAULT_ATTESTATIONS="1")
        self.started = False
        self.built = False

    def docker(self, *args, data=None, timeout=120):
        result = subprocess.run(["docker", "--context", self.context, *args], input=data,
                                capture_output=True, env=self.env, timeout=timeout)
        if result.returncode:
            self.report["last_docker_operation"] = args[0]
            self.report["last_docker_exit_code"] = result.returncode
            self.report_path.parent.mkdir(parents=True, exist_ok=True)
            log = self.report_path.with_suffix(".private.log")
            fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream: stream.write(result.stderr[-32000:])
            raise RuntimeError("Docker operation failed: " + args[0] + " (raw diagnostics withheld)")
        return result.stdout

    def compose(self, *args):
        return self.docker("compose", "--ansi", "never", "--env-file", "/dev/null", "-p", self.project,
                           "-f", str(self.source / "compose.yaml"), "-f", str(self.override), *args)

    def phase(self, name):
        self.report["phase"] = name
        print("GLESKit Docker validation: " + name, flush=True)

    def preflight(self):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", self.context):
            raise RuntimeError("Invalid Docker context")
        endpoint = self.docker("context", "inspect", self.context, "--format", "{{.Endpoints.docker.Host}}").decode().strip()
        if not endpoint.startswith("unix:///"):
            raise RuntimeError("Only a local Unix-socket Docker engine is allowed")
        value = self.docker("info", "--format", "{{.ServerVersion}}|{{.OSType}}|{{.Architecture}}").decode().strip().split("|")
        if len(value) != 3 or value[1:] not in (["linux", "arm64"], ["linux", "aarch64"]) or int(value[0].split(".")[0]) < 28:
            raise RuntimeError("Docker Engine 28+ on native Linux ARM64 is required")
        self.report["engine_version"] = value[0]
        builder = self.docker("buildx", "inspect", self.context).decode().splitlines()
        drivers = [line.split(":", 1)[1].strip() for line in builder if line.startswith("Driver:")]
        endpoints = [line.split(":", 1)[1].strip() for line in builder if line.startswith("Endpoint:")]
        if drivers != ["docker"] or endpoints != [self.context]:
            raise RuntimeError("The build must use the built-in context-local builder")

    def install_probes(self, container):
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w") as archive:
            for name in ("test_web_engine.py", "test_web_shell.py"):
                raw = (self.source / "tests" / name).read_bytes()
                info = tarfile.TarInfo("fixtures/tests/" + name); info.size = len(raw); info.mode = 0o600
                archive.addfile(info, io.BytesIO(raw))
            for name, body in (("docker_probe.py", PROBE), ("r-probe.R", R_PROBE), ("fixtures/tests/__init__.py", "")):
                info = tarfile.TarInfo(name); raw = body.encode(); info.size = len(raw); info.mode = 0o600
                archive.addfile(info, io.BytesIO(raw))
        self.docker("exec", "-i", container, "tar", "-xf", "-", "-C", "/tmp", data=data.getvalue())

    def client(self):
        url = self.compose("exec", "-T", "gleskit", "python", "-m", "gleskit_mcp.web.container", "--launch-info").decode().strip()
        prefix = ORIGIN + "/#launch="
        if not url.startswith(prefix) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", url[len(prefix):]):
            raise RuntimeError("Invalid private launch handoff")
        self.cookie = ""
        code, _ = self.http("/api/auth/bootstrap", {"nonce": url[len(prefix):]})
        if code != 200: raise RuntimeError("Private authentication failed")
        code, data = self.http("/api/session")
        if code != 200: raise RuntimeError("Session bootstrap failed")
        self.csrf = json.loads(data)["csrf"]

    def http(self, path, body=None, headers=None, authenticated=True):
        # An internal Docker Desktop network intentionally blocks host forwarding.
        # Probe the real HTTP boundary on container loopback, without enabling egress.
        values = {"Origin": ORIGIN, "Content-Type": "application/json", "Host": "127.0.0.1:8123", **(headers or {})}
        if authenticated and self.cookie: values["Cookie"] = self.cookie
        code = ("import base64,http.client,json,sys; p=json.load(sys.stdin); c=http.client.HTTPConnection('127.0.0.1',8123,timeout=45); "
                "c.request('POST' if p['body'] is not None else 'GET',p['path'],body=json.dumps(p['body']) if p['body'] is not None else None,headers=p['headers']); "
                "r=c.getresponse(); print(json.dumps({'status':r.status,'headers':{k.lower():v for k,v in r.getheaders()},'body':base64.b64encode(r.read()).decode()})); c.close()")
        result = json.loads(self.docker("exec", "-i", self.container, "python", "-c", code,
                                      data=json.dumps({"path":path,"body":body,"headers":values}).encode()))
        cookie = result["headers"].get("set-cookie")
        if cookie:
            if "HttpOnly" not in cookie or "SameSite=strict" not in cookie: raise RuntimeError("Cookie security attributes differ")
            self.cookie = cookie.split(";",1)[0]
        return result["status"], base64.b64decode(result["body"])

    def call(self, name, **values):
        headers = {"X-GLESKit-CSRF": self.csrf, "X-GLESKit-Tab": "validation_tab", "X-GLESKit-Request": "request_" + uuid.uuid4().hex}
        if self.http("/api/control", {"tab": "validation_tab", "takeover": True}, headers)[0] != 200:
            raise RuntimeError("Controller lease failed")
        status, data = self.http("/api/commands/" + name, values, headers)
        if status != 200: raise RuntimeError("Synthetic UI command failed: " + name)
        return json.loads(data)["result"]

    def app_checks(self):
        started = time.monotonic()
        self.started = True
        self.compose("up", "-d", "--wait", "--wait-timeout", "60", "--no-build", "--pull", "never", "gleskit")
        self.report["cold_ready_seconds"] = round(time.monotonic() - started, 2)
        cid = self.compose("ps", "--quiet", "gleskit").decode().strip()
        self.container = cid
        inspected = json.loads(self.docker("inspect", cid))[0]
        ports = inspected["HostConfig"]["PortBindings"]
        if ports != {"8123/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8123"}]} or not inspected["HostConfig"]["ReadonlyRootfs"] or inspected["Config"]["User"] != "10001:10001":
            raise RuntimeError("Container security boundary differs")
        self.client()
        for path, expected, headers in (("/api/session", 401, None), ("/api/ping", 400, {"Host": "foreign.invalid"})):
            if self.http(path, headers=headers, authenticated=False)[0] != expected: raise RuntimeError("HTTP boundary failed")
        if self.http("/api/control", {"tab": "validation_tab"}, {"Origin": "https://foreign.invalid"})[0] != 403: raise RuntimeError("Origin boundary failed")
        if self.http("/api/control", {"tab": "validation_tab"})[0] != 403: raise RuntimeError("CSRF boundary failed")
        if self.call("list_sessions")["session_count"] or self.call("list_chat_providers"): raise RuntimeError("Fresh volume is not empty")
        self.install_probes(cid)
        self.docker("exec", "-d", cid, "python", "/tmp/docker_probe.py", "provider")
        for _ in range(60):
            time.sleep(.1)
            result = subprocess.run(["docker", "--context", self.context, "exec", cid, "cat", "/tmp/provider-port"], capture_output=True, env=self.env)
            if result.returncode == 0:
                port = int(result.stdout); break
        else: raise RuntimeError("Synthetic provider did not start")
        key = "synthetic-release-private-" + self.id
        self.call("save_chat_provider", providerId="validation_fixture", kind="openai-compatible", label="Synthetic validation",
                  endpoint=f"http://127.0.0.1:{port}/v1", models=["fixture/model", "fixture/alternative"],
                  defaultModel="fixture/model", apiKey=key, enabled=True)
        sid = self.call("create_session", title="Synthetic release session", description="Synthetic only", species="human", tags=[])["session_id"]
        upload = self.call("create_session_upload", sessionId=sid, sourceMode="copied", fileName="synthetic.csv", mimeType="text/csv",
                           dataB64="Z2VuZSxzYW1wbGUKRzEsMTAK", title="Synthetic upload")
        conversation = self.call("create_chat_conversation", title="Synthetic persisted chat")["conversation_id"]
        for _ in range(240):
            if self.call("get_web_status")["mcp"]["running"]: break
            time.sleep(.25)
        else: raise RuntimeError("Private MCP readiness failed")
        self.call("send_chat_message", conversationId=conversation, providerId="validation_fixture", modelId="fixture/model", agentId="gleskit", content="Synthetic fixture completion only.")
        for _ in range(180):
            detail = self.call("get_chat_conversation", conversationId=conversation)
            if detail["turn_state"].get("status") == "completed": break
            time.sleep(.25)
        else: raise RuntimeError("Synthetic chat did not complete")
        if not any("Synthetic protocol completion." in row["content_markdown"] for row in detail["messages"]): raise RuntimeError("Synthetic answer missing")
        self.docker("exec", cid, "python", "/tmp/docker_probe.py", "figure")
        urls = json.loads(self.docker("exec", cid, "cat", "/tmp/figure-urls.json"))
        for url in urls:
            path = url.removeprefix(ORIGIN)
            if self.http(path, authenticated=False)[0] != 401 or self.http(path)[0] != 200:
                raise RuntimeError("Authenticated figure download failed")
        self.report["figure_downloads"] = len(urls)
        self.docker("exec", cid, "Rscript", "--vanilla", "/tmp/r-probe.R", timeout=180)
        cli = self.docker("exec", cid, "python", "-m", "gleskit_mcp", "--project-root", "/app", "workspace-status")
        if not isinstance(json.loads(cli), dict): raise RuntimeError("CLI stdout was not JSON")
        memory = self.docker("exec", cid, "python", "-c", "from pathlib import Path; p=Path('/sys/fs/cgroup/memory.peak'); print(p.read_text().strip() if p.exists() else 'unavailable')").decode().strip()
        self.report["application_peak_memory_bytes"] = int(memory) if memory.isdecimal() else None
        scan = ("from pathlib import Path; import sys; key=sys.stdin.buffer.read(); root=Path('/app/var/local-web/profile'); "
                "print(sum(key in p.read_bytes() for p in root.rglob('*') if p.is_file() and p != root/'.env'))")
        count = int(self.docker("exec", "-i", cid, "python", "-c", scan, data=key.encode()))
        if count or key.encode() in self.docker("logs", cid): raise RuntimeError("Credential canary escaped the private dotenv")
        self.report["unexpected_profile_credential_canary_count"] = count
        # Real pinned OpenCode + existing synthetic provider/failure/cancellation fixtures, with no external network.
        self.phase("offline protocol and credential-boundary fixtures")
        standalone = self.project + "-protocols"
        try:
            self.docker("run", "-d", "--name", standalone, "--network", "none", "--read-only", "--init", "--cap-drop", "ALL",
                        "--security-opt", "no-new-privileges:true", "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=512m,mode=1777",
                        "--tmpfs", "/app/var:rw,noexec,nosuid,nodev,size=256m,mode=700,uid=10001,gid=10001",
                        "--entrypoint", "python", self.image, "-c", "import time; time.sleep(600)")
            self.install_probes(standalone)
            self.docker("exec", standalone, "python", "/tmp/docker_probe.py", "protocols", timeout=240)
        finally:
            self.docker("rm", "-f", standalone)
        self.phase("restart persistence and scoped shutdown")
        self.compose("down", "--timeout", "20")
        started = time.monotonic()
        self.compose("up", "-d", "--wait", "--wait-timeout", "60", "--no-build", "--pull", "never", "gleskit")
        self.report["warm_ready_seconds"] = round(time.monotonic() - started, 2)
        self.container = self.compose("ps", "--quiet", "gleskit").decode().strip()
        self.client()
        provider = self.call("list_chat_providers")[0]
        if not provider["connected"] or provider["models"] != ["fixture/model", "fixture/alternative"]: raise RuntimeError("Provider persistence failed")
        if self.call("get_session_summary", sessionId=sid)["session_id"] != sid: raise RuntimeError("Session persistence failed")
        if self.call("list_session_uploads", sessionId=sid)["upload_count"] != 1: raise RuntimeError("Upload persistence failed")
        detail = self.call("get_chat_conversation", conversationId=conversation)
        if not any("Synthetic protocol completion." in row["content_markdown"] for row in detail["messages"]): raise RuntimeError("Chat persistence failed")
        status = self.call("get_web_status")
        if status["engine"]["running"] or status["runtime"]["active_session_id"] is not None: raise RuntimeError("Restart silently enabled chat/session")
        cid = self.compose("ps", "--quiet", "gleskit").decode().strip()
        self.container = cid
        self.docker("kill", cid)
        if self.docker("inspect", cid, "--format", "{{.State.Running}}").strip() != b"false":
            raise RuntimeError("Abrupt stop left the test container running")

    def verify_source(self, exported):
        expected = {name: hashlib.sha256(path.read_bytes()).hexdigest() for path in exported.rglob("*")
                    if path.is_file() and (name := path.relative_to(exported).as_posix()).startswith("src/gleskit_mcp/")}
        code = ("import hashlib,json,pathlib,sys,gleskit_mcp; root=pathlib.Path(gleskit_mcp.__file__).parent; "
                "names=json.load(sys.stdin); print(json.dumps({name:hashlib.sha256((root/name.removeprefix('src/gleskit_mcp/')).read_bytes()).hexdigest() for name in names}))")
        actual = json.loads(self.docker("run", "--rm", "-i", "--network", "none", "--read-only", "--entrypoint", "python",
                                        self.image, "-c", code, data=json.dumps(list(expected)).encode()))
        if actual != expected: raise RuntimeError("The image does not contain the reviewed source; rerun with --build")
        self.report["installed_source_files_verified"] = len(expected)

    def audit_layers(self, folder, needles):
        self.phase("image history and every filesystem layer")
        history = self.docker("history", "--no-trunc", "--format", "{{json .CreatedBy}}", self.image)
        if any(needle in history for needle in needles): raise RuntimeError("Private build marker in image history")
        log = folder / "image-save.private.log"
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as errors, subprocess.Popen(
                ["docker", "--context", self.context, "image", "save", self.image],
                stdout=subprocess.PIPE, stderr=errors, env=self.env) as process:
            try:
                self.report.update(scan_image_archive(process.stdout, needles))
                if process.wait(timeout=30): raise RuntimeError("Docker image stream failed")
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)

    def run(self, build, reuse_runtime=False):
        self.phase("local runtime preflight")
        self.preflight()
        with tempfile.TemporaryDirectory(prefix="gleskit-docker-smoke-") as folder:
            folder = Path(folder).resolve()
            self.source = folder / "source"; self.source.mkdir()
            self.report["source_sha256"] = snapshot(self.root, self.source)
            self.override = folder / "test-compose.json"
            canary = ("synthetic-excluded-build-" + self.id).encode()
            for name in (".env", "var/private.txt", ".gleskit/private.db", "config/gleskit.local.json",
                         "apps/web/public/private.txt", "apps/web/node_modules/private.txt", "src/gleskit_mcp/private-notes.txt"):
                path = self.source / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(canary)
            self.phase("default-deny context canaries")
            audit_dockerfile = folder / "audit.Dockerfile"; audit_dockerfile.write_text("FROM scratch\nCOPY . /\n")
            exported = folder / "context"
            self.docker("buildx", "build", "--builder", self.context, "--provenance=false", "--file", str(audit_dockerfile),
                        "--output", "type=local,dest=" + str(exported), str(self.source), timeout=120)
            if any(canary in path.read_bytes() for path in exported.rglob("*") if path.is_file()): raise RuntimeError("A private context canary was admitted")
            if build:
                self.phase("build from sanitized history-free source (scientific layers cached)")
                self.built = True
                dockerfile = self.source / "Dockerfile"
                if reuse_runtime:
                    # Diagnostic application rebuild only. This cannot certify a fresh installation.
                    names = ["docker/runtime.json", "docker/python.lock", "docker/r-packages.lock.json"]
                    expected = {name: hashlib.sha256((self.source / name).read_bytes()).hexdigest() for name in names}
                    code = "import hashlib,json,pathlib; print(json.dumps({name:hashlib.sha256((pathlib.Path('/app')/name).read_bytes()).hexdigest() for name in " + repr(names) + "}))"
                    actual = json.loads(self.docker("run", "--rm", "--network", "none", "--read-only", "--entrypoint", "python", self.image, "-c", code))
                    if actual != expected: raise RuntimeError("Existing runtime lock identity differs; reuse refused")
                    base = json.loads(self.docker("image", "inspect", self.image))[0]["Id"]
                    frontend = dockerfile.read_text().split("FROM rocker/", 1)[0]
                    dockerfile = folder / "application-rebuild.Dockerfile"
                    if not re.fullmatch(r"[A-Za-z0-9./:_@-]+", self.image): raise RuntimeError("Invalid local runtime image reference")
                    dockerfile.write_text(frontend + "\nFROM " + self.image + "\nUSER root\nWORKDIR /app\n"
                        "COPY src/gleskit_mcp/ /app/src/gleskit_mcp/\n"
                        "COPY src/gleskit_mcp/ /opt/gleskit/venv/lib/python3.12/site-packages/gleskit_mcp/\n"
                        "COPY LICENSE NOTICE THIRD_PARTY_NOTICES.md /app/\n"
                        "COPY docker/third-party-inventory.json /app/docker/\n"
                        "COPY --from=frontend /build/web/dist-web/ /app/apps/web/dist-web/\nUSER 10001:10001\n")
                    self.report.update(build_mode="application-rebuild-on-verified-local-runtime", runtime_image_id=base,
                                       fresh_runtime_rebuild_passed=False)
                else:
                    self.report.update(build_mode="complete-pinned-runtime-build", fresh_runtime_rebuild_passed=False)
                self.docker("build", "--builder", self.context, "--provenance=false", "--file", str(dockerfile),
                            "--tag", self.test_image, str(self.source), timeout=3600)
                self.image = self.test_image
                if not reuse_runtime: self.report["fresh_runtime_rebuild_passed"] = True
            self.override.write_text(json.dumps({"services":{"gleskit":{"image":self.image}}, "networks":{"default":{"internal":True}}}))
            self.verify_source(exported)
            try:
                self.phase("fresh UI, uploads, synthetic chat, figures and R")
                self.app_checks()
            finally:
                if self.started: self.compose("down", "--timeout", "20")
                volumes = self.docker("volume", "ls", "--quiet", "--filter", "label=com.docker.compose.project=" + self.project).decode().split()
                for volume in volumes:
                    if volume != self.state_volume: raise RuntimeError("Unexpected test volume identity; cleanup refused")
                    self.docker("volume", "rm", volume)
            self.audit_layers(folder, [canary, str(Path.home()).encode()])
            metadata = json.loads(self.docker("image", "inspect", self.image))[0]
            self.report.update(ok=True, image_id=metadata["Id"], image_bytes=metadata["Size"],
                               egress_isolation="internal application network; network-none protocol fixtures",
                               http_probe_transport="container loopback; host-only publish binding inspected separately",
                               stateless_first_preserved=True, provider_models_chat_session_upload_persisted=True)
        self.report["phase"] = "complete; public release holds unchanged"

    def write_report(self):
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.report_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream: json.dump(self.report, stream, indent=2); stream.write("\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--image", default="gleskit-local:arm64")
    parser.add_argument("--build", action="store_true", help="Build a disposable image from sanitized current source")
    parser.add_argument("--reuse-validated-runtime", action="store_true", help="Diagnostic app-only rebuild; cannot approve a fresh install")
    parser.add_argument("--report", type=Path, required=True, help="New private JSON report; never publication approval")
    args = parser.parse_args(argv)
    if args.report.exists(): parser.error("report already exists; choose a new private path")
    if args.reuse_validated_runtime and not args.build: parser.error("--reuse-validated-runtime requires --build")
    probe = Probe(Path(__file__).resolve().parents[2], args.context, args.image, args.report)
    try:
        probe.run(args.build, args.reuse_validated_runtime)
    except Exception as exc:
        probe.report["failure"] = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        print("Docker validation failed in phase: " + probe.report.get("phase", "initialization") + ". No release approval was issued.", file=sys.stderr)
        probe.write_report()
        return 1
    finally:
        if probe.built:
            # Exact unique tag created by this run, never a researcher image or cache prune.
            subprocess.run(["docker", "--context", probe.context, "image", "rm", probe.test_image],
                           env=probe.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
    probe.write_report()
    print("Docker functional validation: PASS. Licensing/content/full network review still required.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
