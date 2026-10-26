# Native Linux ARM64 development image; publication remains a separate release gate.
FROM node@sha256:294d33a992da016e059121deb0c979dffb4478b77a304376bca41393297c1ea2 AS frontend
WORKDIR /build/web
COPY apps/web/package.json apps/web/package-lock.json ./
COPY docker/runtime.json /locks/runtime.json
RUN node --input-type=module <<'JS'
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
const lock = JSON.parse(readFileSync("/locks/runtime.json"));
if (process.arch !== "arm64" || process.version !== "v" + lock.frontend.node_version ||
    execFileSync("npm", ["--version"], { encoding: "utf8" }).trim() !== lock.frontend.npm_version ||
    createHash("sha256").update(readFileSync("package-lock.json")).digest("hex") !== lock.frontend.lock_sha256)
  throw new Error("Frontend runtime or lock differs from the validated baseline.");
JS
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY apps/web/index.html apps/web/vite.config.js ./
COPY apps/web/scripts/build-web.mjs scripts/build-web.mjs
COPY apps/web/src/ src/
RUN npm test && npm run build

FROM rocker/r-ver@sha256:7cc27046e6aa08d13c823f7e5f6b247e5f4cb6978715bf02798d96d5217c56e4 AS runtime-base

RUN printf '%s\n' \
      'deb [check-valid-until=no] https://snapshot.ubuntu.com/ubuntu/20261024T000000Z/ jammy main universe' \
      'deb [check-valid-until=no] https://snapshot.ubuntu.com/ubuntu/20261024T000000Z/ jammy-updates main universe' \
      'deb [check-valid-until=no] https://snapshot.ubuntu.com/ubuntu/20261024T000000Z/ jammy-security main universe' \
      > /etc/apt/sources.list \
    && apt-get -o Acquire::Retries=3 -o APT::Update::Error-Mode=any update \
    && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ca-certificates curl xz-utils libbz2-dev libgdbm-dev libgdbm-compat-dev \
      libffi-dev liblzma-dev libreadline-dev libsqlite3-dev libssl-dev uuid-dev \
      zlib1g-dev libcurl4-openssl-dev libxml2-dev libglpk-dev libharfbuzz-dev \
      libfribidi-dev libfontconfig1-dev libfreetype6-dev libpng-dev libtiff-dev \
      libjpeg-dev libcairo2-dev fonts-dejavu-core procps \
    && rm -rf /var/lib/apt/lists/*

RUN curl --fail --show-error --location \
      https://www.python.org/ftp/python/3.12.13/Python-3.12.13.tar.xz \
      --output /tmp/python.tar.xz \
    && printf '%s\n' 'c08bc65a81971c1dd5783182826503369466c7e67374d1646519adf05207b684  /tmp/python.tar.xz' | sha256sum --check - \
    && mkdir /tmp/python-build \
    && tar --extract --xz --file /tmp/python.tar.xz --directory /tmp/python-build --strip-components=1 \
    && cd /tmp/python-build \
    && ./configure --prefix=/opt/gleskit/python --with-ensurepip=install --disable-test-modules \
    && make -j2 \
    && make install \
    && /opt/gleskit/python/bin/python3.12 -m venv /opt/gleskit/venv \
    && rm -rf /tmp/python-build /tmp/python.tar.xz

ENV PATH="/opt/gleskit/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
    PIP_CONFIG_FILE=/dev/null PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 \
    R_LIBS_USER=/opt/gleskit/r-library

RUN mkdir -p /opt/gleskit/r-library \
    && python --version \
    && Rscript --vanilla -e 'stopifnot(getRversion() == "4.3.2", capabilities("cairo"), capabilities("png")); cat("R headless graphics available\n")'

# All dependency inputs are public, exact and hash-checked. Cache mounts are not image layers.
RUN --mount=type=bind,source=docker/runtime.json,target=/locks/runtime.json \
    python - <<'PY'
import json, platform, subprocess, sys
with open("/locks/runtime.json") as stream:
    lock = json.load(stream)
actual = dict(line.split("\t") for line in subprocess.check_output(
    ["dpkg-query", "-W", "-f=${binary:Package}\t${Version}\n"], text=True).splitlines())
assert platform.machine() == "aarch64" and platform.system() == "Linux"
assert sys.version.split()[0] == lock["python"]["version"]
assert actual == lock["system"]["installed_packages"], "OS package snapshot differs from the reviewed baseline"
PY

FROM runtime-base AS dependencies
RUN --mount=type=bind,source=docker/runtime.json,target=/locks/runtime.json \
    --mount=type=bind,source=docker/python.lock,target=/locks/python.lock \
    python - <<'PY'
import hashlib, json, subprocess, sys
with open("/locks/runtime.json") as stream:
    lock = json.load(stream)
with open("/locks/python.lock", "rb") as stream:
    assert hashlib.sha256(stream.read()).hexdigest() == lock["python"]["lock_sha256"]
subprocess.run([sys.executable, "-m", "pip", "install", "--require-hashes", "--only-binary=:all:",
                "-r", "/locks/python.lock"], check=True)
subprocess.run([sys.executable, "-m", "pip", "check"], check=True)
PY
RUN --mount=type=bind,source=docker/runtime.json,target=/locks/runtime.json \
    --mount=type=bind,source=docker/r-packages.lock.json,target=/locks/r-packages.lock.json \
    --mount=type=bind,source=docker/install-r.R,target=/locks/install-r.R \
    --mount=type=cache,id=gleskit-r-sources-4.3.2-arm64-v1,target=/opt/gleskit/r-sources,sharing=locked \
    python - <<'PY'
import hashlib, json, subprocess
with open("/locks/runtime.json") as stream:
    meta = json.load(stream)
for filename, expected in (("r-packages.lock.json", meta["r_base"]["lock_sha256"]),
                            ("install-r.R", meta["r_base"]["installer_sha256"])):
    with open("/locks/" + filename, "rb") as stream:
        assert hashlib.sha256(stream.read()).hexdigest() == expected
with open("/locks/r-packages.lock.json") as stream:
    lock = json.load(stream)
bootstrap = next(row for row in lock["packages"] if row["name"] == "jsonlite")
subprocess.run(["Rscript", "--vanilla", "/locks/install-r.R", "/locks/r-packages.lock.json",
                meta["r_base"]["library_path"], "/opt/gleskit/r-sources",
                bootstrap["url"], bootstrap["sha256"]], check=True)
PY
RUN --mount=type=bind,source=docker/runtime.json,target=/locks/runtime.json \
    python - <<'PY'
import hashlib, json, pathlib, subprocess, tarfile, urllib.request
with open("/locks/runtime.json") as stream:
    lock = json.load(stream)["opencode"]
archive = pathlib.Path("/tmp/gleskit-opencode.tar.gz")
try:
    with urllib.request.urlopen(lock["url"], timeout=60) as response, archive.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            output.write(chunk)
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == lock["sha256"]
    target = pathlib.Path("/opt/gleskit/bin/opencode")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as source:
        member = source.getmember("opencode")
        assert member.isfile() and member.size < 512 * 1024 * 1024
        with source.extractfile(member) as data, target.open("wb") as output:
            while chunk := data.read(1024 * 1024):
                output.write(chunk)
    target.chmod(0o755)
    assert subprocess.check_output([str(target), "--version"], text=True).strip() == lock["version"]
finally:
    archive.unlink(missing_ok=True)
PY

FROM dependencies AS project-wheel
WORKDIR /build/project
COPY pyproject.toml README.md LICENSE NOTICE THIRD_PARTY_NOTICES.md ./
COPY src/gleskit_mcp/ src/gleskit_mcp/
RUN --mount=type=bind,source=docker/runtime.json,target=/locks/runtime.json \
    python - <<'PY'
import hashlib, json, pathlib, subprocess, sys, urllib.request
with open("/locks/runtime.json") as stream:
    tool = json.load(stream)["python"]["build_only_packages"][0]
wheel = pathlib.Path("/tmp") / (tool["name"] + "-" + tool["version"] + "-py3-none-any.whl")
try:
    with urllib.request.urlopen(tool["url"], timeout=60) as source:
        wheel.write_bytes(source.read(8 * 1024 * 1024))
    assert hashlib.sha256(wheel.read_bytes()).hexdigest() == tool["sha256"]
    subprocess.run([sys.executable, "-m", "pip", "install", "--no-index", "--no-deps", str(wheel)], check=True)
    subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-index", "--no-deps",
                    "--no-build-isolation", "--wheel-dir", "/wheels", "."], check=True)
finally:
    wheel.unlink(missing_ok=True)
PY

FROM dependencies AS local-app
LABEL org.gleskit.app="local-web" org.gleskit.release-status="development-validation-only"
WORKDIR /app
COPY src/gleskit_mcp/ /app/src/gleskit_mcp/
COPY config/gleskit.defaults.json /app/config/gleskit.defaults.json
COPY docs/opencode/prompts/agents/gleskit-agent-prompt.md /app/docs/opencode/prompts/agents/gleskit-agent-prompt.md
COPY docs/opencode/skills/gleskit-research-assistant/ /app/docs/opencode/skills/gleskit-research-assistant/
COPY LICENSE NOTICE THIRD_PARTY_NOTICES.md /app/
COPY docker/runtime.json docker/python.lock docker/r-packages.lock.json docker/third-party-inventory.json /app/docker/
COPY --from=frontend /build/web/dist-web/ /app/apps/web/dist-web/
RUN --mount=type=bind,from=project-wheel,source=/wheels,target=/wheels \
    python -m pip install --no-index --no-deps /wheels/gleskit_mcp-*.whl \
    && groupadd --gid 10001 gleskit \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /app/var/home --shell /usr/sbin/nologin gleskit \
    && mkdir -p /app/var/home /app/var/cache /app/var/config /app/var/data /app/var/state \
    && chown -R 10001:10001 /app/var \
    && chmod 0700 /app/var /app/var/home /app/var/cache /app/var/config /app/var/data /app/var/state

ENV HOME=/app/var/home XDG_CACHE_HOME=/app/var/cache XDG_CONFIG_HOME=/app/var/config \
    XDG_DATA_HOME=/app/var/data XDG_STATE_HOME=/app/var/state \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring \
    GLESKIT_R_LIB_HOME=/opt/gleskit/r-library GLESKIT_RSCRIPT_BIN=/usr/local/bin/Rscript \
    GLESKIT_LOCAL_MAX_WORKERS=2 GLESKIT_R_THREADS=1 GLESKIT_R_MAX_VSIZE=4Gb \
    OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
USER 10001:10001
EXPOSE 8123
HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=3 \
    CMD ["/opt/gleskit/venv/bin/python", "-m", "gleskit_mcp.web.container", "--healthcheck"]
STOPSIGNAL SIGTERM
ENTRYPOINT ["/opt/gleskit/venv/bin/python", "-m", "gleskit_mcp.web.container"]
