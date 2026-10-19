"""Regression tests for the JSONL baseline and real Sphinx link checking."""

import http.server
import json
import os
import re
import runpy
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sphinx.errors import ConfigError

from pug_sphinx_extensions.linkcheck import baseline_patterns, configure_linkcheck

ROOT = Path(__file__).resolve().parents[1]


class BaselineTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.report = Path(directory.name) / "output.json"

    def write_records(self, *uris):
        self.report.write_text(
            "".join(json.dumps({"uri": uri}) + "\n" for uri in uris),
            encoding="utf-8",
        )

    def test_exact_patterns_and_duplicates(self):
        uri = "https://example.org/a+b[0].html?q=x.y&v=%2F#é"
        self.write_records(uri, uri, "", "https://EXAMPLE.org/")
        patterns = baseline_patterns(self.report)
        self.write_records(uri, "", "https://EXAMPLE.org/")
        self.assertEqual(patterns, baseline_patterns(self.report))
        for candidate in (uri, "", "https://EXAMPLE.org/"):
            self.assertTrue(any(re.match(p, candidate) for p in patterns))
        for candidate in (
            uri + "/child",
            uri + "\n",
            uri.replace("x.y", "xay"),
            uri.replace("%2F", "/"),
            uri.replace("#é", "#other"),
            uri.replace("q=", "other="),
            "https://example.org/",
            "https://EXAMPLE.org",
        ):
            with self.subTest(candidate=candidate):
                self.assertFalse(any(re.match(p, candidate) for p in patterns))

    def test_all_statuses_are_known(self):
        statuses = ("broken", "working", "ignored", "unchecked", "redirected")
        self.report.write_text(
            "".join(
                json.dumps({"uri": f"https://example.org/{s}", "status": s}) + "\n"
                for s in statuses
            ),
            encoding="utf-8",
        )
        patterns = baseline_patterns(self.report)
        for status in statuses:
            self.assertTrue(
                any(re.match(p, f"https://example.org/{status}") for p in patterns)
            )

    def test_exact_matching_across_batch_boundaries(self):
        for count in (255, 256, 257, 513):
            with self.subTest(count=count):
                uris = [f"https://example.org/{i:04d}?q=a|b[0]#é" for i in range(count)]
                self.write_records(*uris)
                patterns = [re.compile(p) for p in baseline_patterns(self.report)]
                for uri in uris:
                    self.assertEqual(sum(bool(p.match(uri)) for p in patterns), 1)
                    for suffix in ("/new", "\n", "?changed"):
                        self.assertFalse(any(p.match(uri + suffix) for p in patterns))
                self.assertFalse(
                    any(p.match("https://example.org/new") for p in patterns)
                )

    def test_empty_inventory(self):
        self.report.touch()
        self.assertEqual(baseline_patterns(self.report), [])

    def test_missing_file(self):
        with self.assertRaisesRegex(ConfigError, "Cannot read linkcheck baseline"):
            baseline_patterns(self.report)

    def test_invalid_records_fail_with_line_number(self):
        for line in ("{", "[]", "null", "{}", '{"uri": 1}', "\n"):
            with self.subTest(line=line):
                self.report.write_text('{"uri": "old"}\n' + line, encoding="utf-8")
                with self.assertRaisesRegex(ConfigError, "line 2"):
                    baseline_patterns(self.report)

    def test_invalid_encoding(self):
        self.report.write_bytes(b"\xff")
        with self.assertRaisesRegex(ConfigError, "Cannot read linkcheck baseline"):
            baseline_patterns(self.report)

    def test_json_decoder_resource_limits_fail_with_line_number(self):
        for value in ("[" * 2000 + "0" + "]" * 2000, "9" * 5000):
            with self.subTest(size=len(value)):
                self.report.write_text(
                    '{"uri": "old"}\n{"uri": "new", "extra":' + value + "}",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ConfigError, "line 2"):
                    baseline_patterns(self.report)

    def test_activation_and_preserved_exclusions(self):
        self.write_records("https://example.org/old")
        for event in (
            "pull_request",
            "merge_group",
            "push",
            "schedule",
            "workflow_dispatch",
        ):
            for builder in ("linkcheck", "html", "gettext"):
                for actions in ("true", "false"):
                    with self.subTest(event=event, builder=builder, actions=actions):
                        app = SimpleNamespace(
                            builder=SimpleNamespace(name=builder),
                            config=SimpleNamespace(linkcheck_ignore=["existing"]),
                        )
                        with patch.dict(
                            os.environ,
                            {
                                "GITHUB_ACTIONS": actions,
                                "GITHUB_EVENT_NAME": event,
                                "LINKCHECK_BASELINE": str(self.report),
                            },
                            clear=True,
                        ):
                            configure_linkcheck(app)
                        active = (
                            actions == "true"
                            and builder == "linkcheck"
                            and event in {"pull_request", "merge_group"}
                        )
                        self.assertEqual(
                            len(app.config.linkcheck_ignore), 2 if active else 1
                        )
                        self.assertEqual(app.config.linkcheck_ignore[0], "existing")

    def test_producer_has_no_baseline(self):
        app = SimpleNamespace(
            builder=SimpleNamespace(name="linkcheck"),
            config=SimpleNamespace(linkcheck_ignore=[".*"]),
        )
        with patch.dict(
            os.environ,
            {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "pull_request"},
            clear=True,
        ):
            configure_linkcheck(app)
        self.assertEqual(app.config.linkcheck_ignore, [".*"])

    def test_invalid_report_does_not_partly_modify_configuration(self):
        self.report.write_text('{"uri": "old"}\n{', encoding="utf-8")
        app = SimpleNamespace(
            builder=SimpleNamespace(name="linkcheck"),
            config=SimpleNamespace(linkcheck_ignore=["existing"]),
        )
        with (
            patch.dict(
                os.environ,
                {
                    "GITHUB_ACTIONS": "true",
                    "GITHUB_EVENT_NAME": "merge_group",
                    "LINKCHECK_BASELINE": str(self.report),
                },
                clear=True,
            ),
            self.assertRaises(ConfigError),
        ):
            configure_linkcheck(app)
        self.assertEqual(app.config.linkcheck_ignore, ["existing"])

    def test_repository_conf_connects_the_helper(self):
        with patch.dict(os.environ, {}, clear=True):
            config = runpy.run_path(str(ROOT / "source" / "conf.py"))
        connections = []
        config["setup"](SimpleNamespace(connect=lambda *args: connections.append(args)))
        self.assertIn(("builder-inited", configure_linkcheck), connections)


class Handler(http.server.BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.respond(body=False)

    def do_GET(self):
        self.respond(body=True)

    def respond(self, body):
        self.server.requests.append(self.path)
        content = b'<html><a id="found"></a></html>'
        self.send_response(200 if self.path in {"/valid", "/anchor"} else 404)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        if body:
            self.wfile.write(content)

    def log_message(self, *args):
        pass


class SphinxTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.source.joinpath("conf.py").write_text(
            f"import sys\nsys.path.insert(0, {str(ROOT)!r})\n"
            "from pug_sphinx_extensions.linkcheck import configure_linkcheck\n"
            "extensions = ['sphinx.ext.extlinks']\n"
            "project = 'fixture'\n"
            "linkcheck_retries = 1\nlinkcheck_timeout = 2\n"
            "def setup(app):\n    app.connect('builder-inited', configure_linkcheck)\n",
            encoding="utf-8",
        )
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.requests = []
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.count = 0

    def document(self, *paths):
        self.source.joinpath("index.rst").write_text(
            "Fixture\n=======\n\n"
            + "\n\n".join(
                f"`link {i} <{self.url}{path}>`_" for i, path in enumerate(paths)
            )
            + "\n",
            encoding="utf-8",
        )

    def build(
        self, *, baseline=None, event="pull_request", inventory=False, fresh=True
    ):
        self.count += 1
        output = self.root / f"build-{self.count}"
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("GITHUB_", "LINKCHECK_"))
        }
        env.update(
            GITHUB_ACTIONS="true",
            GITHUB_EVENT_NAME=event,
            NO_PROXY="127.0.0.1",
            PYTHONIOENCODING="utf-8",
        )
        if baseline is not None:
            env["LINKCHECK_BASELINE"] = str(baseline)
        args = [
            sys.executable,
            "-m",
            "sphinx",
            "-b",
            "linkcheck",
            "-n",
            "-W",
            "--keep-going",
            "-d",
            str(self.root / "doctrees"),
        ]
        if fresh:
            args.append("-E")
        if inventory:
            args.extend(["-D", "linkcheck_ignore=.*"])
        result = subprocess.run(
            args + [str(self.source), str(output)],
            check=False,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        report = output / "output.json"
        records = {}
        if report.exists():
            records = {
                r["uri"]: r
                for r in map(
                    json.loads, report.read_text(encoding="utf-8").splitlines()
                )
            }
        return result, report, records

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_old_broken_new_valid_and_new_broken(self):
        self.document("/old", "/old")
        result, baseline, records = self.build(inventory=True)
        self.assert_success(result)
        self.assertEqual(list(records), [self.url + "/old"])
        self.assertEqual(records[self.url + "/old"]["status"], "ignored")
        self.assertEqual(self.server.requests, [])

        self.document("/old", "/valid")
        for event in ("pull_request", "merge_group"):
            with self.subTest(event=event):
                result, _, records = self.build(
                    baseline=baseline, event=event, fresh=False
                )
                self.assert_success(result)
                self.assertEqual(records[self.url + "/old"]["status"], "ignored")
                self.assertEqual(records[self.url + "/valid"]["status"], "working")
                self.assertNotIn("/old", self.server.requests)
        self.document("/old", "/valid", "/old/new")
        result, _, records = self.build(baseline=baseline)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(records[self.url + "/old/new"]["status"], "broken")
        self.assertIn("/old/new", self.server.requests)
        self.assertNotIn("/old", self.server.requests)

    def test_full_audit_ignores_baseline_setting_and_reports_old_failure(self):
        self.document("/old")
        result, baseline, _ = self.build(inventory=True)
        self.assert_success(result)
        for event in ("push", "schedule"):
            with self.subTest(event=event):
                result, _, records = self.build(baseline=baseline, event=event)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(records[self.url + "/old"]["status"], "broken")

    def test_completed_unhealthy_audit_is_a_usable_baseline(self):
        self.document("/old")
        result, baseline, records = self.build(event="push")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(records[self.url + "/old"]["status"], "broken")
        result, _, records = self.build(baseline=baseline)
        self.assert_success(result)
        self.assertEqual(records[self.url + "/old"]["status"], "ignored")

    def test_missing_and_truncated_reports_fail_before_requests(self):
        self.document("/valid")
        report = self.root / "baseline.json"
        for content in (None, '{"uri":'):
            with self.subTest(content=content):
                if content:
                    report.write_text(content, encoding="utf-8")
                result, _, _ = self.build(baseline=report)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("linkcheck baseline", result.stdout + result.stderr)
                self.assertEqual(self.server.requests, [])

    def test_fragments_are_distinct_urls(self):
        self.document("/anchor#found")
        result, baseline, _ = self.build(inventory=True)
        self.assert_success(result)
        self.document("/anchor#found", "/anchor#absent")
        result, _, records = self.build(baseline=baseline)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(records[self.url + "/anchor#found"]["status"], "ignored")
        self.assertEqual(records[self.url + "/anchor#absent"]["status"], "broken")

    def test_image_links_are_in_inventory(self):
        self.document("/old")
        with self.source.joinpath("index.rst").open("a", encoding="utf-8") as document:
            document.write(f"\n.. image:: {self.url}/image.png\n")
        result, _, records = self.build(inventory=True)
        self.assert_success(result)
        self.assertEqual(records[self.url + "/image.png"]["status"], "ignored")
        self.assertEqual(self.server.requests, [])

    def test_generated_roles_are_in_inventory_and_new_ones_are_checked(self):
        with self.source.joinpath("conf.py").open("a", encoding="utf-8") as config:
            config.write(f"extlinks = {{'fixture': ({self.url!r} + '/%s', '%s')}}\n")
        self.source.joinpath("index.rst").write_text(
            ":fixture:`old`\n", encoding="utf-8"
        )
        result, baseline, records = self.build(inventory=True)
        self.assert_success(result)
        self.assertIn(self.url + "/old", records)
        self.source.joinpath("index.rst").write_text(
            ":fixture:`old` :fixture:`new`\n", encoding="utf-8"
        )
        result, _, records = self.build(baseline=baseline)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(records[self.url + "/new"]["status"], "broken")


if __name__ == "__main__":
    unittest.main()
