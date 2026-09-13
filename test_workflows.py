from pathlib import Path
import re


ROOT = Path(__file__).resolve().parent
WORKFLOWS = ROOT / ".github" / "workflows"


def require(text: str, fragment: str) -> None:
    assert fragment in text, f"missing: {fragment}"


def main() -> None:
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

    print("workflow handoff checks passed")


if __name__ == "__main__":
    main()
