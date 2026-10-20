"""Guard the trust boundary and failure wiring of the baseline handoff."""

import os
import shlex
import subprocess
import sys
import unittest
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "test.yml"


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # BaseLoader avoids interpreting the YAML 1.2 key 'on' as a boolean.
        cls.workflow = yaml.load(
            WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader
        )
        cls.jobs = cls.workflow["jobs"]

    def action(self, job, name):
        return next(
            step
            for step in self.jobs[job]["steps"]
            if step.get("uses", "").startswith(name + "@")
        )

    def test_only_unprivileged_events(self):
        self.assertIn("pull_request", self.workflow["on"])
        self.assertIn("merge_group", self.workflow["on"])
        self.assertNotIn("pull_request_target", self.workflow["on"])
        self.assertEqual(self.workflow["permissions"], {})
        self.assertEqual(
            self.jobs["linkcheck-baseline"]["permissions"], {"contents": "read"}
        )
        self.assertNotIn("permissions", self.jobs["build"])
        self.assertNotIn("permissions", self.jobs["linkcheck"])
        for job in self.jobs.values():
            for step in job["steps"]:
                if "uses" in step:
                    self.assertRegex(step["uses"], r"@[a-f0-9]{40}$")
                self.assertNotIn("secrets.", str(step))

    def test_base_revision_and_candidate_are_separate(self):
        checkout = self.action("linkcheck-baseline", "actions/checkout")["with"]
        self.assertEqual(checkout["repository"], "${{ github.repository }}")
        self.assertEqual(
            checkout["ref"],
            "${{ github.event.pull_request.base.sha || github.event.merge_group.base_sha }}",
        )
        self.assertEqual(checkout["persist-credentials"], "false")
        # The producer must not restore dependency caches from earlier PR runs.
        self.assertNotIn(
            "cache", self.action("linkcheck-baseline", "actions/setup-python")["with"]
        )
        candidate = self.action("build", "actions/checkout")["with"]
        self.assertNotIn("ref", candidate)
        self.assertEqual(candidate["persist-credentials"], "false")

    def test_only_a_successful_fresh_inventory_is_published(self):
        producer = self.jobs["linkcheck-baseline"]
        self.assertNotIn("continue-on-error", producer)
        run = next(
            step
            for step in producer["steps"]
            if step.get("name") == "Collect baseline links"
        )
        args = shlex.split(run["run"])
        self.assertEqual(
            args,
            [
                "python",
                "-m",
                "nox",
                "-s",
                "linkcheck",
                "--",
                "-E",
                "-D",
                "linkcheck_ignore=.*",
            ],
        )
        self.assertNotIn("env", run)
        self.assertNotIn("continue-on-error", run)
        upload = self.action("linkcheck-baseline", "actions/upload-artifact")
        # Default success() gating rejects cancellation, incomplete reports and errors.
        self.assertNotIn("if", upload)
        self.assertEqual(upload["with"]["if-no-files-found"], "error")
        self.assertEqual(upload["with"]["path"], "build/output.json")
        self.assertIn("github.run_attempt", upload["with"]["name"])

    def test_artifact_identity_comes_from_this_producer(self):
        producer = self.jobs["linkcheck-baseline"]
        consumer = self.jobs["linkcheck"]
        self.assertEqual(
            producer["outputs"]["artifact-id"],
            "${{ steps.upload.outputs.artifact-id }}",
        )
        self.assertEqual(consumer["needs"], "linkcheck-baseline")
        download = self.action("linkcheck", "actions/download-artifact")["with"]
        self.assertEqual(
            download["artifact-ids"],
            "${{ needs.linkcheck-baseline.outputs.artifact-id }}",
        )
        for key in ("name", "pattern", "run-id", "repository", "github-token"):
            self.assertNotIn(key, download)
        self.assertEqual(download["path"], "${{ runner.temp }}/linkcheck-baseline")
        self.assertEqual(download["merge-multiple"], "true")
        self.assertEqual(download["digest-mismatch"], "error")
        self.assertFalse(
            any(
                "actions/cache" in step.get("uses", "")
                for job in self.jobs.values()
                for step in job["steps"]
            )
        )

    def test_empty_or_invalid_artifact_ids_fail_before_download(self):
        steps = self.jobs["linkcheck"]["steps"]
        guard = next(
            s for s in steps if s.get("name") == "Require a baseline artifact ID"
        )
        download = self.action("linkcheck", "actions/download-artifact")
        self.assertLess(steps.index(guard), steps.index(download))
        self.assertEqual(guard["if"], download["if"])
        self.assertEqual(guard["shell"], "python")
        self.assertEqual(
            guard["env"]["BASELINE_ARTIFACT_ID"], download["with"]["artifact-ids"]
        )
        self.assertNotIn("continue-on-error", guard)
        for value in (
            "",
            " ",
            "0",
            "-1",
            "1,2",
            "1oops",
            "1\n",
            "1.0",
            "١",
            "$(echo x)",
            "12345",
        ):
            with self.subTest(value=value):
                result = subprocess.run(
                    [sys.executable, "-c", guard["run"]],
                    env={**os.environ, "BASELINE_ARTIFACT_ID": value},
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
                self.assertEqual(result.returncode == 0, value == "12345")

    def test_incremental_failures_reach_the_existing_gate(self):
        build = self.jobs["build"]
        matrix = build["strategy"]["matrix"]
        self.assertIn("linkcheck_tests", matrix["noxenv"])
        linkcheck = self.jobs["linkcheck"]
        self.assertEqual(
            linkcheck["continue-on-error"],
            "${{ github.event_name == 'push' && github.ref_name != github.event.repository.default_branch }}",
        )
        nox = next(
            step for step in build["steps"] if step.get("name", "").startswith("Nox ")
        )
        self.assertNotIn("continue-on-error", nox)
        gate = self.jobs["check"]
        self.assertEqual(
            set(gate["needs"]), {"build", "linkcheck", "linkcheck-baseline"}
        )
        settings = self.action("check", "re-actors/alls-green")["with"]
        self.assertNotIn("allowed-failures", settings)
        self.assertEqual(settings["jobs"], "${{ toJSON(needs) }}")
        self.assertIn(
            '!contains(fromJSON(\'["pull_request", "merge_group"]\')',
            settings["allowed-skips"],
        )

    def test_baseline_activation_matches_download_and_producer(self):
        condition = self.jobs["linkcheck-baseline"]["if"]
        self.assertEqual(
            condition,
            'contains(fromJSON(\'["pull_request", "merge_group"]\'), github.event_name)',
        )
        download = self.action("linkcheck", "actions/download-artifact")
        self.assertIn(condition, download["if"])
        self.assertIn("matrix.noxenv == 'linkcheck'", download["if"])
        nox = next(
            step
            for step in self.jobs["build"]["steps"]
            if step.get("name", "").startswith("Nox ")
        )
        activation = nox["env"]["LINKCHECK_BASELINE"]
        self.assertIn(condition, activation)
        self.assertIn("matrix.noxenv == 'linkcheck'", activation)
        self.assertIn("runner.temp", activation)
        self.assertIn("/linkcheck-baseline/output.json", activation)
        self.assertIn("|| ''", activation)

    def test_independent_checks_start_without_baseline_and_share_runner_steps(self):
        build = self.jobs["build"]
        linkcheck = self.jobs["linkcheck"]
        self.assertNotIn("needs", build)
        self.assertNotIn("continue-on-error", build)
        self.assertEqual(
            build["strategy"]["matrix"]["noxenv"], ["build", "linkcheck_tests"]
        )
        self.assertEqual(linkcheck["strategy"]["matrix"]["noxenv"], ["linkcheck"])
        self.assertEqual(linkcheck["needs"], "linkcheck-baseline")
        for index in range(3):
            self.assertEqual(build["steps"][index], linkcheck["steps"][index])
        self.assertEqual(build["steps"][-1], linkcheck["steps"][-1])
        self.assertEqual(len(build["steps"]), 4)


if __name__ == "__main__":
    unittest.main()
