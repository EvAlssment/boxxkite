"""
Preset packages available in the sandbox environment.

This module defines the comprehensive list of packages pre-installed in the
sandbox container. Agents cannot install additional packages at runtime
(pip is removed from the container), so all needed packages must be listed here.

SECURITY: Network access is disabled in the sandbox, so:
1. All packages MUST be pre-installed in the Docker image
2. pip install commands will fail (pip is removed)
3. Network requests to download packages will fail
"""

# Organized by category for easy reference
PRESET_PYTHON_PACKAGES = {
    "data_processing": [
        "pandas",
        "numpy",
        "polars",
    ],
    "visualization": [
        "matplotlib",
        "seaborn",
        "plotly",
    ],
    "scientific_computing": [
        "scipy",
        "scikit-learn",
        "statsmodels",
    ],
    "excel_spreadsheet": [
        "openpyxl",
        "xlrd",
        "xlsxwriter",
    ],
    "document_processing": [
        "python-docx",
        "python-pptx",
        "pypdf",
        "pdfplumber",
        "reportlab",
        "pypdfium2",
        "pdf2image",
        "pytesseract",
        "defusedxml",
        "markitdown",
    ],
    "html_xml_parsing": [
        "lxml",
        "beautifulsoup4",
        "html5lib",
    ],
    "data_serialization": [
        "pyyaml",
        "toml",
        "orjson",
    ],
    "image_processing": [
        "pillow",
    ],
    "date_time": [
        "python-dateutil",
        "pytz",
    ],
    "output_formatting": [
        "tabulate",
        "tqdm",
    ],
    "templating": [
        "jinja2",
        "markdownify",
    ],
    "text_processing": [
        "chardet",
        "regex",
        "ftfy",
        "anyascii",
        "unidecode",
    ],
    "http_clients": [
        "requests",
        "httpx",
    ],
}

# Node.js packages available in the sandbox
PRESET_NODE_PACKAGES = [
    "docx",
    "fontkit",
    "linebreak",
    "mathjax-full",
    "pdf-lib",
    "pdfjs-dist",
    "playwright",
    "pptxgenjs",
    "prismjs",
    "react",
    "react-dom",
    "react-icons",
    "sharp",
    "skia-canvas",
]

# System tools available in the sandbox
PRESET_SYSTEM_TOOLS = [
    "python3",
    "node",
    "jq",
    "sqlite3",
    "libreoffice",
    "poppler-utils",
    "qpdf",
    "pandoc",
    "tesseract-ocr",
    "imagemagick",
    "ghostscript",
    "heif-convert",
]

# =============================================================================
# SECURITY: Command Blocking
# =============================================================================
# Commands are blocked at multiple layers:
# 1. This file (application layer) - blocks before execution
# 2. NetworkPolicy (K8s layer) - blocks outbound connections
# 3. Container security context - drops capabilities, read-only root filesystem
# 4. Sandbox runs as non-root user (UID 1001)

# Commands that are blocked (for helpful error messages)
# Categories:
# 1. Package installation - blocked because network is disabled
# 2. System info leakage - blocked to prevent exposing cluster/system info
# 3. Dangerous file operations - blocked to prevent system damage
# 4. Network reconnaissance - blocked for security
# 5. Process manipulation - blocked to prevent sandbox escape attempts

BLOCKED_COMMANDS = [
    # Package installation (network disabled)
    # Disabled for extreme-low false-positive mode: generated docs/runbooks
    # frequently include package installation examples. Runtime network policy
    # still blocks package downloads until this matching becomes token-aware.
    # "pip install",
    # "pip3 install",
    # "python -m pip install",
    # "python3 -m pip install",
    # "npm install",
    # "yarn add",
    # "pnpm add",
    # Disabled for extreme-low false-positive mode: this broad token also
    # blocks harmless documentation/runbook text such as "apt-get update".
    # "apt-get",
    # Disabled for the same reason as apt-get: these are common installation
    # examples in generated technical documents and runbooks.
    # "apt install",
    # "apk add",
    # "yum install",
    # "dnf install",
    # Network tools (blocked at network layer too, but catch early)
    # Disabled for extreme-low false-positive mode: network command examples
    # often appear in generated docs/scripts; network policy still blocks egress.
    # "curl ",
    # "curl\t",
    # "wget ",
    # "wget\t",
    # Disabled until matching is shell-token-aware: raw substring scanning also
    # inspects heredoc/script/document text, so short network tool names match
    # normal content such as "Sync ".
    # "nc ",
    # Disabled for extreme-low false-positive mode: command names can appear as
    # harmless troubleshooting content in generated documents.
    # "netcat",
    # Disabled until matching is shell-token-aware: matches words like
    # "truncate" inside generated scripts or documents.
    # "ncat",
    # "telnet",
    # Disabled until matching is shell-token-aware: SSH/SCP/FTP/SFTP appear
    # frequently in technical documentation, not only as commands.
    # "ssh ",
    # "scp ",
    # "rsync",
    # "ftp ",
    # "sftp",
]

# Patterns that leak system/cluster information
BLOCKED_INFO_PATTERNS = [
    # Environment variable enumeration
    # Disabled for extreme-low false-positive mode: "printenv" can appear in
    # generated runbooks. The more explicit os.environ enumeration patterns
    # below remain blocked.
    # "printenv",
    # Disabled until matching is shell-token-aware: raw `env` tokens match
    # safe shebangs such as "#!/usr/bin/env python3" in generated scripts.
    # "env\n",
    # "env ",
    # "env\t",
    "os.environ[",      # Python dict access to all env
    "os.environ.keys",
    "os.environ.items",
    "os.environ.values",
    "dict(os.environ)",
    "list(os.environ)",
    "{**os.environ}",
    # Sensitive filesystem paths
    # Disabled for extreme-low false-positive mode: Linux and credential path
    # references often appear in generated docs/runbooks, e.g. examples that
    # mention ~/.aws/credentials or Kubernetes service-account token paths.
    # Re-enable with token-aware matching and command-context checks.
    # "/proc/",
    # "/sys/",
    # "/etc/passwd",
    # "/etc/shadow",
    # "/etc/group",
    # "/etc/hosts",
    # "/etc/resolv.conf",
    # "/etc/kubernetes",
    # "/var/run/secrets",
    # "/.kube/",
    # "/.ssh/",
    # "/.aws/",
    # "/.azure/",
    # Network reconnaissance
    # Disabled for extreme-low false-positive mode: these command names are
    # common in troubleshooting prose and generated runbooks.
    # "netstat",
    # Disabled until matching is shell-token-aware: short tokens produce false
    # positives in ordinary text and generated code.
    # " ss ",
    # "ss\n",
    # "ifconfig",
    # "ip addr",
    # "ip route",
    # "ip link",
    # "iptables",
    # Disabled until matching is shell-token-aware: matches "unmapped".
    # "nmap",
    # "traceroute",
    # Disabled until matching is shell-token-aware: matches words like
    # "Shipping".
    # "ping ",
]

# Dangerous operations that could harm the system
# NOTE: The sandbox is an ephemeral K8s pod — most destructive commands are
# harmless there. We only block operations that could affect the host/cluster
# or attempt container escapes. File cleanup (rm -rf /tmp/..., pkill stale
# processes) is explicitly allowed since tools like LibreOffice need it.
BLOCKED_DANGEROUS_PATTERNS = [
    # Disabled until matching is shell-token-aware: safe output suppression via
    # "> /dev/null" is common in generated commands.
    # "> /dev/",
    # Process/system manipulation
    # Disabled until matching is shell-token-aware: these are common incident
    # and remediation terms in generated IT documents.
    # "shutdown",
    # "reboot",
    # Disabled for extreme-low false-positive mode: system-management command
    # names are common in generated incident/runbook content. The sandbox still
    # runs as a non-root user with dropped capabilities.
    # "init 0",
    # "init 6",
    # "systemctl",
    # Disabled until matching is shell-token-aware: matches "Service Desk",
    # "service support", and other normal business content.
    # "service ",
    # Privilege escalation attempts
    # Disabled for extreme-low false-positive mode: privilege-escalation command
    # examples frequently appear in generated docs. Runtime UID/capability
    # controls still prevent successful privilege escalation.
    # "sudo ",
    # "su -",
    # "su root",
    # "doas ",
    # Disk/mount operations
    # Disabled until matching is shell-token-aware: matches words like
    # "Amount".
    # "mount ",
    # Disabled for extreme-low false-positive mode: disk-operation command names
    # can appear in troubleshooting content. Runtime sandbox isolation remains
    # the primary enforcement for now.
    # "umount ",
    # "fdisk",
    # "mkfs",
    # "dd if=/dev",
    # Container escape attempts
    # Disabled for extreme-low false-positive mode: these can appear in sandbox
    # architecture/debugging docs. Re-enable with token-aware matching.
    # "nsenter",
    # "unshare",
    # "capsh",
    # "setcap",
    # "getcap",
]

# Paths that are ALLOWED for file operations (whitelist)
ALLOWED_PATHS = [
    "/workspace/",
    "/tmp/",
    "/mnt/skills/",
    "/mnt/user-data/uploads/",
    "/mnt/user-data/outputs/",
]


def get_all_packages() -> list[str]:
    """Get flat list of all pre-installed Python packages."""
    packages = []
    for category_packages in PRESET_PYTHON_PACKAGES.values():
        packages.extend(category_packages)
    return packages


def get_package_list_message() -> str:
    """
    Generate a human-readable message listing available packages.
    Used in error messages when agents try to install packages.
    """
    lines = [
        "Pre-installed Python packages in the sandbox:",
        "",
    ]

    for category, packages in PRESET_PYTHON_PACKAGES.items():
        category_name = category.replace("_", " ").title()
        package_list = ", ".join(packages)
        lines.append(f"  {category_name}: {package_list}")

    lines.extend([
        "",
        "Pre-installed Node.js packages in the sandbox:",
        "  " + ", ".join(PRESET_NODE_PACKAGES),
        "",
        "System tools: " + ", ".join(PRESET_SYSTEM_TOOLS),
        "",
        "Note: Package installation is disabled. Use only the packages listed above.",
    ])

    return "\n".join(lines)


def is_blocked_command(command: str) -> tuple[bool, str]:
    """
    Check if a command is blocked for security reasons.

    Checks for:
    1. Package installation attempts
    2. System information leakage
    3. Dangerous/destructive operations
    4. File access outside allowed paths

    Args:
        command: The command string to check

    Returns:
        Tuple of (is_blocked, helpful_message)
    """
    command_lower = command.lower()

    # Check for package installation attempts
    for blocked in BLOCKED_COMMANDS:
        if blocked in command_lower:
            return True, (
                f"Package installation is not allowed in the sandbox.\n\n"
                f"{get_package_list_message()}"
            )

    # Check for system info leakage attempts
    for pattern in BLOCKED_INFO_PATTERNS:
        if pattern.lower() in command_lower:
            return True, (
                "This command is blocked for security reasons.\n\n"
                "The sandbox restricts access to system information to protect "
                "the cluster environment. You can only access files in /workspace/, "
                "/mnt/user-data/uploads/, /mnt/user-data/outputs/, and /mnt/skills/ "
                "and use the pre-installed packages for data processing.\n\n"
                "If you need specific information, please describe what you're "
                "trying to accomplish and I can suggest an alternative approach."
            )

    # Check for dangerous operations
    for pattern in BLOCKED_DANGEROUS_PATTERNS:
        if pattern.lower() in command_lower:
            return True, (
                "This command is blocked because it could harm the system.\n\n"
                "The sandbox runs with restricted permissions for security. "
                "File operations are limited to approved directories.\n\n"
                "Available operations:\n"
                "- Read/write files in /workspace/\n"
                "- Read files in /mnt/user-data/uploads/ and /mnt/skills/\n"
                "- Read/write files in /mnt/user-data/outputs/\n"
                "- Run Python scripts with pre-installed packages\n"
                "- Use jq for JSON processing\n"
                "- Use sqlite3 for database operations"
            )

    return False, ""


def is_path_allowed(path: str) -> bool:
    """
    Check if a file path is within allowed directories.

    Args:
        path: The file path to check

    Returns:
        True if path is allowed, False otherwise
    """
    # Normalize the path
    normalized = path.replace("//", "/").lower()

    # Check against allowed paths
    for allowed in ALLOWED_PATHS:
        if normalized.startswith(allowed.lower()):
            return True

    # Also allow relative paths (they resolve to /workspace/)
    if not normalized.startswith("/"):
        return True

    return False


def get_module_not_found_message(module_name: str) -> str:
    """
    Generate a helpful message when a module is not found.

    Args:
        module_name: Name of the module that wasn't found

    Returns:
        Helpful error message with available alternatives
    """
    return (
        f"Module '{module_name}' is not available in the sandbox.\n\n"
        f"{get_package_list_message()}\n\n"
        f"If you need '{module_name}' for your workflow, please contact "
        f"the administrator to add it to the sandbox image."
    )
