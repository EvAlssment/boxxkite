# syntax=docker/dockerfile:1.4

# LSP-enabled Sandbox Container (docs/LSP-SUPPORT-SCOPING.md, GitHub issue #183)
# Identical to sandbox.Dockerfile (same data science/file processing library
# set) PLUS two language servers -- pyright (Python) and
# typescript-language-server (TypeScript/JS) -- for BOXKITE_LSP_ENABLED's
# /lsp/* routes (sidecar/sidecar_lsp.py). A new, separate, opt-in image
# variant, NOT a change to sandbox.Dockerfile itself -- every existing image
# stays exactly as it is today. This directly addresses #81 point (5)
# (image bloat/attack surface for a UX class boxkite doesn't serve by
# default): callers who don't need LSP completions keep the smaller,
# unmodified default image; only callers who explicitly opt in build/pull
# this one instead. Mirrors sandbox-node.Dockerfile's own framing as "a
# genuinely different footprint... for callers who specifically need this."
#
# SECURITY NOTES (identical posture to sandbox.Dockerfile, see its own
# header comment):
# - pip is REMOVED after installation to prevent runtime package installs
# - uv is REMOVED after installation to prevent runtime package installs
# - npm is REMOVED after JS packages (including the two language servers
#   below) are installed; runtime only needs node -- pyright-langserver/
#   typescript-language-server run via the already-globally-linked `node`
#   binary and their installed node_modules, they don't need npm present at
#   runtime (same reasoning that already lets sandbox.Dockerfile remove npm
#   after Playwright's install)
# - Container runs with minimal environment variables (no API keys, credentials)
# - Generated commands can run in an isolated network namespace at runtime
# - Runs as non-root user (UID 1001)

ARG PYTHON_VERSION=3.11
ARG UV_VERSION=0.11.2
ARG PANDOC_VERSION=3.9.0.2
ARG CHROME_FOR_TESTING_VERSION=148.0.7778.179

FROM cgr.dev/chainguard/wolfi-base:latest@sha256:02dab76bd852a70556b5b2002195c8a5fdab77d323c433bf6642aab080489795
ARG PYTHON_VERSION
ARG UV_VERSION
ARG PANDOC_VERSION
ARG CHROME_FOR_TESTING_VERSION

# libreoffice-26.2 is intentionally pinned for the current Snyk-clean Wolfi package set;
# revisit this pin when Wolfi rolls LibreOffice to a newer fixed minor.
RUN apk update && apk add --no-cache \
    python-${PYTHON_VERSION} \
    py3.11-pip \
    ca-certificates \
    bash \
    curl \
    libstdc++ \
    jq \
    openssh-client \
    sqlite \
    libreoffice-26.2 \
    poppler-utils \
    qpdf \
    imagemagick-7 \
    ghostscript \
    libheif \
    tesseract \
    tesseract-eng \
    nodejs-22 \
    npm \
    font-liberation \
    fontconfig \
    alsa-lib \
    libatk-bridge-2.0 \
    libatk-1.0 \
    at-spi2-core \
    dbus-libs \
    libdrm \
    libudev \
    mesa-gbm \
    libnspr \
    libnss \
    wayland-libs-client \
    libx11 \
    libxcb \
    libxcomposite \
    libxdamage \
    libxfixes \
    libxkbcommon \
    libxrandr \
    libxrender \
    cairo \
    pango \
    gdk-pixbuf \
    gtk-3 \
    && ln -sf /usr/bin/python${PYTHON_VERSION} /usr/bin/python3 \
    && ln -sf /usr/bin/python${PYTHON_VERSION} /usr/bin/python \
    && ln -sf /usr/lib/libreoffice/program/soffice /usr/bin/soffice \
    && node --version | grep -Eq '^v22\.(2[2-9]|[3-9][0-9])\.' \
    && npm --version | grep -Eq '^(11\.(1[5-9]|[2-9][0-9])\.|1[2-9]\.)'

# Wolfi does not currently package pandoc. Install the upstream static binary.
# Checksums below were computed directly from the official GitHub release
# assets for the exact PANDOC_VERSION pinned above (jgm/pandoc does not
# publish a checksums.txt for this release). They have since been
# independently cross-checked against GitHub's own server-computed
# per-release-asset `digest` field (exposed by the Releases API, computed by
# GitHub at upload time -- independent of both jgm/pandoc's release process
# and this repo's maintainer) plus a fresh independent re-download, and both
# matched exactly. See scripts/verify-pinned-checksums.sh (repeatable on
# every PANDOC_VERSION bump), deploy/pinned-checksums-verification.json
# (dated record of the last run), and SECURITY.md for the full writeup.
RUN set -eux; \
    case "$(uname -m)" in \
        x86_64) architecture="amd64"; pandoc_sha256="a69abfababda8a56969a254b09f9553a7be89ddec00d4e0fe9fd585d71a67508" ;; \
        aarch64|arm64) architecture="arm64"; pandoc_sha256="b6d21e8f9c3b15744f5a7ab40248019157ed7793875dbe0383d4c82ff572b528" ;; \
        *) echo "Unsupported architecture: $(uname -m)" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://github.com/jgm/pandoc/releases/download/${PANDOC_VERSION}/pandoc-${PANDOC_VERSION}-linux-${architecture}.tar.gz" \
        -o /tmp/pandoc.tar.gz; \
    echo "${pandoc_sha256}  /tmp/pandoc.tar.gz" | sha256sum -c -; \
    mkdir -p /tmp/pandoc /usr/local/bin; \
    tar -xzf /tmp/pandoc.tar.gz -C /tmp/pandoc --strip-components=1; \
    cp /tmp/pandoc/bin/pandoc /usr/local/bin/pandoc; \
    chmod +x /usr/local/bin/pandoc; \
    rm -rf /tmp/pandoc /tmp/pandoc.tar.gz; \
    pandoc --version

# Preinstall JS libraries required by document skills (runtime has no network access).
# Install from a committed package.json + package-lock.json so transitive deps are pinned
# deterministically. `overrides` in package.json force-patches vulnerable transitives
# (lodash, lodash.template, undici, flatted, @babel/traverse, tar, minimatch, handlebars,
# uuid, word-wrap, http-cache-semantics, brace-expansion, serialize-javascript,
# @babel/plugin-transform-modules-systemjs).
# Resolved modules live under /usr/local/lib/node_modules; NODE_PATH (set below)
# points scripts there so `require('docx')` etc. continues to work.
#
# The two language servers for /lsp/* (BOXKITE_LSP_ENABLED,
# docs/LSP-SUPPORT-SCOPING.md) are installed globally INSIDE this same RUN,
# AFTER `npm ci` and BEFORE npm is removed below -- NOT in an earlier,
# separate RUN step. Confirmed directly, the hard way: npm's global
# install prefix on this base image is `/usr/local` (the same prefix
# `cd /usr/local/lib; npm ci` below operates against), so an earlier
# `npm install -g pyright ...` step gets silently WIPED by this directory's
# own `npm ci` (a clean install that removes anything not in
# deploy/package.json's own lockfile) -- a real image was built and run to
# discover this; `pyright-langserver`/`typescript-language-server` were
# present on disk right after their own install step but gone by the time
# the image finished building, until the ordering below fixed it. Both
# binaries land on $PATH at /usr/local/bin with no extra symlink needed
# (npm's global bin dir here already is /usr/local/bin, the first entry in
# the $PATH set at the bottom of this file and in sidecar/main.py's
# SAFE_EXEC_ENV) -- confirmed by actually running the built image as the
# `sandbox` user and invoking both binaries, not just checked at build time
# as root.
#
# `typescript@5` is EXPLICITLY PINNED, not left to float to whatever
# `typescript` resolves to at build time: typescript-language-server
# requires a real `tsserver.js` (the classic TS language service API) to be
# present in the `typescript` package it resolves -- confirmed directly
# that TypeScript 7.x (the new native/Go-rewrite preview line, published to
# the same `typescript` npm package name) does NOT ship `tsserver.js` at
# all, which breaks typescript-language-server's `initialize` handshake
# outright ("Could not find a valid TypeScript installation"). An
# unpinned `npm install -g typescript` would silently pull whatever is
# newest at build time and could break this exact way on a future rebuild.
#
# Install Playwright first for its expected browser layout, then replace the
# bundled Chromium/headless shell with a Chrome-for-Testing build that clears
# the scanner findings against Playwright's older browser binary. Chrome for
# Testing publishes linux64 artifacts only, so arm64 fails rather than shipping
# the older bundled browser. Chrome for Testing does not publish a checksum of
# any kind in its own version manifests (confirmed by inspecting both
# known-good-versions-with-downloads.json and
# last-known-good-versions-with-downloads.json -- neither contains a
# sha256/md5/hash field for any version). The sha256 values pinned below were
# originally self-computed, and have since been cross-checked two ways: a
# fresh independent re-download's sha256 still matches this pin, and that same
# fresh download's md5 matches Google Cloud Storage's own server-computed
# `x-goog-hash` header for the object (generated by GCS at upload time,
# independent of whoever recorded this pin). See
# scripts/verify-pinned-checksums.sh (repeatable on every
# CHROME_FOR_TESTING_VERSION bump), deploy/pinned-checksums-verification.json
# (dated record of the last run), and SECURITY.md for the full writeup.
COPY --link deploy/package.json deploy/package-lock.json /usr/local/lib/
RUN --mount=type=cache,id=npm-sandbox,target=/root/.npm \
    set -eux; \
    cd /usr/local/lib; \
    npm ci --omit=dev --no-audit --no-fund; \
    ln -sf /usr/local/lib/node_modules/playwright/cli.js /usr/local/bin/playwright; \
    chmod +x /usr/local/bin/playwright; \
    mkdir -p /ms-playwright; \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright /usr/local/bin/playwright install chromium; \
    case "$(uname -m)" in \
        x86_64) chrome_platform="linux64"; chrome_dir="chrome-linux64"; headless_dir="chrome-headless-shell-linux64"; \
            chrome_sha256="808f79e139425ebccd8077e599bfcfbae4c54698ddea577c47254acb1971241c"; \
            headless_sha256="43b19e1e6dca93c87c74ed52e8fa046739e59c0e65b5397b100930415c556491" ;; \
        aarch64|arm64) echo "Chrome for Testing does not publish Linux arm64 artifacts for ${CHROME_FOR_TESTING_VERSION}; build linux/amd64 or update Playwright when a remediated arm64 browser is available." >&2; exit 1 ;; \
        *) echo "Unsupported architecture: $(uname -m)" >&2; exit 1 ;; \
    esac; \
    if [ -n "$chrome_platform" ]; then \
        tmp="$(mktemp -d)"; \
        curl -fsSL "https://storage.googleapis.com/chrome-for-testing-public/${CHROME_FOR_TESTING_VERSION}/${chrome_platform}/${chrome_dir}.zip" -o "$tmp/chrome.zip"; \
        curl -fsSL "https://storage.googleapis.com/chrome-for-testing-public/${CHROME_FOR_TESTING_VERSION}/${chrome_platform}/${headless_dir}.zip" -o "$tmp/chrome-headless-shell.zip"; \
        echo "${chrome_sha256}  $tmp/chrome.zip" | sha256sum -c -; \
        echo "${headless_sha256}  $tmp/chrome-headless-shell.zip" | sha256sum -c -; \
        python -c "import pathlib, sys, zipfile; root = pathlib.Path(sys.argv[1]); [zipfile.ZipFile(path).extractall(root) for path in (root / 'chrome.zip', root / 'chrome-headless-shell.zip')]" "$tmp"; \
        chromium_dir="$(find /ms-playwright -maxdepth 1 -type d -name 'chromium-*' | head -n 1)"; \
        headless_shell_dir="$(find /ms-playwright -maxdepth 1 -type d -name 'chromium_headless_shell-*' | head -n 1)"; \
        test -n "$chromium_dir"; \
        test -n "$headless_shell_dir"; \
        rm -rf "$chromium_dir/$chrome_dir"; \
        rm -rf "$headless_shell_dir/$headless_dir"; \
        mv "$tmp/$chrome_dir" "$chromium_dir/$chrome_dir"; \
        mv "$tmp/$headless_dir" "$headless_shell_dir/$headless_dir"; \
        chmod -R a+rx "$chromium_dir/$chrome_dir"; \
        chmod -R a+rx "$headless_shell_dir/$headless_dir"; \
        "$chromium_dir/$chrome_dir/chrome" --version; \
        "$headless_shell_dir/$headless_dir/chrome-headless-shell" --version; \
        rm -rf "$tmp"; \
    fi; \
    chmod -R a+rx /ms-playwright; \
    npm install -g pyright typescript-language-server "typescript@5"; \
    npm cache clean --force; \
    command -v pyright-langserver; \
    command -v typescript-language-server; \
    apk del npm node-gyp || true; \
    rm -rf /usr/bin/npm /usr/bin/npx

# Install comprehensive Python packages with pinned versions for supply-chain
# safety. `requirements.txt` is the direct-deps list; `requirements.lock` is
# resolved for the Python 3.11 Linux sandbox runtime and includes transitives.
COPY --link deploy/requirements.txt deploy/requirements.lock /tmp/sandbox-requirements/
RUN --mount=type=cache,id=uv-sandbox,target=/root/.cache/uv \
    python -m pip install --break-system-packages --no-cache-dir uv==${UV_VERSION} && \
    UV_LINK_MODE=copy uv pip install --system --break-system-packages -r /tmp/sandbox-requirements/requirements.lock && \
    python -m pip uninstall -y uv

# Preserve the common `from unidecode import unidecode` sandbox import without
# shipping the GPL-licensed Unidecode package.
RUN python - <<'PY'
from pathlib import Path
import sysconfig

site = Path(sysconfig.get_paths()["purelib"])
pkg = site / "unidecode"
pkg.mkdir(exist_ok=True)
(pkg / "__init__.py").write_text(
    """from anyascii import anyascii

__version__ = '0.0-local-anyascii-compat'

def unidecode(value, errors='ignore', replace_str='?'):
    try:
        return anyascii(str(value))
    except Exception:
        if errors == 'strict':
            raise
        if errors == 'replace':
            return replace_str
        return ''

def unidecode_expect_ascii(value, errors='ignore', replace_str='?'):
    return unidecode(value, errors=errors, replace_str=replace_str)
""",
    encoding="utf-8",
)
PY

# Preserve legacy `import PyPDF2` sandbox scripts without shipping the vulnerable
# PyPDF2 package. The maintained pypdf package keeps the same top-level API.
RUN python - <<'PY'
from pathlib import Path
import sysconfig

site = Path(sysconfig.get_paths()["purelib"])
pkg = site / "PyPDF2"
pkg.mkdir(exist_ok=True)
(pkg / "__init__.py").write_text(
    """import importlib
import pkgutil
import sys

import pypdf as _pypdf
from pypdf import *

PdfReader = _pypdf.PdfReader
PdfWriter = _pypdf.PdfWriter
PdfFileReader = PdfReader
PdfFileWriter = PdfWriter

def _normalize_merger_kwargs(kwargs):
    if 'bookmark' in kwargs and 'outline_item' not in kwargs:
        kwargs['outline_item'] = kwargs.pop('bookmark')
    if 'import_bookmarks' in kwargs and 'import_outline' not in kwargs:
        kwargs['import_outline'] = kwargs.pop('import_bookmarks')
    return kwargs

class PdfMerger(PdfWriter):
    def merge(self, *args, **kwargs):
        return super().merge(*args, **_normalize_merger_kwargs(kwargs))

    def append(self, *args, **kwargs):
        return super().append(*args, **_normalize_merger_kwargs(kwargs))

PdfFileMerger = PdfMerger

__version__ = getattr(_pypdf, '__version__', '0.0-local-pypdf-compat')
__path__ = []
__all__ = list(getattr(_pypdf, '__all__', [])) + [
    'PdfFileReader',
    'PdfFileWriter',
    'PdfMerger',
    'PdfFileMerger',
]

for _module_info in pkgutil.iter_modules(_pypdf.__path__):
    _module_name = _module_info.name
    try:
        sys.modules[f'PyPDF2.{_module_name}'] = importlib.import_module(f'pypdf.{_module_name}')
    except Exception:
        pass
""",
    encoding="utf-8",
)
PY

# SECURITY: Remove pip/uv to prevent runtime package installation.
# Agents should use pre-installed packages only.
RUN rm -f /usr/bin/pip /usr/bin/pip3 /usr/bin/pip3.11 \
    && rm -f /usr/bin/uv /usr/bin/uvx /usr/local/bin/uv /usr/local/bin/uvx \
    && rm -rf /usr/lib/python3.11/ensurepip \
    && rm -rf /usr/lib/python3.11/site-packages/pip* \
    && rm -rf /usr/lib/python3.11/site-packages/uv* \
    && rm -rf /root/.cache/pip /root/.cache/uv

# Create non-root user for sandbox execution
RUN adduser -D -u 1001 -s /bin/bash sandbox

# Create directories (will be mounted as EmptyDir volumes)
RUN mkdir -p /workspace /mnt/skills /mnt/user-data/uploads /mnt/user-data/outputs \
    && chown -R sandbox:sandbox /workspace \
    && chmod 1777 /tmp /var/tmp

# SECURITY: Set minimal environment variables
# These are the ONLY env vars that should be set - no API keys, credentials, etc.
ENV PATH="/usr/local/bin:/usr/bin:/bin" \
    HOME="/workspace" \
    LANG="C.UTF-8" \
    LC_ALL="C.UTF-8" \
    NODE_PATH="/usr/local/lib/node_modules" \
    PLAYWRIGHT_BROWSERS_PATH="/ms-playwright" \
    PYTHONUNBUFFERED="1" \
    PYTHONDONTWRITEBYTECODE="1" \
    XDG_CONFIG_HOME="/tmp/.config" \
    XDG_CACHE_HOME="/tmp/.cache"

USER sandbox

WORKDIR /workspace

CMD ["tail", "-f", "/dev/null"]
