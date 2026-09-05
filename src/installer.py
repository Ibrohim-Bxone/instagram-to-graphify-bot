"""Turning an archived entry into an offer to install what it talks about.

Everything here is built around one constraint: the text this runs on came off
the internet. A reel's caption or an article's body can say anything, so no
command string is ever taken from that text — only a *name* is, validated
against a strict pattern, and then substituted into a fixed argv template that
this file owns. There is no shell: no `shell=True`, no pipes, no `curl | bash`,
and an installer that is not one of the four below simply has no code path.
"""

import logging
import re
import secrets
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import archive, config

log = logging.getLogger(__name__)

# A name is only ever interpolated into argv after matching one of these. They
# deliberately exclude every shell metacharacter, whitespace and leading dash,
# so a matched name cannot turn into a second argument or an option flag.
_GH_OWNER = r"[A-Za-z0-9][A-Za-z0-9._-]{0,38}"
_GH_REPO = r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}"
_SAFE_NPM = re.compile(r"^(?:@[a-z0-9][a-z0-9._-]{0,63}/)?[a-z0-9][a-z0-9._-]{0,63}$")
_SAFE_PIP = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SAFE_OLLAMA = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,63}(?::[A-Za-z0-9._-]{1,32})?$")

_GITHUB_RE = re.compile(rf"github\.com/({_GH_OWNER})/({_GH_REPO})", re.IGNORECASE)
_NPM_RE = re.compile(r"npm\s+(?:i|install|add)\s+(?:-g\s+|--global\s+)?([@A-Za-z0-9._/-]+)")
_PIP_RE = re.compile(r"pip3?\s+install\s+(?:-U\s+|--upgrade\s+)?([A-Za-z0-9._-]+)")
_OLLAMA_RE = re.compile(r"ollama\s+(?:run|pull)\s+([A-Za-z0-9._:/-]+)")

# Repos whose name says "these are Claude skills" get offered a second target.
_SKILLISH = re.compile(r"skill|claude|agent|prompt", re.IGNORECASE)


@dataclass
class Candidate:
    kind: str          # git | skill | npm | pip | ollama
    name: str          # validated, safe to interpolate
    argv: list         # the exact command that will run
    cwd: Path | None = None
    label: str = ""
    note: str = ""
    token: str = field(default_factory=lambda: secrets.token_urlsafe(6))

    @property
    def command(self) -> str:
        """Human-readable rendering. Display only — never executed as a string."""
        return " ".join(self.argv)


def _record_text(record: dict) -> str:
    """Every field a repo/package name might be hiding in."""
    parts = [record.get("caption", ""), record.get("onscreen", ""),
             record.get("transcript", "")]
    parts += record.get("links", [])
    for item in record.get("items", []):
        parts += [item.get("content", ""), item.get("name_en", "")]
    return "\n".join(p for p in parts if p)


def _git_candidates(text: str) -> list:
    out = []
    for owner, repo in _GITHUB_RE.findall(text):
        repo = repo.removesuffix(".git")
        if repo.lower() in {"blob", "tree", "releases", "issues", "pulls"}:
            continue
        full = f"{owner}/{repo}"
        url = f"https://github.com/{full}.git"
        out.append(Candidate(
            kind="git", name=full,
            argv=["git", "clone", "--depth", "1", url, repo],
            cwd=config.TOOLS_DIR,
            label=f"📦 git clone — {full}",
            note=f"{config.TOOLS_DIR / repo}",
        ))
        if _SKILLISH.search(full):
            out.append(Candidate(
                kind="skill", name=full,
                argv=["git", "clone", "--depth", "1", url, repo],
                cwd=config.SKILLS_DIR,
                label=f"🧩 Claude skill sifatida — {full}",
                note=f"{config.SKILLS_DIR / repo}",
            ))
    return out


def _simple_candidates(text: str) -> list:
    out = []
    specs = [
        ("npm", _NPM_RE, _SAFE_NPM, lambda n: ["npm", "install", "-g", n], "📥 npm -g"),
        ("pip", _PIP_RE, _SAFE_PIP, lambda n: ["pip", "install", n], "🐍 pip install"),
        ("ollama", _OLLAMA_RE, _SAFE_OLLAMA, lambda n: ["ollama", "pull", n], "🦙 ollama pull"),
    ]
    for kind, pattern, safe, build, prefix in specs:
        for name in pattern.findall(text):
            name = name.strip().rstrip(".,;)")
            if not safe.match(name):
                log.info("installer: rejected unsafe %s name %r", kind, name)
                continue
            out.append(Candidate(kind=kind, name=name, argv=build(name),
                                 label=f"{prefix} — {name}"))
    return out


def candidates_for(shortcode: str) -> list:
    """What this entry offers to install. Deduplicated, capped, never executed."""
    record = archive.read(archive.path_for(shortcode))
    if record is None:
        return []
    text = _record_text(record)

    seen, out = set(), []
    for cand in _git_candidates(text) + _simple_candidates(text):
        key = (cand.kind, cand.name.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(cand)
    return out[:8]


def preflight(cand: Candidate) -> str | None:
    """Reason this cannot run right now, or None if it can."""
    if shutil.which(cand.argv[0]) is None:
        return f"<code>{cand.argv[0]}</code> tizimda topilmadi (PATH da yo'q)."
    if cand.cwd is not None:
        target = cand.cwd / cand.argv[-1]
        if target.exists():
            return f"Allaqachon mavjud: <code>{target}</code>"
    return None


def run(cand: Candidate) -> tuple[bool, str]:
    """Execute the fixed argv. Returns (ok, combined output tail)."""
    cwd = cand.cwd
    if cwd is not None:
        cwd.mkdir(parents=True, exist_ok=True)
    log.info("installer: running %s (cwd=%s)", cand.argv, cwd)
    try:
        proc = subprocess.run(
            cand.argv, cwd=str(cwd) if cwd else None,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=config.INSTALL_TIMEOUT_SEC, shell=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"Vaqt tugadi ({config.INSTALL_TIMEOUT_SEC}s)."
    except Exception as e:  # missing binary, permission, ...
        return False, str(e)

    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode == 0, output[-1500:] or "(chiqish bo'sh)"
