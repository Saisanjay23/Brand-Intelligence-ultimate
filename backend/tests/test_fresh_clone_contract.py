"""What a fresh clone depends on: requirements.txt, the doctor, and the docs agree.

THE BUG THIS PINS. `run.py` demanded `apscheduler`, which the code dropped long
ago and requirements.txt never installed. On a machine that already had it the
doctor said "ok"; on a clean laptop it could never be satisfied, so `--check`
failed for ever and every `python run.py` re-ran the whole setup. Nothing in the
repo noticed, because the only machine anyone ran it on had the package.

So both directions are checked here, offline, against the files themselves:
  * everything the doctor demands is something requirements.txt installs;
  * everything the code imports is installable from requirements.txt (optional
    libraries, guarded by `try/except ImportError`, are the stated exceptions).
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# import name -> the pip name that provides it, where they differ
PIP_NAME = {
    "PIL": "pillow",
    "bson": "pymongo",
    "multipart": "python-multipart",
    "pydantic_settings": "pydantic-settings",
    "dns": "dnspython",
}
# Optional by construction: guarded imports, absent from requirements on
# purpose (the CLIP tier degrades to the hash tier without them).
OPTIONAL = {"torch", "transformers"}


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirements() -> set[str]:
    names = set()
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            names.add(_norm(re.split(r"[\[<>=!~; ]", line, maxsplit=1)[0]))
    return names


def _run_module():
    spec = importlib.util.spec_from_file_location("run_under_test", ROOT / "run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestDoctorMatchesRequirements:
    def test_everything_the_doctor_demands_gets_installed(self):
        reqs = _requirements()
        demanded = {_norm(pip) for pip in _run_module().IMPORT_NAMES}
        missing = sorted(demanded - reqs)
        assert not missing, (
            f"run.py's IMPORT_NAMES demands {missing}, which requirements.txt never "
            "installs -- on a clean laptop --check would fail for ever")

    def test_each_demanded_package_imports_under_the_name_given(self):
        # the import name must be the one the pip package really provides
        for pip, mod in _run_module().IMPORT_NAMES.items():
            assert PIP_NAME.get(mod, mod).replace("_", "-") == _norm(pip) or _norm(pip) == _norm(mod), (pip, mod)

    def test_the_hard_dependencies_are_all_checked(self):
        checked = {_norm(pip) for pip in _run_module().IMPORT_NAMES}
        for hard in ("fastapi", "uvicorn", "pydantic", "pydantic-settings", "motor", "pymongo",
                     "pillow", "imagehash", "aiohttp", "httpx"):
            assert hard in checked, f"{hard} is imported unguarded but the doctor never checks it"


class TestEveryImportIsInstallable:
    def _third_party_imports(self):
        stdlib = set(sys.stdlib_module_names)
        out: dict[str, str] = {}
        files = [p for p in (ROOT / "backend").rglob("*.py") if "tests" not in p.parts]
        files += [ROOT / "run.py", ROOT / "migrate_mongo.py"]
        for p in files:
            tree = ast.parse(p.read_text(encoding="utf-8"))
            for n in ast.walk(tree):
                if isinstance(n, ast.Import):
                    names = [a.name.split(".")[0] for a in n.names]
                elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                    names = [n.module.split(".")[0]]
                else:
                    continue
                for nm in names:
                    if nm not in stdlib and nm not in ("backend", "run", "migrate_mongo"):
                        out.setdefault(nm, p.relative_to(ROOT).as_posix())
        return out

    def test_nothing_imported_is_missing_from_requirements(self):
        reqs = _requirements()
        missing = {
            mod: where for mod, where in self._third_party_imports().items()
            if mod not in OPTIONAL and _norm(PIP_NAME.get(mod, mod)) not in reqs
        }
        assert not missing, (
            f"imported but not in requirements.txt: {missing} -- a clean install would "
            "fail at import time")

    def test_the_optional_libraries_really_are_optional(self):
        # not in requirements, and every use sits behind a lazy/guarded import
        assert not (OPTIONAL & _requirements())


class TestSetupSkipsAnUnneededDownload:
    """`--setup` fetched Playwright's ~300 MB Chromium on every laptop, though
    sessions launch the real Chrome the machine already has and only fall back
    to that Chromium when there is none."""

    def _setup(self, monkeypatch, tmp_path, chrome):
        run = _run_module()
        calls: list[list[str]] = []
        monkeypatch.setattr(run, "ROOT", tmp_path)
        monkeypatch.setattr(run, "FRONTEND", tmp_path / "no-frontend")
        monkeypatch.setattr(run, "missing_packages", lambda: [])
        monkeypatch.setattr(run, "have", lambda m: True)
        monkeypatch.setattr(run, "chrome_on_this_machine", lambda: chrome)
        monkeypatch.setattr(run, "run", lambda cmd, cwd=None, why="": calls.append(cmd) or True)
        run.setup()
        return calls

    def test_chrome_present_means_no_chromium_download(self, monkeypatch, tmp_path):
        calls = self._setup(monkeypatch, tmp_path, "C:/Chrome/chrome.exe")
        assert not any("playwright" in part for c in calls for part in c)

    def test_no_chrome_still_downloads_the_fallback(self, monkeypatch, tmp_path):
        calls = self._setup(monkeypatch, tmp_path, "")
        assert any(c[-3:] == ["playwright", "install", "chromium"] for c in calls) or any(
            "install" in c and "chromium" in c for c in calls)

    def test_the_probe_answer_is_the_last_real_path_not_a_log_line(self, tmp_path):
        run = _run_module()
        exe = tmp_path / "chrome.exe"
        exe.write_text("x")
        out = '{"ts": "2026-11-04", "msg": "using installed Chrome"}\n' + str(exe) + "\n"
        assert run.last_existing_path(out) == str(exe)
        assert run.last_existing_path('{"msg": "nothing"}\n') == ""
        assert run.last_existing_path("") == ""


class TestEnvTemplateEqualsTheDefaults:
    """Copying backend/.env.example must not change how the tool behaves.

    It used to carry ANALYSIS_DELAY_SEC=6.0 and DISCOVERY_MAX_SECONDS=300 against
    code defaults of 2.5 and 900, so a laptop that copied the template ran slower
    analysis and cut every sweep off after five minutes instead of fifteen, while
    a laptop that did not copy it behaved differently. Same repo, different tool.
    """

    def _template(self) -> dict[str, str]:
        out = {}
        for line in (ROOT / "backend" / ".env.example").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
        return out

    def test_every_active_value_is_the_code_default(self):
        from backend.config.settings import Settings

        defaults = Settings(_env_file=None)
        differs = []
        for key, raw in self._template().items():
            attr = key.lower()
            if not hasattr(defaults, attr):
                continue                      # read from os.environ, not Settings
            want = getattr(defaults, attr)
            if raw == "":
                ok = want in ("", None, 0, [], ())
            elif isinstance(want, bool):
                ok = raw.lower() == str(want).lower()
            elif isinstance(want, (int, float)):
                ok = float(raw) == float(want)
            elif isinstance(want, list):
                ok = [p.strip() for p in raw.split(",") if p.strip()] == want
            else:
                ok = raw == str(want)
            if not ok:
                differs.append((key, raw, want))
        assert not differs, f"template values that are not the code default: {differs}"

    def test_a_commented_knob_names_a_real_setting(self):
        from backend.config.settings import Settings

        fields = set(Settings.model_fields)
        bad = []
        for line in (ROOT / "backend" / ".env.example").read_text(encoding="utf-8").splitlines():
            m = re.match(r"^#\s*([A-Z][A-Z0-9_]+)=", line)
            if m and m.group(1).lower() not in fields and m.group(1) != "CHROME_BINARY":
                bad.append(m.group(1))
        assert not bad, f"the template documents settings that do not exist: {bad}"


class TestDocsMatchTheTestedPythons:
    def test_the_readme_does_not_claim_a_python_nobody_ran(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        assert "3.10 to 3.12" not in readme, "stale Python range in the README"
