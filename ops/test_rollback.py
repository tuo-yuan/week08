import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

import rollback


OLD = "registry/frontend@sha256:" + "a" * 64
NEW = "registry/frontend@sha256:" + "b" * 64


def initial_deployment():
    return {
        "metadata": {"uid": "frontend-uid", "generation": 4,
                     "annotations": {"deployment.kubernetes.io/revision": "7"}},
        "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "frontend"}},
                 "template": {"metadata": {"labels": {"app": "frontend"}},
                              "spec": {"containers": [{"name": "frontend", "image": OLD,
                                                       "env": [{"name": "SECRET", "value": "never-save-me"}]}]}}},
        "status": {"observedGeneration": 4, "replicas": 1, "updatedReplicas": 1,
                   "readyReplicas": 1, "availableReplicas": 1},
    }


class Cluster:
    def __init__(self, candidate=None, recovered=None):
        self.deploy = initial_deployment()
        self.responses = {"candidate": list(candidate or [(0, "200")] * 5),
                          "recovered": list(recovered or [(0, "200")] * 5)}
        self.phase = "before"
        self.commands = []
        self.fail_rollout = False
        self.fail_undo = False
        self.change_after_probe = False

    def __call__(self, argv, **kwargs):
        assert kwargs["timeout"] <= 125 and kwargs["capture_output"]
        assert "shell" not in kwargs
        self.commands.append(argv)
        command = argv[4:]
        code, output = 0, ""
        if command[:2] == ["get", "deployment"]:
            output = json.dumps(self.deploy)
        elif command[:2] == ["get", "pods"]:
            output = json.dumps({"items": [{"metadata": {"name": "frontend-123"},
                                            "status": {"phase": "Running", "message": "never-save-me"}}]})
        elif command[:2] == ["set", "image"]:
            self.phase = "candidate"
            self.update(NEW, "8")
        elif command[:2] == ["rollout", "undo"]:
            self.phase = "recovered"
            if self.fail_undo:
                code = 1
            else:
                self.update(OLD, "9")
        elif command[:2] == ["rollout", "status"]:
            code = int(self.fail_rollout and self.phase == "candidate")
        elif command[0] == "exec":
            response = self.responses[self.phase].pop(0)
            if isinstance(response, Exception):
                raise response
            code, output = response
            if self.change_after_probe and not self.responses[self.phase]:
                self.deploy["metadata"]["generation"] += 1
        else:
            raise AssertionError(command)
        return subprocess.CompletedProcess(argv, code, output, "secret stderr")

    def update(self, image, revision):
        self.deploy["metadata"]["generation"] += 1
        self.deploy["metadata"]["annotations"]["deployment.kubernetes.io/revision"] = revision
        self.deploy["status"]["observedGeneration"] = self.deploy["metadata"]["generation"]
        self.deploy["spec"]["template"]["spec"]["containers"][0]["image"] = image


class RollbackTests(unittest.TestCase):
    def run_controller(self, cluster, *extra):
        with tempfile.TemporaryDirectory() as directory:
            arguments = ["--namespace", "production", "--deployment", "frontend", "--container",
                         "frontend", "--image", NEW, "--probe-pod", "http-probe", "--output", directory,
                         "--samples", "5", "--interval", "0", "--warmup", "0", *extra]
            with patch.object(rollback.subprocess, "run", side_effect=cluster), \
                    patch.object(rollback.time, "sleep"), patch.dict(os.environ, {}, clear=True):
                exit_code = rollback.main(arguments)
            evidence = {entry.name: entry.read_text() for entry in Path(directory).iterdir()}
        self.assertNotIn("never-save-me", json.dumps(evidence))
        self.assertNotIn("secret stderr", json.dumps(evidence))
        return exit_code, json.loads(evidence["decision.json"]), evidence

    def test_healthy_kept(self):
        cluster = Cluster()
        code, decision, evidence = self.run_controller(cluster)
        self.assertEqual(code, 0)
        self.assertEqual(decision["outcome"], "candidate_kept")
        self.assertEqual(decision["rollback_count"], 0)
        observations = [json.loads(line) for line in evidence["candidate-probes.jsonl"].splitlines()]
        self.assertEqual(len(observations), 5)
        self.assertTrue(all(row["status"] == 200 and row["curl_exit"] == 0 for row in observations))
        self.assertTrue(all("timestamp" in row and "duration_seconds" in row for row in observations))

    def test_bad_candidate_recovers_but_exit_fails(self):
        cluster = Cluster(candidate=[(0, "500")] * 5)
        code, decision, evidence = self.run_controller(cluster)
        self.assertEqual((code, decision["outcome"]), (1, "candidate_rejected_recovered"))
        undo = [command for command in cluster.commands if "undo" in command]
        self.assertEqual(len(undo), 1)
        self.assertIn("--to-revision=7", undo[0])
        self.assertEqual(decision["probes"]["candidate"]["failure_rate"], 1)
        self.assertEqual(decision["probes"]["recovered"]["failure_rate"], 0)
        self.assertEqual(len(evidence["recovered-probes.jsonl"].splitlines()), 5)

    def test_both_bad_one_undo_and_escalation(self):
        with patch.object(rollback, "escalation", return_value="sent") as slack:
            code, decision, _ = self.run_controller(Cluster([(0, "500")] * 5, [(0, "503")] * 5))
        self.assertEqual(code, 1)
        self.assertEqual(decision["outcome"], "rollback_unhealthy")
        self.assertEqual(decision["rollback_count"], 1)
        slack.assert_called_once()
        self.assertEqual(set(decision["snapshots"]), {"before", "candidate", "failing", "recovered"})

    def test_threshold_equality_passes(self):
        code, decision, _ = self.run_controller(Cluster([(0, "503")] + [(0, "204")] * 4))
        self.assertEqual(code, 0)
        self.assertEqual(decision["probes"]["candidate"]["failure_rate"], 0.2)

    def test_infrastructure_never_healthy_even_with_threshold_one(self):
        for response in ((0, ""), (0, "garbage"), (0, "000"), (1, "200"),
                         subprocess.TimeoutExpired("kubectl", 10)):
            with self.subTest(response=response):
                cluster = Cluster([response] + [(0, "200")] * 4)
                code, decision, evidence = self.run_controller(cluster, "--threshold", "1")
                self.assertEqual(code, 1)
                self.assertFalse(decision["probes"]["candidate"]["healthy"])
                self.assertEqual(len(evidence["candidate-probes.jsonl"].splitlines()), 5)

    def test_curl_timeouts_are_failures_with_threshold_equality(self):
        code, decision, evidence = self.run_controller(Cluster([(28, "000")] + [(0, "200")] * 4))
        self.assertEqual(code, 0)
        self.assertEqual(decision["probes"]["candidate"]["failure_rate"], 0.2)
        first = json.loads(evidence["candidate-probes.jsonl"].splitlines()[0])
        self.assertTrue(first["timeout"] and first["failed"])
        self.assertEqual(first["curl_exit"], 28)

    def test_candidate_rollout_failure(self):
        cluster = Cluster()
        cluster.fail_rollout = True
        code, decision, _ = self.run_controller(cluster)
        self.assertEqual((code, decision["rollback_count"]), (1, 1))
        self.assertEqual(decision["candidate_rollout"], "failed")
        self.assertNotIn("candidate", decision["probes"])
        self.assertEqual(decision["outcome"], "candidate_rejected_recovered")

    def test_changed_deployment_aborts_without_undo(self):
        cluster = Cluster([(0, "500")] * 5)
        cluster.change_after_probe = True
        code, decision, _ = self.run_controller(cluster)
        self.assertEqual((code, decision["rollback_count"]), (1, 0))
        self.assertFalse(any("undo" in command for command in cluster.commands))

    def test_undo_failure_escalates_without_retry(self):
        cluster = Cluster([(0, "500")] * 5)
        cluster.fail_undo = True
        with patch.object(rollback, "escalation", return_value="sent") as slack:
            code, decision, _ = self.run_controller(cluster)
        self.assertEqual((code, decision["rollback_count"]), (1, 1))
        slack.assert_called_once()
        self.assertEqual(sum("undo" in command for command in cluster.commands), 1)

    def test_baseline_not_ready_and_stale_generation(self):
        for status in ({"readyReplicas": 0}, {"observedGeneration": 3}):
            cluster = Cluster()
            cluster.deploy["status"].update(status)
            code, decision, _ = self.run_controller(cluster)
            self.assertEqual(code, 1)
            self.assertEqual(decision["rollback_count"], 0)
            self.assertFalse(any("set" in command for command in cluster.commands))

    def test_validation_writes_evidence_before_any_cluster_change(self):
        for arguments in (("--image", "frontend:latest"), ("--samples", "0"),
                          ("--threshold", "nan"), ("--interval", "inf")):
            cluster = Cluster()
            code, decision, _ = self.run_controller(cluster, *arguments)
            self.assertEqual(code, 1)
            self.assertEqual(decision["outcome"], "operational_error")
            self.assertEqual(cluster.commands, [])

    def test_slack_errors_sanitized_and_timeout_bounded(self):
        config = type("Config", (), {"namespace": "production", "deployment": "frontend"})()
        webhook = "https://example.invalid/credential-do-not-persist"
        with patch.dict(os.environ, {"SLACK_WEBHOOK_URL": webhook}), \
                patch.object(rollback.urllib.request, "urlopen",
                             side_effect=urllib.error.URLError(webhook)) as network:
            status = rollback.escalation(config, {"outcome": "rollback_unhealthy"})
        self.assertEqual(status, "failed_network")
        self.assertNotIn(webhook, status)
        self.assertEqual(network.call_args.kwargs["timeout"], 10)
        request = network.call_args.args[0]
        self.assertEqual(request.method, "POST")
        self.assertIn("production/frontend", json.loads(request.data)["text"])
        self.assertNotIn(webhook, request.data.decode())


if __name__ == "__main__":
    unittest.main()
