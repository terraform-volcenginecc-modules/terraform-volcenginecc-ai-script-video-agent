#!/usr/bin/env python3
"""Exercise the real release barrier with local Terraform resource substitutes.

Uses an isolated temporary state and no cloud provider or credentials.
"""
from pathlib import Path
import subprocess
import tempfile

PROJECT = Path(__file__).resolve().parents[1]


def verify():
    main = (PROJECT / "main.tf").read_text()
    barrier = main.split('resource "terraform_data" "eni_release_wait" {', 1)[1]
    barrier = 'resource "terraform_data" "eni_release_wait" {' + barrier.split(
        'resource "volcenginecc_ecs_instance" "app" {', 1
    )[0]
    barrier = barrier.replace("volcenginecc_vpc_security_group.app", "terraform_data.sg")
    ecs_body = main.split('resource "volcenginecc_ecs_instance" "app" {', 1)[1]
    assert "depends_on = [terraform_data.eni_release_wait]" in ecs_body
    assert "security_group_ids = [volcenginecc_vpc_security_group.app.id]" in ecs_body
    sg_body = main.split('resource "volcenginecc_vpc_security_group" "app" {', 1)[1]
    assert "depends_on = [volcenginecc_vpc_subnet.app]" in sg_body.split(
        'resource "volcenginecc_tos_bucket"', 1
    )[0]

    def stub(name, dependency, message):
        depends = f"depends_on = [{dependency}]" if dependency else ""
        return f'''
resource "terraform_data" "{name}" {{
  {depends}
  provisioner "local-exec" {{
    when = destroy
    interpreter = ["python3", "-c"]
    command = "print('{message}', flush=True)"
  }}
}}
'''

    config = '''
terraform { required_version = ">= 1.5.7" }
variable "eni_release_wait_seconds" { default = 1 }
''' + stub("vpc", None, "VPC deleted")
    config += stub("subnet", "terraform_data.vpc", "Subnet deleted")
    config += stub("sg", "terraform_data.subnet", "Security group deleted")
    config += barrier
    config += stub("ecs", "terraform_data.eni_release_wait", "ECS deleted")
    with tempfile.TemporaryDirectory(prefix="terraform-destroy-order-") as directory:
        (Path(directory) / "main.tf").write_text(config)

        def run(*args):
            result = subprocess.run(
                ["terraform", *args, "-no-color"], cwd=directory,
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode:
                raise RuntimeError(result.stdout + result.stderr)
            return result.stdout

        run("init", "-backend=false")
        run("apply", "-auto-approve", "-input=false")
        output = run("destroy", "-auto-approve", "-input=false")
        markers = [
            "(local-exec): ECS deleted",
            "(local-exec): Waiting 1s",
            "(local-exec): Security group deleted",
            "(local-exec): Subnet deleted",
            "(local-exec): VPC deleted",
        ]
        positions = [output.index(marker) for marker in markers]
        assert positions == sorted(positions), output
        assert "Destroy complete! Resources: 5 destroyed." in output, output
    print("Default-parallelism destroy order verified: ECS -> wait -> SG -> subnet -> VPC")


if __name__ == "__main__":
    verify()
