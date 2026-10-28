"""Tier-3 Account Probes for vendor watch.

Detects what is actually available or changed on the user's own accounts and tools:
1. agy_models: Available models list in Antigravity (agy).
2. agy_changelog: Version entries and release notes in agy changelog.
3. cli_versions: Installed versions of codex, claude, agy vs latest GitHub releases.
4. vertex_models: Candidate model availability check on Vertex AI via count_tokens (200=available, 404=unavailable).
"""

from datetime import datetime
import json
import logging
from pathlib import Path
import re
import shutil
import subprocess
import urllib.error
import urllib.request

log = logging.getLogger("vendor_watch.probes")

_SEMVER_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")

CANNED_PROBE_OUTPUTS = {
    "agy_models": """Fetching available models...
gemini-3.8-flash-high\tGemini 3.8 Flash (High)
gemini-3.8-flash-medium\tGemini 3.8 Flash (Medium)
gemini-3.8-flash-low\tGemini 3.8 Flash (Low)
claude-sonnet-4-6\tClaude Sonnet 4.6
claude-opus-4-6-thinking\tClaude Opus 4.6 Thinking
gpt-oss-120b-medium\tGPT OSS 120B Medium
""",
    "agy_changelog": """1.4.8:
· Fix terminal resize bug
· Update model fallback order
1.4.7:
· Add gpt-oss-120b-medium
""",
    "cli_versions": {
        "codex": "codex-cli 0.156.1",
        "claude": "2.1.310 (Claude Code)",
        "agy": "1.4.8",
    },
    "vertex_models": {
        "gemini-2.5-flash": 200,
        "gemini-2.5-pro": 200,
        "gemini-3.7-flash": 200,
        "gemini-4-argon": 200,
        "gemini-3.1-pro-high": 404,
    },
}


def load_probes_config() -> dict:
    config_path = Path(__file__).parent / "probes.json"
    if not config_path.is_file():
        return {}
    return json.loads(config_path.read_text(encoding="utf-8"))


def extract_semver(text: str) -> tuple[int, int, int] | None:
    m = _SEMVER_RE.search(text)
    if m:
        try:
            return int(m.group(1)), int(m.group(2)), int(m.group(3))
        except ValueError:
            return None
    return None


def compare_semver(v1_str: str, v2_str: str) -> int:
    """Compare two semver strings: -1 if v1 < v2, 0 if v1 == v2, 1 if v1 > v2."""
    sv1 = extract_semver(v1_str)
    sv2 = extract_semver(v2_str)
    if not sv1 or not sv2:
        return 0
    if sv1 < sv2:
        return -1
    if sv1 > sv2:
        return 1
    return 0


def run_command(
    cmd: list[str],
    fallback_paths: list[str] | None = None,
    timeout: int = 15,
) -> tuple[int, str, str]:
    """Execute command with timeout and fallback path resolution."""
    actual_cmd = list(cmd)
    binary = actual_cmd[0]
    resolved = shutil.which(binary)

    if not resolved and fallback_paths:
        for fb in fallback_paths:
            if Path(fb).is_file():
                actual_cmd[0] = fb
                resolved = fb
                break

    try:
        res = subprocess.run(
            actual_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return res.returncode, res.stdout, res.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"Command timed out after {timeout}s: {' '.join(actual_cmd)}"
    except FileNotFoundError:
        return -1, "", f"Command not found: {binary}"
    except Exception as e:
        return -1, "", str(e)


def check_vertex_model_count_tokens(
    model_id: str,
    project: str = "project-92b77c1d-c511-47ce-965",
    location: str = "global",
    timeout: int = 15,
) -> tuple[int | None, str | None]:
    """Check model availability via count_tokens on Vertex AI.

    Returns: (status_code, error_message).
    200 -> available to this account
    404 -> not available
    other (None, 401, 403, 429, timeout, network) -> probe failure (never treated as removed).
    """
    try:
        from google import genai
        client = genai.Client(vertexai=True, project=project, location=location)
        client.models.count_tokens(model=model_id, contents="ping")
        return 200, None
    except ImportError:
        pass
    except Exception as e:
        status = getattr(e, "code", getattr(e, "status_code", None))
        err_msg = str(e)
        if status == 404 or "404" in err_msg or "NOT_FOUND" in err_msg:
            return 404, err_msg
        return (status if isinstance(status, int) else 500), err_msg

    # Fallback to existing src.vertex token helper + urllib
    try:
        from .. import vertex
        token = vertex._get_token()
        proj = project or vertex._get_project()
        loc = location or vertex._get_location()
        url = (
            f"https://aiplatform.googleapis.com/v1beta1/projects/{proj}"
            f"/locations/{loc}/publishers/google/models/{model_id}:countTokens"
        )
        payload = json.dumps({"contents": [{"role": "user", "parts": [{"text": "ping"}]}]}).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
            "x-goog-user-project": proj,
        }
        req = urllib.request.Request(url, data=payload, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, None
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return 404, str(e)
        return e.code, str(e)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return None, str(e)
    except Exception as e:
        return None, str(e)


def get_model_profile_impact(model_id: str, action: str = "added") -> str:
    """Generate Uzbek profile stack impact analysis based on profile.md."""
    m = model_id.lower()
    if action == "added":
        if "argon" in m:
            return (
                "Profilingizga ko'ra, Gemini 4 Argon interaktiv multimodal fikrlash hamda "
                "argon-ish.py klipboard ko'prigi uchun asosiy model hisoblanadi. Endi uni "
                "Vertex API orqali to'g'ridan-to'g'ri skriptlarga ulash va ishlatish mumkin."
            )
        if "gemini-3.8" in m:
            return (
                "Antigravity yoki Vertex AI ekotizimidagi ushbu model sizning asosiy dasturlash "
                "drayveringiz (agy-run.py konveyeri va Antigravity Switcher) uchun tezroq kod yozish "
                "va kvotalarni taqsimlash imkonini beradi."
            )
        if "opus" in m or "claude" in m:
            return (
                "Anthropic modeli sizning tizim arxitektoringiz va spetsifikatsiyalarni tekshirish "
                "vazifalarida aniqlik hamda ziddiyatlarni hal qilish samaradorligini oshiradi."
            )
        if "astra" in m or "gpt" in m:
            return (
                "OpenAI modeli mustaqil QA, xavfsizlik auditlari va agy-run tekshiruv bosqichlarida "
                "adversarial sinovlarni kuchaytirish imkonini beradi."
            )
        return (
            f"Ushbu model ({model_id}) profilingizdagi dasturlash va avtomatlashtirish "
            "vazifalari uchun akkauntingizda muvaffaqiyatli tekshirildi va faollashdi."
        )
    else:  # removed
        if "gemini-3.1" in m or "gemini" in m:
            return (
                f"Hisobingizdan {model_id} olib tashlandi. Antigravity Switcher va agy-run.py "
                "konveyeri konfiguratsiyasidan ushbu model nomini chiqarib tashlash va faol modellarga "
                "(masalan, gemini-3.8-flash-high) o'tish zarur."
            )
        return (
            f"Hisobingizdan {model_id} olib tashlandi. Tegishli skriptlar va konveyer parametrlarini "
            "tekshirib, muqobil modellarga yo'naltirish tavsiya etiladi."
        )


def get_model_command(
    model_id: str,
    source: str = "vertex",
    project: str = "project-92b77c1d-c511-47ce-965",
    location: str = "global",
) -> str:
    """Generate exact command to test or use model."""
    if source == "vertex":
        return f"gcloud ai models describe {model_id} --project={project} --region={location}"
    return f"agy --model {model_id}"


# ---------------------------------------------------------------------------
# Individual Probes
# ---------------------------------------------------------------------------


def probe_agy_models(config: dict | None = None, fixtures: bool = False) -> dict:
    """Probe 1: Retrieve available model IDs from agy models."""
    cfg = (config or {}).get("agy_models", {})
    now_iso = datetime.now().isoformat()

    if fixtures:
        stdout = CANNED_PROBE_OUTPUTS["agy_models"]
        retcode = 0
        stderr = ""
    else:
        cmd = cfg.get("command", ["agy", "models"])
        fb = cfg.get("fallback_paths", [])
        timeout = cfg.get("timeout", 15)
        retcode, stdout, stderr = run_command(cmd, fallback_paths=fb, timeout=timeout)

    if retcode != 0:
        return {
            "probe": "agy_models",
            "timestamp": now_iso,
            "status": "error",
            "data": {"models": {}, "model_ids": []},
            "error": stderr or f"Exit code {retcode}",
        }

    models: dict[str, str] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line or "fetching" in line.lower() or line.startswith("---"):
            continue
        parts = line.split("\t", 1)
        if len(parts) == 2:
            mid, mname = parts[0].strip(), parts[1].strip()
        else:
            sub = line.split(None, 1)
            mid = sub[0].strip()
            mname = sub[1].strip() if len(sub) > 1 else mid
        if mid:
            models[mid] = mname

    return {
        "probe": "agy_models",
        "timestamp": now_iso,
        "status": "success",
        "data": {
            "models": models,
            "model_ids": sorted(list(models.keys())),
        },
        "error": None,
    }


def probe_agy_changelog(config: dict | None = None, fixtures: bool = False) -> dict:
    """Probe 2: Retrieve recent changelog entries from agy changelog."""
    cfg = (config or {}).get("agy_changelog", {})
    now_iso = datetime.now().isoformat()

    if fixtures:
        stdout = CANNED_PROBE_OUTPUTS["agy_changelog"]
        retcode = 0
        stderr = ""
    else:
        cmd = cfg.get("command", ["agy", "changelog"])
        fb = cfg.get("fallback_paths", [])
        timeout = cfg.get("timeout", 15)
        retcode, stdout, stderr = run_command(cmd, fallback_paths=fb, timeout=timeout)

    if retcode != 0:
        return {
            "probe": "agy_changelog",
            "timestamp": now_iso,
            "status": "error",
            "data": {"versions": [], "latest_version": "", "notes": {}},
            "error": stderr or f"Exit code {retcode}",
        }

    versions: list[str] = []
    notes: dict[str, str] = {}
    current_ver = ""
    current_lines: list[str] = []

    for line in stdout.splitlines():
        line_s = line.strip()
        if not line_s:
            continue
        v_match = re.match(r"^(\d+\.\d+(?:\.\d+)?):", line_s)
        if v_match:
            if current_ver:
                notes[current_ver] = "\n".join(current_lines).strip()
            current_ver = v_match.group(1)
            versions.append(current_ver)
            current_lines = []
        elif current_ver:
            current_lines.append(line_s)

    if current_ver:
        notes[current_ver] = "\n".join(current_lines).strip()

    latest = versions[0] if versions else ""
    return {
        "probe": "agy_changelog",
        "timestamp": now_iso,
        "status": "success",
        "data": {
            "versions": versions,
            "latest_version": latest,
            "notes": notes,
        },
        "error": None,
    }


def probe_cli_versions(
    config: dict | None = None,
    fixtures: bool = False,
    latest_releases: dict[str, str] | None = None,
) -> dict:
    """Probe 3: Detect installed versions of codex, claude, agy vs latest GitHub releases."""
    cfg = (config or {}).get("cli_versions", {})
    tools_cfg = cfg.get("tools", {})
    now_iso = datetime.now().isoformat()
    latest_releases = latest_releases or {}

    default_tools = {
        "codex": {
            "command": ["codex", "--version"],
            "source_id": "openai-codex",
            "product": "Codex CLI",
            "vendor": "OpenAI",
        },
        "claude": {
            "command": ["claude", "--version"],
            "source_id": "anthropic-claude-code",
            "product": "Claude Code",
            "vendor": "Anthropic",
        },
        "agy": {
            "command": ["agy", "--version"],
            "fallback_paths": [
                "C:/Users/user/AppData/Local/agy/bin/agy.exe",
                "/usr/local/bin/agy",
            ],
            "source_id": "google-gemini-cli",
            "product": "Antigravity (agy)",
            "vendor": "Google",
        },
    }
    tools = dict(default_tools)
    tools.update(tools_cfg)

    tool_results: dict[str, dict] = {}
    for tool_name, tcfg in tools.items():
        if fixtures:
            stdout = CANNED_PROBE_OUTPUTS["cli_versions"].get(tool_name, "")
            retcode = 0
            stderr = ""
        else:
            cmd = tcfg.get("command", [tool_name, "--version"])
            fb = tcfg.get("fallback_paths", [])
            timeout = cfg.get("timeout", 10)
            retcode, stdout, stderr = run_command(cmd, fallback_paths=fb, timeout=timeout)

        installed_ver = ""
        if retcode == 0 and stdout:
            sv = extract_semver(stdout)
            if sv:
                installed_ver = f"{sv[0]}.{sv[1]}.{sv[2]}"

        src_id = tcfg.get("source_id", "")
        latest_ver = latest_releases.get(src_id, "")

        is_outdated = False
        if installed_ver and latest_ver:
            if compare_semver(installed_ver, latest_ver) < 0:
                is_outdated = True

        tool_results[tool_name] = {
            "installed": installed_ver,
            "latest": latest_ver,
            "outdated": is_outdated,
            "vendor": tcfg.get("vendor", ""),
            "product": tcfg.get("product", tool_name),
            "status": "ok" if installed_ver else "not_installed",
            "error": stderr if retcode != 0 else None,
        }

    return {
        "probe": "cli_versions",
        "timestamp": now_iso,
        "status": "success",
        "data": {"tools": tool_results},
        "error": None,
    }


def probe_vertex_models(
    config: dict | None = None,
    fixtures: bool = False,
    extra_candidates: set[str] | None = None,
) -> dict:
    """Probe 4: Check availability of candidate Vertex AI models via count_tokens."""
    cfg = (config or {}).get("vertex_models", {})
    now_iso = datetime.now().isoformat()
    candidates = list(cfg.get("candidate_models", [
        "gemini-2.5-flash",
        "gemini-2.5-pro",
        "gemini-3.7-flash",
        "gemini-4-argon",
        "gemini-3.1-pro-high",
    ]))
    if extra_candidates:
        for c in extra_candidates:
            if c not in candidates:
                candidates.append(c)

    project = cfg.get("project", "project-92b77c1d-c511-47ce-965")
    location = cfg.get("location", "global")
    timeout = cfg.get("timeout", 15)

    available: list[str] = []
    unavailable: list[str] = []
    errors: dict[str, str] = {}

    for mid in candidates:
        if fixtures:
            code = CANNED_PROBE_OUTPUTS["vertex_models"].get(mid, 200)
            err = None if code == 200 else f"HTTP {code}"
        else:
            code, err = check_vertex_model_count_tokens(
                mid,
                project=project,
                location=location,
                timeout=timeout,
            )

        if code == 200:
            available.append(mid)
        elif code == 404:
            unavailable.append(mid)
        else:
            # Probe failure (timeout, network, auth 401/403, 500)
            errors[mid] = err or f"Probe failed with status {code}"

    status = "error" if len(errors) == len(candidates) and candidates else "success"
    return {
        "probe": "vertex_models",
        "timestamp": now_iso,
        "status": status,
        "data": {
            "available": sorted(available),
            "unavailable": sorted(unavailable),
            "errors": errors,
            "project": project,
            "location": location,
        },
        "error": None if status == "success" else f"{len(errors)} models failed availability probe",
    }


# ---------------------------------------------------------------------------
# Diffing & Event Construction
# ---------------------------------------------------------------------------


def _extract_data(obj: dict | None) -> dict:
    if not obj or not isinstance(obj, dict):
        return {}
    if "data" in obj and isinstance(obj["data"], dict):
        return obj["data"]
    return obj


def diff_agy_models(current: dict, previous: dict | None) -> list[dict]:
    """Diff agy_models snapshot against last good snapshot."""
    if isinstance(current, dict) and current.get("status") == "error":
        return []
    curr_data = _extract_data(current)
    prev_data = _extract_data(previous) if previous else None
    if prev_data is None:
        return []  # Baseline establishes initial state

    prev_ids = set(prev_data.get("model_ids") or prev_data.get("models", {}).keys())
    curr_ids = set(curr_data.get("model_ids") or curr_data.get("models", {}).keys())
    curr_models = curr_data.get("models", {})
    now_iso = datetime.now().isoformat()

    added = sorted(list(curr_ids - prev_ids))
    removed = sorted(list(prev_ids - curr_ids))
    events: list[dict] = []

    for mid in added:
        mname = curr_models.get(mid, mid)
        events.append({
            "id": f"probe:agy_models:added:{mid}",
            "source_id": "probe:agy_models",
            "entry_id": f"added:{mid}",
            "vendor": "Google" if "gemini" in mid else ("Anthropic" if "claude" in mid else "OpenAI"),
            "product": "Antigravity (agy)",
            "title": f"Sizning akkauntingizda: {mid} paydo bo'ldi",
            "link": "",
            "published_at": now_iso,
            "raw_content": (
                f"Antigravity (agy) tizimida yangi model paydo bo'ldi: {mname} ({mid}).\n"
                f"{get_model_profile_impact(mid, 'added')}"
            ),
            "triage_type": "account_change",
            "priority": "high",
            "status": "unseen",
            "command": get_model_command(mid, source="agy"),
            "profile_impact": get_model_profile_impact(mid, "added"),
        })

    for mid in removed:
        events.append({
            "id": f"probe:agy_models:removed:{mid}",
            "source_id": "probe:agy_models",
            "entry_id": f"removed:{mid}",
            "vendor": "Google" if "gemini" in mid else ("Anthropic" if "claude" in mid else "OpenAI"),
            "product": "Antigravity (agy)",
            "title": f"Sizning akkauntingizda: {mid} yo'qoldi",
            "link": "",
            "published_at": now_iso,
            "raw_content": (
                f"Antigravity (agy) tizimidan model olib tashlandi: {mid}.\n"
                f"{get_model_profile_impact(mid, 'removed')}"
            ),
            "triage_type": "account_change",
            "priority": "high",
            "status": "unseen",
            "command": "agy models",
            "profile_impact": get_model_profile_impact(mid, "removed"),
        })

    return events


def diff_vertex_models(current: dict, previous: dict | None) -> list[dict]:
    """Diff vertex_models snapshot against last good snapshot.

    Rules:
    - 404 -> 200 transition = model newly available (high priority)
    - 200 -> 404 transition = model newly unavailable (high priority)
    - Probe failure (timeout, network, auth) is recorded in errors and NEVER generates removal.
    """
    if isinstance(current, dict) and current.get("status") == "error":
        return []
    curr_data = _extract_data(current)
    prev_data = _extract_data(previous) if previous else None
    if prev_data is None:
        return []  # Baseline establishes initial state

    prev_avail = set(prev_data.get("available", []))
    curr_avail = set(curr_data.get("available", []))
    curr_unavail = set(curr_data.get("unavailable", []))
    proj = curr_data.get("project") or (prev_data.get("project") if prev_data else "") or "project-92b77c1d-c511-47ce-965"
    loc = curr_data.get("location") or (prev_data.get("location") if prev_data else "") or "global"
    now_iso = datetime.now().isoformat()

    events: list[dict] = []

    # 404 -> 200: Newly available
    newly_available = sorted(list(curr_avail - prev_avail))
    for mid in newly_available:
        events.append({
            "id": f"probe:vertex_models:available:{mid}",
            "source_id": "probe:vertex_models",
            "entry_id": f"available:{mid}",
            "vendor": "Google",
            "product": "Vertex AI",
            "title": f"Sizning akkauntingizda: {mid} paydo bo'ldi",
            "link": "https://cloud.google.com/vertex-ai/docs/generative-ai/model-reference/overview",
            "published_at": now_iso,
            "raw_content": (
                f"Google Cloud Vertex AI hisobingizda ({proj}, {loc}) {mid} modeli "
                f"muvaffaqiyatli tekshirildi (count_tokens 200 OK).\n"
                f"{get_model_profile_impact(mid, 'added')}"
            ),
            "triage_type": "account_change",
            "priority": "high",
            "status": "unseen",
            "command": get_model_command(mid, source="vertex", project=proj, location=loc),
            "profile_impact": get_model_profile_impact(mid, "added"),
        })

    # 200 -> 404: Newly unavailable (confirmed only by clean 404, never probe error)
    newly_unavailable = sorted(list(prev_avail & curr_unavail))
    for mid in newly_unavailable:
        events.append({
            "id": f"probe:vertex_models:unavailable:{mid}",
            "source_id": "probe:vertex_models",
            "entry_id": f"unavailable:{mid}",
            "vendor": "Google",
            "product": "Vertex AI",
            "title": f"Sizning akkauntingizda: {mid} yo'qoldi",
            "link": "https://cloud.google.com/vertex-ai/docs/generative-ai/model-reference/overview",
            "published_at": now_iso,
            "raw_content": (
                f"Google Cloud Vertex AI hisobingizda ({proj}, {loc}) {mid} modeli "
                f"endi mavjud emas (count_tokens 404 Not Found).\n"
                f"{get_model_profile_impact(mid, 'removed')}"
            ),
            "triage_type": "account_change",
            "priority": "high",
            "status": "unseen",
            "command": f"gcloud ai models list --project={proj} --region={loc}",
            "profile_impact": get_model_profile_impact(mid, "removed"),
        })

    return events


def diff_agy_changelog(current: dict, previous: dict | None) -> list[dict]:
    """Diff agy_changelog snapshot for new version entries."""
    if isinstance(current, dict) and current.get("status") == "error":
        return []
    curr_data = _extract_data(current)
    prev_data = _extract_data(previous) if previous else None
    if prev_data is None:
        return []

    prev_vers = set(prev_data.get("versions", []))
    curr_vers = curr_data.get("versions", [])
    notes = curr_data.get("notes", {})
    now_iso = datetime.now().isoformat()
    events: list[dict] = []

    for v in curr_vers:
        if v not in prev_vers:
            v_notes = notes.get(v, "")
            events.append({
                "id": f"probe:agy_changelog:{v}",
                "source_id": "probe:agy_changelog",
                "entry_id": f"version:{v}",
                "vendor": "Google",
                "product": "Antigravity (agy)",
                "title": f"Antigravity (agy) yangi versiyasi: {v}",
                "link": "",
                "published_at": now_iso,
                "raw_content": f"Antigravity changelog yangi versiyani e'lon qildi: {v}.\n{v_notes}",
                "triage_type": "account_change",
                "priority": "digest",
                "status": "unseen",
                "command": "agy changelog",
                "profile_impact": "Antigravity IDE va agy CLI muhitida yangilanish mavjud.",
            })

    return events


def diff_cli_versions(current: dict, previous: dict | None) -> list[dict]:
    """Diff cli_versions snapshot for newly outdated tools or installed version bumps."""
    if isinstance(current, dict) and current.get("status") == "error":
        return []
    curr_data = _extract_data(current)
    prev_data = _extract_data(previous) if previous else None
    if prev_data is None:
        return []

    curr_tools = curr_data.get("tools", {})
    prev_tools = prev_data.get("tools", {})
    now_iso = datetime.now().isoformat()
    events: list[dict] = []

    for tname, tinfo in curr_tools.items():
        installed = tinfo.get("installed", "")
        latest = tinfo.get("latest", "")
        outdated = tinfo.get("outdated", False)
        vendor = tinfo.get("vendor", "")
        product = tinfo.get("product", tname)

        prev_tinfo = prev_tools.get(tname, {})
        prev_installed = prev_tinfo.get("installed", "")
        prev_latest = prev_tinfo.get("latest", "")
        prev_outdated = prev_tinfo.get("outdated", False)

        # 1. Installed version bumped
        if installed and prev_installed and compare_semver(prev_installed, installed) < 0:
            events.append({
                "id": f"probe:cli_versions:bump:{tname}:{installed}",
                "source_id": "probe:cli_versions",
                "entry_id": f"bump:{tname}:{installed}",
                "vendor": vendor,
                "product": product,
                "title": f"{product} yangilandi: {installed}",
                "link": "",
                "published_at": now_iso,
                "raw_content": f"{product} versiyasi {prev_installed} dan {installed} ga muvaffaqiyatli yangilandi.",
                "triage_type": "account_change",
                "priority": "digest",
                "status": "unseen",
                "command": f"{tname} --version",
                "profile_impact": f"{product} vositasi muvaffaqiyatli yangilandi.",
            })

        # 2. Newly outdated (or latest release bumped while outdated)
        elif outdated and (not prev_outdated or (prev_latest and prev_latest != latest)):
            update_cmd = (
                "npm install -g @anthropic-ai/claude-code@latest"
                if tname == "claude"
                else (f"pip install -U {tname}" if tname == "codex" else "agy update")
            )
            events.append({
                "id": f"probe:cli_versions:outdated:{tname}:{installed}:{latest}",
                "source_id": "probe:cli_versions",
                "entry_id": f"outdated:{tname}:{installed}",
                "vendor": vendor,
                "product": product,
                "title": f"{product} yangilanishi mavjud (o'rnatilgan: {installed}, so'nggi: {latest})",
                "link": "",
                "published_at": now_iso,
                "raw_content": (
                    f"{product} vositangiz eskirgan. O'rnatilgan versiya: {installed}, "
                    f"rasmiy oxirgi reliz: {latest}. Yangilash tavsiya etiladi."
                ),
                "triage_type": "account_change",
                "priority": "digest",
                "status": "unseen",
                "command": update_cmd,
                "profile_impact": f"{product} uchun yangi xususiyatlar va barqarorlik relizi mavjud.",
            })

    return events


# ---------------------------------------------------------------------------
# Runner Orchestration for Probes
# ---------------------------------------------------------------------------


def run_all_probes(
    conn,
    config: dict | None = None,
    fixtures: bool = False,
) -> list[dict]:
    """Execute all tier-3 account probes, store snapshots, diff, and return new events."""
    from . import store

    cfg = config or load_probes_config()
    latest_releases = store.get_latest_release_versions(conn)
    extra_candidates = store.get_announced_model_candidates(conn)

    generated_events: list[dict] = []

    # 1. agy_models
    res_models = probe_agy_models(cfg, fixtures=fixtures)
    prev_models_snap = store.get_latest_probe_snapshot(conn, "agy_models", only_successful=True)
    prev_models_data = prev_models_snap.get("data") if prev_models_snap else None
    if res_models["status"] == "success":
        store.save_probe_snapshot(conn, "agy_models", res_models, status="success")
        generated_events.extend(diff_agy_models(res_models, prev_models_data))
    else:
        store.save_probe_snapshot(conn, "agy_models", res_models, status="error")

    # 2. agy_changelog
    res_changelog = probe_agy_changelog(cfg, fixtures=fixtures)
    prev_cl_snap = store.get_latest_probe_snapshot(conn, "agy_changelog", only_successful=True)
    prev_cl_data = prev_cl_snap.get("data") if prev_cl_snap else None
    if res_changelog["status"] == "success":
        store.save_probe_snapshot(conn, "agy_changelog", res_changelog, status="success")
        generated_events.extend(diff_agy_changelog(res_changelog, prev_cl_data))
    else:
        store.save_probe_snapshot(conn, "agy_changelog", res_changelog, status="error")

    # 3. cli_versions
    res_cli = probe_cli_versions(cfg, fixtures=fixtures, latest_releases=latest_releases)
    prev_cli_snap = store.get_latest_probe_snapshot(conn, "cli_versions", only_successful=True)
    prev_cli_data = prev_cli_snap.get("data") if prev_cli_snap else None
    if res_cli["status"] == "success":
        store.save_probe_snapshot(conn, "cli_versions", res_cli, status="success")
        generated_events.extend(diff_cli_versions(res_cli, prev_cli_data))
    else:
        store.save_probe_snapshot(conn, "cli_versions", res_cli, status="error")

    # 4. vertex_models
    res_vertex = probe_vertex_models(cfg, fixtures=fixtures, extra_candidates=extra_candidates)
    prev_vx_snap = store.get_latest_probe_snapshot(conn, "vertex_models", only_successful=True)
    prev_vx_data = prev_vx_snap.get("data") if prev_vx_snap else None
    if res_vertex["status"] == "success":
        store.save_probe_snapshot(conn, "vertex_models", res_vertex, status="success")
        generated_events.extend(diff_vertex_models(res_vertex, prev_vx_data))
    else:
        store.save_probe_snapshot(conn, "vertex_models", res_vertex, status="error")

    # Persist probe events in SQLite
    new_persisted = store.record_probe_events(conn, generated_events)
    return new_persisted
