# HTTP-verified releases with bounded rollback

Task 10.3HD extends KoalaTech's delivery pipeline with a post-deployment decision. The separate `05-verified-release.yml` workflow builds the real frontend, publishes digest-addressed images to ACR, deploys a candidate to an isolated AKS namespace, and invokes `rollback.py`. It leaves the existing four 10.2D workflows unchanged. This is a single-service demonstration, not a claim that the entire application has automated recovery.

## Decision

The controller captures the baseline Deployment revision and image before changing it. Once Kubernetes reports the candidate ready, it waits 10 seconds, then makes 20 HTTP requests through a separate curl pod to the frontend ClusterIP Service. Requests are spaced by 2 seconds; each curl request has a 3-second timeout. Actual window duration includes API/exec overhead. Every non-2xx result, timeout or failed probe counts as a failure. The threshold is strictly greater than 20%: 4/20 failures pass and 5/20 fail. No successful decision is made from an empty or partial sample window.

An unhealthy candidate is undone once, to the specifically captured revision, then checked with the same policy. The controller refuses to overwrite a Deployment that another writer has changed. Recovery still leaves the delivery job failed because that candidate was rejected. If the restored release is unhealthy or rollback fails, the controller stops and sends a Slack incoming-webhook diagnostic rather than trying another revision. GitHub concurrency serializes demo runs; Kubernetes access should also restrict other writers.

The short window and 20% threshold are demonstration settings chosen to keep a narrated walkthrough short, not a statistically established production SLO. A real rollout needs representative routes, workload-driven sample sizes and longer observation. This synthetic probe only measures the frontend route, not real-user error rate or backend health. An API/probe outage fails closed but does not prove an application regression. Database migrations are not reverted.

## Fixtures and workflow

Run `05 - Verified release and bounded rollback` manually on `main` with one scenario:

| Scenario | Baseline route | Candidate route | Expected result |
|---|---|---|---|
| `healthy` | HTTP 200 | HTTP 200 | Candidate retained, job green |
| `rollback` | HTTP 200 | HTTP 500 | One undo, restored route HTTP 200, job red |
| `escalate` | HTTP 500 | HTTP 500 | One undo, restored route still HTTP 500, Slack escalation, job red |

Setup seeds the baseline; it is deliberately faulty for `escalate`. Readiness probes call `/healthz`, which returns 200 in both fixtures. The business-route probe calls `/`, which returns the real KoalaTech frontend in the healthy image and an injected HTTP 500 in the faulty image. This makes a readiness-passing regression reproducible. The fixture-specific Nginx configuration omits backend proxy routes so this service can run independently. It is not a full-stack acceptance test.

Each run has its own `hd-<run-id>-<attempt>` namespace. The Deployment retains five ReplicaSet revisions. Both images are addressed by SHA-256 digest; the baseline and candidate include distinct release identifiers so Kubernetes creates a new revision. The controller has no scenario switch: all three cases use the same decision path.

## Reproduce

Prerequisites: Azure CLI login with permission to create AKS/ACR and role assignments, Terraform 1.7+, Python 3.12+, kubectl, and a GitHub OIDC identity trusted for the repository's main branch. Existing 10.2D OIDC variables are reused. Terraform gives that identity Contributor on the new demo resource group only; change its principal ID when using another identity. Store the Slack incoming webhook in the repository Actions secret `SLACK_WEBHOOK_URL`; never commit its value.

```powershell
$env:ARM_SUBSCRIPTION_ID = (az account show --query id -o tsv)
terraform -chdir=ops/infra init
terraform -chdir=ops/infra validate
terraform -chdir=ops/infra plan -out=demo.tfplan
terraform -chdir=ops/infra apply demo.tfplan
python -m unittest discover -s ops -p 'test_rollback.py' -v
```

The workflow uses `sit722-103hd-rg`, `sit722-103hd-aks`, and `sit722103hd2304184.azurecr.io`; change the workflow's resource settings together with Terraform if reproducing in another subscription. The fixture creates a single small node and uses private ClusterIP access without a public application LoadBalancer.

Artifacts named `rollback-<scenario>-<run-id>-<attempt>` retain individual JSONL samples, `decision.json`, immutable image references, pod state and rollout history for 30 days, even for rejected releases. Download them before that retention period expires. Terraform state remains local, contains sensitive infrastructure data, and is ignored by git. Do not upload state or kubeconfig with the evidence.

After recording and downloading evidence, remove only these demo resources:

```powershell
terraform -chdir=ops/infra plan -destroy -out=destroy.tfplan
terraform -chdir=ops/infra apply destroy.tfplan
```

Terraform also removes its demo-scope identity role assignment. Revoking the exposed demonstration webhook is also recommended after the exercise.

## References

- Kubernetes, Deployments: https://kubernetes.io/docs/concepts/workloads/controllers/deployment/ (revision history, `rollout undo --to-revision`, and rollout progress reporting).
- Slack, Sending messages using incoming webhooks: https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks/ (JSON messages, secret URL handling and HTTP errors).
