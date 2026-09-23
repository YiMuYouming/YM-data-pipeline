from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "ym-data"


class RepoLauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.temp = Path(self._temporary.name)
        self.fake_bin = self.temp / "fake bin"
        self.fake_bin.mkdir()
        self.cache_dir = self.temp / "uv cache with spaces"
        self.log_path = self.temp / "uv-calls.jsonl"
        fake_uv = self.fake_bin / "uv"
        fake_uv.write_text(
            f"#!{sys.executable}\n"
            + """\
import json
import os
import sys

if sys.argv[1:] == ["cache", "dir"]:
    if os.environ.get("FAKE_UV_CACHE_FAIL") == "1":
        print("raw-cache-error-marker", file=sys.stderr)
        raise SystemExit(23)
    print(os.environ["FAKE_UV_CACHE_DIR"])
    raise SystemExit(0)

if sys.argv[1:] == ["--version"]:
    raise SystemExit(0)

with open(os.environ["FAKE_UV_LOG"], "a", encoding="utf-8") as handle:
    json.dump(
        {
            "argv": sys.argv[1:],
            "project_environment": os.environ.get("UV_PROJECT_ENVIRONMENT"),
        },
        handle,
    )
    handle.write("\\n")
""",
            encoding="utf-8",
        )
        fake_uv.chmod(0o755)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def _project(self, name: str) -> Path:
        project = self.temp / name
        project.mkdir()
        launcher = project / "ym-data"
        shutil.copy2(LAUNCHER, launcher)
        launcher.chmod(0o755)
        return project

    def _run(
        self,
        project: Path,
        *arguments: str,
        override: str | None = None,
        uv_bin: str | None = None,
        cache_fail: bool = False,
        path: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.pop("UV_PROJECT_ENVIRONMENT", None)
        environment.pop("YM_DATA_UV_BIN", None)
        environment.update(
            {
                "PATH": path
                or os.pathsep.join((str(self.fake_bin), "/usr/bin", "/bin")),
                "FAKE_UV_CACHE_DIR": str(self.cache_dir),
                "FAKE_UV_LOG": str(self.log_path),
            }
        )
        if override is not None:
            environment["UV_PROJECT_ENVIRONMENT"] = override
        if uv_bin is not None:
            environment["YM_DATA_UV_BIN"] = uv_bin
        if cache_fail:
            environment["FAKE_UV_CACHE_FAIL"] = "1"
        return subprocess.run(
            [str(project / "ym-data"), *arguments],
            cwd=self.temp,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def _calls(self) -> list[dict]:
        if not self.log_path.exists():
            return []
        return [
            json.loads(line)
            for line in self.log_path.read_text(encoding="utf-8").splitlines()
        ]

    def test_default_environment_is_external_stable_and_root_specific(self):
        first = self._project("project one")
        second = self._project("project two")

        self.assertEqual(0, self._run(first, "doctor", "--json").returncode)
        self.assertEqual(0, self._run(first, "list").returncode)
        self.assertEqual(0, self._run(second, "doctor", "--json").returncode)

        environments = [call["project_environment"] for call in self._calls()]
        first_environment = Path(environments[0])
        expected_hash = hashlib.sha256(str(first.resolve()).encode()).hexdigest()
        self.assertEqual(first_environment, Path(environments[1]))
        self.assertNotEqual(first_environment, Path(environments[2]))
        self.assertFalse(first_environment.is_relative_to(first.resolve()))
        self.assertEqual(
            self.cache_dir / "ym-stock-data-project-envs" / expected_hash,
            first_environment,
        )

    def test_explicit_environment_override_is_preserved(self):
        project = self._project("override project")
        override = str(self.temp / "caller selected environment")

        completed = self._run(project, "doctor", override=override)

        self.assertEqual(0, completed.returncode)
        self.assertEqual(override, self._calls()[0]["project_environment"])

    def test_arguments_are_forwarded_without_rewriting(self):
        project = self._project("argument project")
        arguments = ("query", "review_sentiment", "--query", "A 股 非ST", "")

        completed = self._run(project, *arguments)

        self.assertEqual(0, completed.returncode)
        self.assertEqual(
            ["--project", str(project.resolve()), "run", "ym-data", *arguments],
            self._calls()[0]["argv"],
        )

    def test_missing_explicit_uv_fails_fast(self):
        project = self._project("missing explicit uv")

        completed = self._run(
            project,
            "doctor",
            uv_bin=str(self.temp / "does-not-exist"),
        )

        self.assertEqual(126, completed.returncode)
        self.assertIn("YM_DATA_UV_BIN is not runnable", completed.stderr)
        self.assertEqual([], self._calls())

    def test_nonzero_explicit_uv_is_sanitized_and_does_not_fallback(self):
        project = self._project("invalid explicit uv")
        invalid_uv = self.temp / "invalid uv"
        invalid_uv.write_text(
            "#!/bin/sh\nprintf '%s\\n' 'raw-probe-error-marker' >&2\nexit 23\n",
            encoding="utf-8",
        )
        invalid_uv.chmod(0o755)

        completed = self._run(project, "doctor", uv_bin=str(invalid_uv))

        self.assertEqual(126, completed.returncode)
        self.assertIn("YM_DATA_UV_BIN is not runnable", completed.stderr)
        self.assertNotIn("raw-probe-error-marker", completed.stderr)
        self.assertEqual([], self._calls())

    def test_valid_explicit_uv_is_used(self):
        project = self._project("valid explicit uv")
        shadow_bin = self.temp / "explicit shadow bin"
        shadow_bin.mkdir()
        shadow_uv = shadow_bin / "uv"
        shadow_uv.write_text("#!/bin/sh\nexit 126\n", encoding="utf-8")
        shadow_uv.chmod(0o755)

        completed = self._run(
            project,
            "doctor",
            uv_bin=str(self.fake_bin / "uv"),
            path=os.pathsep.join((str(shadow_bin), "/usr/bin", "/bin")),
        )

        self.assertEqual(0, completed.returncode)
        self.assertEqual("doctor", self._calls()[0]["argv"][-1])

    def test_unusable_shadowed_uv_does_not_hide_working_candidate(self):
        project = self._project("shadowed uv")
        shadow_bin = self.temp / "shadow bin"
        shadow_bin.mkdir()
        shadow_uv = shadow_bin / "uv"
        shadow_uv.write_text("#!/bin/sh\nexit 126\n", encoding="utf-8")
        shadow_uv.chmod(0o755)
        path = os.pathsep.join(
            (str(shadow_bin), str(self.fake_bin), "/usr/bin", "/bin")
        )

        completed = self._run(project, "doctor", path=path)

        self.assertEqual(0, completed.returncode)
        self.assertEqual("doctor", self._calls()[0]["argv"][-1])

    def test_cache_discovery_failure_is_sanitized(self):
        project = self._project("cache failure")

        completed = self._run(project, "doctor", cache_fail=True)

        self.assertEqual(69, completed.returncode)
        self.assertIn("unable to resolve uv cache directory", completed.stderr)
        self.assertNotIn("raw-cache-error-marker", completed.stderr)
        self.assertEqual([], self._calls())


class LauncherDocumentationTests(unittest.TestCase):
    def test_formal_docs_recommend_repo_launcher_for_cli(self):
        for relative in ("README.md", "docs/INSTALL.md", "AGENTS.md"):
            with self.subTest(file=relative):
                text = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn("./ym-data doctor --json", text)
                self.assertFalse(
                    any(
                        line.strip().startswith("uv run ym-data")
                        for line in text.splitlines()
                    )
                )

        plan_path = (
            ROOT
            / "docs/superpowers/plans/2026-07-29-unified-a-share-data-channel.md"
        )
        plan = plan_path.read_text(encoding="utf-8")
        task_14 = plan.split("### Task 14:", 1)[1]
        self.assertIn("./ym-data doctor --json", task_14)
        self.assertFalse(
            any(
                line.strip().startswith("uv run ym-data")
                for line in task_14.splitlines()
            )
        )

    def test_python_library_entry_remains_public_query(self):
        for relative in ("README.md", "docs/INSTALL.md", "AGENTS.md"):
            with self.subTest(file=relative):
                text = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn("from ym_stock_data import query", text)


if __name__ == "__main__":
    unittest.main()
