"""One-service Kubernetes candidate gate. Requires kubectl and curl in the probe pod."""
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request


class OperationError(RuntimeError):
    pass


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def kubectl(config, *arguments, timeout=30):
    return subprocess.run(
        ["kubectl", "--namespace", config.namespace, "--request-timeout=15s", *arguments],
        capture_output=True, text=True, timeout=timeout, check=False,
    )


def checked(config, *arguments, timeout=30):
    completed = kubectl(config, *arguments, timeout=timeout)
    if completed.returncode:
        # Never persist stderr: cluster errors may contain credentials or request URLs.
        raise OperationError("kubectl command failed")
    return completed.stdout


def deployment(config):
    return json.loads(checked(config, "get", "deployment", config.deployment, "-o", "json"))


def fingerprint(template):
    return hashlib.sha256(json.dumps(template, sort_keys=True).encode()).hexdigest()


def images(deploy):
    return {entry["name"]: entry["image"] for entry in deploy["spec"]["template"]["spec"]["containers"]}


def ready(deploy):
    desired = deploy["spec"].get("replicas", 1)
    status = deploy.get("status", {})
    return desired > 0 and status.get("observedGeneration", 0) == deploy["metadata"]["generation"] and all(
        status.get(field, 0) == desired
        for field in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas")
    ) and status.get("unavailableReplicas", 0) == 0


def selector(deploy):
    labels = deploy["spec"]["selector"]
    terms = [f"{key}={label}" for key, label in labels.get("matchLabels", {}).items()]
    for expression in labels.get("matchExpressions", []):
        key, operator = expression["key"], expression["operator"]
        if operator in ("In", "NotIn"):
            terms.append(f"{key} {'in' if operator == 'In' else 'notin'} ({','.join(expression['values'])})")
        elif operator in ("Exists", "DoesNotExist"):
            terms.append(key if operator == "Exists" else "!" + key)
        else:
            raise OperationError("unsupported selector")
    if not terms:
        raise OperationError("empty selector")
    return ",".join(terms)


def pod_status(pod):
    status = pod.get("status", {})
    return {
        "name": pod["metadata"]["name"], "phase": status.get("phase"),
        "conditions": [{"type": condition.get("type"), "status": condition.get("status")}
                       for condition in status.get("conditions", [])],
        "containers": [{"name": container.get("name"), "ready": container.get("ready"),
                        "restarts": container.get("restartCount"),
                        "state": list(container.get("state", {}))}
                       for container in status.get("containerStatuses", [])],
    }


def snapshot(config, deploy):
    metadata = deploy["metadata"]
    pods = json.loads(checked(config, "get", "pods", "-l", selector(deploy), "-o", "json"))
    return {
        "timestamp": timestamp(), "uid": metadata["uid"],
        "generation": metadata["generation"],
        "observed_generation": deploy.get("status", {}).get("observedGeneration"),
        "revision": metadata.get("annotations", {}).get("deployment.kubernetes.io/revision"),
        "images": images(deploy), "ready": ready(deploy),
        "template_hash": fingerprint(deploy["spec"]["template"]),
        "pods": [pod_status(pod) for pod in pods["items"]],
    }


def rollout(config):
    checked(config, "rollout", "status", "deployment/" + config.deployment,
            "--timeout=120s", timeout=125)


def sample(config):
    started = time.monotonic()
    observation = {"timestamp": timestamp(), "status": None, "curl_exit": None,
                   "infrastructure_error": False, "timeout": False}
    try:
        response = kubectl(config, "exec", config.probe_pod, "--", "curl", "-sS", "-o",
                           "/dev/null", "-w", "%{http_code}", "--max-time", "3", config.url,
                           timeout=10)
        raw_status = response.stdout.strip()
        observation["curl_exit"] = response.returncode
        if re.fullmatch(r"[1-5][0-9]{2}", raw_status):
            observation["status"] = int(raw_status)
        observation["timeout"] = response.returncode == 28
        transport_failure = response.returncode in (7, 28) and raw_status == "000"
        observation["infrastructure_error"] = not transport_failure and (
            observation["status"] is None or response.returncode != 0)
    except (subprocess.TimeoutExpired, OSError):
        observation["timeout"] = True
        observation["infrastructure_error"] = True
    observation["duration_seconds"] = round(time.monotonic() - started, 6)
    observation["failed"] = (observation["infrastructure_error"] or observation["curl_exit"] != 0
                             or not 200 <= observation["status"] < 300)
    return observation


def probe(config, phase):
    time.sleep(config.warmup)
    observations = []
    with (Path(config.output) / (phase + "-probes.jsonl")).open("w", encoding="utf-8") as stream:
        for index in range(config.samples):
            observation = sample(config)
            observations.append(observation)
            stream.write(json.dumps(observation) + "\n")
            stream.flush()
            if index + 1 < config.samples:
                time.sleep(config.interval)
    failures = sum(observation["failed"] for observation in observations)
    rate = failures / config.samples
    return {"samples": config.samples, "failures": failures, "failure_rate": rate,
            "healthy": rate <= config.threshold and not any(
                observation["infrastructure_error"] for observation in observations),
            "file": phase + "-probes.jsonl"}


def same_candidate(deploy, expected):
    return (deploy["metadata"]["uid"] == expected["uid"] and
            deploy["metadata"]["generation"] == expected["generation"] and
            fingerprint(deploy["spec"]["template"]) == expected["template_hash"])


def escalation(config, decision):
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook:
        return "not_configured"
    report = {key: decision.get(key) for key in
              ("outcome", "error", "candidate_image", "probes", "snapshots", "run_url")}
    text = f"AKS rollback escalation: {config.namespace}/{config.deployment}\n" + json.dumps(report)
    try:
        if urllib.parse.urlsplit(webhook).scheme != "https":
            return "failed_invalid_webhook"
        request = urllib.request.Request(webhook, json.dumps({"text": text}).encode(),
                                         {"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:
            return "sent" if 200 <= response.status < 300 else "failed_http"
    except (urllib.error.URLError, OSError, ValueError):
        return "failed_network"


def run_url():
    server, repository, run = (os.environ.get(key) for key in
                               ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
    return f"{server.rstrip('/')}/{repository}/actions/runs/{run}" if all((server, repository, run)) else None


def validate(config):
    if not re.fullmatch(r"[^\s@]+@sha256:[0-9a-fA-F]{64}", config.image):
        raise OperationError("candidate must be pinned to a sha256 digest")
    for name in (config.namespace, config.deployment, config.container, config.probe_pod):
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", name):
            raise OperationError("invalid Kubernetes name")
    if config.samples <= 0 or not 0 <= config.threshold <= 1:
        raise OperationError("invalid sample count or threshold")
    if any(not math.isfinite(duration) or duration < 0 for duration in (config.interval, config.warmup)):
        raise OperationError("invalid duration")
    address = urllib.parse.urlsplit(config.url)
    if address.scheme not in ("http", "https") or not address.hostname or address.username or address.password:
        raise OperationError("invalid probe URL")


def baseline(config, decision):
    deploy = deployment(config)
    before = snapshot(config, deploy)
    decision["snapshots"]["before"] = before
    if not str(before["revision"]).isdigit() or int(before["revision"]) < 1 or not ready(deploy):
        raise OperationError("baseline is not ready or revision is missing")
    if config.container not in before["images"] or before["images"][config.container] == config.image:
        raise OperationError("container missing or candidate already deployed")
    template = copy.deepcopy(deploy["spec"]["template"])
    for container in template["spec"]["containers"]:
        if container["name"] == config.container:
            container["image"] = config.image
    return {"uid": before["uid"], "generation": before["generation"] + 1,
            "template_hash": fingerprint(template)}


def evaluate_candidate(config, decision, expected):
    checked(config, "set", "image", "deployment/" + config.deployment, config.container + "=" + config.image)
    try:
        rollout(config)
        decision["candidate_rollout"] = "ready"
    except (OperationError, subprocess.TimeoutExpired):
        decision["candidate_rollout"] = "failed"
    deploy = deployment(config)
    decision["snapshots"]["candidate"] = snapshot(config, deploy)
    if not same_candidate(deploy, expected):
        raise OperationError("candidate changed concurrently")
    if decision["candidate_rollout"] == "failed" or not ready(deploy):
        return False
    decision["probes"]["candidate"] = probe(config, "candidate")
    current = deployment(config)
    decision["snapshots"]["failing"] = snapshot(config, current)
    if not same_candidate(current, expected):
        raise OperationError("candidate changed concurrently")
    return ready(current) and decision["probes"]["candidate"]["healthy"]


def recover(config, decision, expected):
    current = deployment(config)
    decision["snapshots"]["failing"] = snapshot(config, current)
    if not same_candidate(deployment(config), expected):
        raise OperationError("candidate changed concurrently; undo aborted")
    previous = decision["snapshots"]["before"]
    # kubectl undo has no resourceVersion precondition. Serialize external writers too.
    decision["rollback_count"] = 1
    try:
        checked(config, "rollout", "undo", "deployment/" + config.deployment,
                "--to-revision=" + previous["revision"])
        rollout(config)
    finally:
        restored = deployment(config)
        decision["snapshots"]["recovered"] = snapshot(config, restored)
    restored_identity = {"uid": previous["uid"], "generation": expected["generation"] + 1,
                         "template_hash": previous["template_hash"]}
    if not same_candidate(restored, restored_identity) or not ready(restored):
        raise OperationError("rollback did not restore ready baseline images")
    decision["probes"]["recovered"] = probe(config, "recovered")
    restored = deployment(config)
    decision["snapshots"]["recovered"] = snapshot(config, restored)
    return (same_candidate(restored, restored_identity) and ready(restored)
            and decision["probes"]["recovered"]["healthy"])


def execute(config, decision):
    validate(config)
    expected = baseline(config, decision)
    if evaluate_candidate(config, decision, expected):
        decision["outcome"] = "candidate_kept"
        return 0
    decision["outcome"] = "candidate_rejected"
    if recover(config, decision, expected):
        decision["outcome"] = "candidate_rejected_recovered"
    else:
        decision["outcome"] = "rollback_unhealthy"
        decision["escalation"] = escalation(config, decision)
    return 1


def run(config):
    decision = {"namespace": config.namespace, "deployment": config.deployment,
                "candidate_image": config.image, "started_at": timestamp(),
                "outcome": "operational_error", "rollback_count": 0, "probes": {},
                "snapshots": {}, "run_url": run_url(),
                "policy": {"samples": config.samples, "interval_seconds": config.interval,
                           "warmup_seconds": config.warmup, "failure_threshold": config.threshold}}
    try:
        Path(config.output).mkdir(parents=True, exist_ok=True)
        return execute(config, decision)
    except (OperationError, subprocess.SubprocessError, OSError, ValueError,
            KeyError, TypeError, AttributeError, OverflowError) as error:
        decision["outcome"] = "operational_error"
        decision["error"] = str(error) if isinstance(error, OperationError) else type(error).__name__
        decision["escalation"] = escalation(config, decision)
        return 1
    finally:
        decision["finished_at"] = timestamp()
        try:
            destination = Path(config.output) / "decision.json"
            temporary = destination.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(decision, indent=2), encoding="utf-8")
            temporary.replace(destination)
            print(json.dumps({key: decision[key] for key in
                              ("outcome", "rollback_count", "probes")}), flush=True)
            if "error" in decision:
                print("Failure reason: " + decision["error"], flush=True)
            if "escalation" in decision:
                print("Slack delivery: " + decision["escalation"], flush=True)
        except OSError:
            # A failed evidence write must never turn a deployment into a passing job.
            print("Unable to write decision evidence.")
            return 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for argument in ("namespace", "deployment", "container", "image", "probe-pod"):
        parser.add_argument("--" + argument, required=True)
    parser.add_argument("--url", default="http://frontend/")
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--interval", type=float, default=2)
    parser.add_argument("--threshold", type=float, default=0.2)
    parser.add_argument("--warmup", type=float, default=10)
    parser.add_argument("--output", default="evidence")
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
