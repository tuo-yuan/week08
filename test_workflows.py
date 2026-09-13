from pathlib import Path
import re


ROOT = Path(__file__).resolve().parent
WORKFLOWS = ROOT / ".github" / "workflows"


def require(text: str, fragment: str) -> None:
    assert fragment in text, f"missing: {fragment}"


def main() -> None:
    ci = (WORKFLOWS / "01-ci.yml").read_text()
    staging = (WORKFLOWS / "02-deploy-staging.yml").read_text()
    test = (WORKFLOWS / "03-staging-test.yml").read_text()
    production = (WORKFLOWS / "04-deploy-production.yml").read_text()

    for workflow, upstream in ((staging, "01 - CI"), (test, "02 - Deploy to Staging"), (production, "03 - Test Staging")):
        require(workflow, f'- "{upstream}"')
        require(workflow, "types:\n      - completed")
        require(workflow, "branches:\n      - main")
        require(workflow, "github.event.workflow_run.conclusion == 'success'")
        require(workflow, "contents: read")
        require(workflow, "actions: read")

    require(production, "github.event.workflow_run.head_repository.full_name == github.repository")
    require(production, "cancel-in-progress: false")
    assert "workflow_dispatch" not in production
    assert "inputs.image_tag" not in production

    require(staging, "uses: actions/upload-artifact@v7")
    require(staging, "name: staged-release")
    require(staging, "printf '%s\\n' \"$IMAGE_SHA\" > image-tag.txt")
    require(test, "uses: actions/download-artifact@v8")
    require(test, "name: staged-release")
    require(test, "uses: actions/upload-artifact@v7")
    require(test, "name: tested-release")
    require(test, "path: release/image-tag.txt")
    require(production, "uses: actions/download-artifact@v8")
    require(production, "name: tested-release")
    require(production, "path: release")

    for workflow, artifact in ((test, "staged-release"), (production, "tested-release")):
        download_block = (
            "uses: actions/download-artifact@v8\n"
            "        with:\n"
            f"          name: {artifact}\n"
            "          path: release\n"
            "          repository: ${{ github.repository }}\n"
            "          run-id: ${{ github.event.workflow_run.id }}\n"
            "          github-token: ${{ github.token }}"
        )
        require(workflow, download_block)

    sha_pattern = r"=~ \^\[0-9a-f\]\{40\}\$"
    for workflow in (staging, test, production):
        assert re.search(sha_pattern, workflow), "missing lowercase 40-character SHA validation"

    assert "github.event.workflow_run.head_sha" in staging
    assert "github.event.workflow_run.head_sha" not in test
    assert "github.event.workflow_run.head_sha" not in production

    assert test.index("Validate staged image SHA") < test.index("Login to Azure")
    assert production.index("Validate tested image SHA before checkout") < production.index("Checkout tested commit")
    require(staging, "ref: ${{ steps.image.outputs.image_sha }}")
    require(production, "ref: ${{ steps.release.outputs.image_sha }}")
    require(test, "echo \"image_sha=$image_sha\" >> \"$GITHUB_OUTPUT\"")
    require(production, "echo \"image_sha=$image_sha\" >> \"$GITHUB_OUTPUT\"")

    services = ("frontend", "user-service", "student-service", "lecturer-service", "course-service", "enrollment-service")
    for service in services:
        require(staging, f"kubectl set image deployment/{service}")
        require(staging, f"koalatech-{service}:${{IMAGE_SHA}}")
        require(production, f"kubectl set image deployment/{service}")
        require(production, f"koalatech-{service}:${{IMAGE_SHA}}")
        require(test, f"            {service}\n")

    require(test, 'expected_image="${ACR_LOGIN_SERVER}/koalatech-${deployment}:${EXPECTED_IMAGE_SHA}"')
    assert staging.index("Upload staged release") > staging.index("Wait for enrollment-service rollout")
    assert test.index("Upload tested release") > test.index("Test frontend")

    # 10.2D: every Azure workflow uses short-lived OIDC rather than a client secret.
    for workflow in (ci, staging, test, production):
        require(workflow, "id-token: write")
        require(workflow, "contents: read")
        require(workflow, "actions: read")
        require(workflow, "uses: azure/login@v3")
        require(workflow, "client-id: ${{ vars.AZURE_CLIENT_ID_102D }}")
        require(workflow, "tenant-id: ${{ vars.AZURE_TENANT_ID_102D }}")
        require(workflow, "subscription-id: ${{ vars.AZURE_SUBSCRIPTION_ID_102D }}")
        assert "AZURE_CREDENTIALS" not in workflow

    require(ci, "uses: actions/checkout@v5")
    require(ci, "uses: actions/setup-python@v6")
    require(staging, "uses: actions/checkout@v5")
    require(production, "uses: actions/checkout@v5")

    # Infrastructure must succeed before any image can be built and pushed.
    assert ci.index("  infrastructure:") < ci.index("  build-and-push:")
    build_job = ci[ci.index("  build-and-push:"):]
    require(build_job, "      - infrastructure")
    require(ci, "uses: hashicorp/setup-terraform@v3")
    require(ci, "terraform_version: 1.15.8")
    require(ci, "terraform_wrapper: false")
    require(ci, "ARM_USE_OIDC: \"true\"")
    require(ci, "TF_VAR_environment: 10.2D")
    require(ci, "storage_account_name=sit722102dtf2304184")
    require(ci, "key=week08-102d.tfstate")
    require(ci, "use_oidc=true")
    require(ci, "terraform state show azurerm_resource_group.rg")
    require(ci, '"/subscriptions/${ARM_SUBSCRIPTION_ID}/resourceGroups/sit722-102d-rg"')
    require(ci, "terraform plan -no-color -out=tfplan")
    require(ci, "terraform apply -no-color -auto-approve tfplan")
    assert "path: terraform/reports/*.txt" in ci
    assert "path: terraform/tfplan" not in ci and "path: terraform/terraform.tfstate" not in ci

    # Scout scans the local image before push; only python-multipart is a mandatory gate.
    assert ci.index("Build Docker image") < ci.index("Record full advisory Scout findings")
    assert ci.index("Record full advisory Scout findings") < ci.index("Enforce python-multipart remediation")
    assert ci.index("Enforce python-multipart remediation") < ci.index("Push Docker image with commit SHA")
    require(ci, "docker-scout_1.24.0_linux_amd64.tar.gz")
    require(ci, "docker login --username")
    require(ci, "--password-stdin")
    require(ci, "8aa54a49df760324b210703b9edbc37bd1c6f82c")
    require(ci, "git archive 8aa54a49df760324b210703b9edbc37bd1c6f82c user-service")
    require(ci, "docker scout cves --only-package python-multipart baseline-user-service:8aa54a4")
    require(ci, 'docker scout cves "$IMAGE"')
    require(ci, 'docker scout recommendations "$IMAGE"')
    require(ci, 'docker scout cves --exit-code --only-package python-multipart "$IMAGE"')
    for service in ("user-service", "student-service", "lecturer-service"):
        service_block = ci[ci.index(f"          - service: {service}"):]
        require(service_block, "multipart-gate: true")
    require(ci, "remaining critical/high findings are NOT cleared")
    require(ci, '$HOME/.docker/cli-plugins')
    require(ci, 'sha256sum --check')
    require(ci, 'docker scout policy "$IMAGE"')
    require(ci, 'shell: bash')
    require(ci, "Production security requires a broad severity gate")

    # Only Kubernetes objects are applied; Helm consumes values.yaml.
    require(staging, "--version 90.2.0")
    require(staging, "--values kubernetes/monitoring/values.yaml")
    require(staging, "kubectl apply -f kubernetes/monitoring/user-service-monitor.yaml")
    require(staging, "kubectl apply -f kubernetes/monitoring/user-service-dashboard.yaml")
    assert "kubectl apply -f kubernetes/monitoring/values.yaml" not in staging
    assert staging.index("Apply named monitoring manifests") > staging.index("Wait for enrollment-service rollout")
    require(staging, "kubectl port-forward service/user-service 18000:http -n staging")
    require(staging, "kubectl port-forward service/prometheus-kube-prometheus-prometheus 19090:9090 -n monitoring")
    require(staging, "Prometheus did not report the staging user-service target as up")
    require(staging, "user_service_http_requests_total")
    require(staging, "user_service_http_request_duration_seconds")
    require(staging, "--timeout=600s")

    for workflow in (staging, production):
        require(workflow, "az storage account show-connection-string")
        require(workflow, "echo \"::add-mask::$storage_connection\"")
        require(workflow, 'AZURE_STORAGE_CONNECTION_STRING="$AZURE_STORAGE_CONNECTION_STRING"')
        assert "secrets.AZURE_STORAGE_CONNECTION_STRING" not in workflow

    print("workflow handoff and 10.2D regression checks passed")


if __name__ == "__main__":
    main()
