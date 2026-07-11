import json
import os
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY = REPO_ROOT / "ops/new-api-release/rtoc-newapi-deploy"
DISPATCH = REPO_ROOT / "ops/new-api-release/rtoc-newapi-deploy-dispatch"


class NewApiReleaseTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.app = self.root / "app"
        self.state_dir = self.app / ".rtoc-release"
        self.fakebin = self.root / "bin"
        self.app.mkdir()
        self.fakebin.mkdir()
        self.compose = self.app / "docker-compose.yml"
        self.compose.write_text(
            textwrap.dedent(
                """\
                services:
                  new-api-a:
                    image: ghcr.io/hechuyi/new-api-rtoc@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
                    container_name: new-api-ai-a
                  new-api-b:
                    image: ghcr.io/hechuyi/new-api-rtoc@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
                    container_name: new-api-ai-b
                """
            )
        )
        self.runtime_file = self.root / "runtime.json"
        self.runtime_file.write_text(
            json.dumps(
                {
                    "a": {"status": "UP", "weight": 0, "sessions": 0},
                    "b": {"status": "UP", "weight": 100, "sessions": 3},
                }
            )
        )
        self.runtime_recovery_file = self.root / "runtime-recovery.json"
        self.runtime_recovery_file.write_text("{}")
        self.docker_file = self.root / "docker.json"
        self.docker_file.write_text(
            json.dumps(
                {
                    "images": {
                        "ghcr.io/hechuyi/new-api-rtoc@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa": {
                            "id": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
                            "revision": "a" * 40,
                            "platform": "linux/amd64",
                        },
                        "ghcr.io/hechuyi/new-api-rtoc@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb": {
                            "id": "sha256:2222222222222222222222222222222222222222222222222222222222222222",
                            "revision": "b" * 40,
                            "platform": "linux/amd64",
                        },
                        "ghcr.io/hechuyi/new-api-rtoc@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc": {
                            "id": "sha256:3333333333333333333333333333333333333333333333333333333333333333",
                            "revision": "c" * 40,
                            "platform": "linux/amd64",
                        },
                        "ghcr.io/hechuyi/new-api-rtoc@sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd": {
                            "id": "sha256:4444444444444444444444444444444444444444444444444444444444444444",
                            "revision": "d" * 40,
                            "platform": "linux/amd64",
                        },
                    },
                    "containers": {
                        "new-api-ai-a": {
                            "running": True,
                            "image": "ghcr.io/hechuyi/new-api-rtoc@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                            "id": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
                            "revision": "a" * 40,
                        },
                        "new-api-ai-b": {
                            "running": True,
                            "image": "ghcr.io/hechuyi/new-api-rtoc@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                            "id": "sha256:2222222222222222222222222222222222222222222222222222222222222222",
                            "revision": "b" * 40,
                        },
                    },
                }
            )
        )
        self.command_log = self.root / "commands.log"
        self._write_fake_newapi_ab()
        self._write_fake_docker()
        self._write_fake_curl()
        self._write_executable("flock", "#!/bin/sh\nexit 0\n")
        self.env = os.environ.copy()
        self.env.update(
            {
                "PATH": f"{self.fakebin}:{self.env['PATH']}",
                "NEWAPI_DEPLOY_COMPOSE_DIR": str(self.app),
                "NEWAPI_DEPLOY_COMPOSE_FILE": str(self.compose),
                "NEWAPI_DEPLOY_STATE_DIR": str(self.state_dir),
                "NEWAPI_DEPLOY_LOCK_FILE": str(self.root / "deploy.lock"),
                "NEWAPI_DEPLOY_HEALTH_ATTEMPTS": "2",
                "NEWAPI_DEPLOY_SLEEP_SECONDS": "0",
                "NEWAPI_DEPLOY_STABLE_URL": "http://127.0.0.1:3010/api/status",
                "NEWAPI_DEPLOY_PUBLIC_URL": "https://api.example.invalid/api/status",
                "FAKE_RUNTIME_FILE": str(self.runtime_file),
                "FAKE_RUNTIME_RECOVERY_FILE": str(self.runtime_recovery_file),
                "FAKE_DOCKER_FILE": str(self.docker_file),
                "FAKE_COMPOSE_FILE": str(self.compose),
                "FAKE_COMMAND_LOG": str(self.command_log),
            }
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def _write_executable(self, name, content):
        path = self.fakebin / name
        path.write_text(content)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def _write_fake_newapi_ab(self):
        self._write_executable(
            "newapi-ab",
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import json
                import os
                import sys
                from pathlib import Path

                path = Path(os.environ["FAKE_RUNTIME_FILE"])
                recovery_path = Path(os.environ["FAKE_RUNTIME_RECOVERY_FILE"])
                state = json.loads(path.read_text())
                recovery = json.loads(recovery_path.read_text())
                command = sys.argv[1]
                if command == "status":
                    for slot in ("a", "b"):
                        remaining = recovery.get(slot, 0)
                        if remaining > 0:
                            state[slot]["status"] = "DOWN"
                            recovery[slot] = remaining - 1
                        elif slot in recovery:
                            state[slot]["status"] = "UP"
                            del recovery[slot]
                    path.write_text(json.dumps(state))
                    recovery_path.write_text(json.dumps(recovery))
                    for slot in ("a", "b"):
                        item = state[slot]
                        print(
                            f"{slot} status={item['status']} weight={item['weight']} "
                            f"active_sessions={item['sessions']} checks=0"
                        )
                elif command in ("active-a", "active-b"):
                    active = command[-1]
                    state[active]["weight"] = 100
                    state["b" if active == "a" else "a"]["weight"] = 0
                    path.write_text(json.dumps(state))
                else:
                    raise SystemExit(64)
                """
            ),
        )

    def _write_fake_docker(self):
        self._write_executable(
            "docker",
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import json
                import os
                import re
                import sys
                from pathlib import Path

                state_path = Path(os.environ["FAKE_DOCKER_FILE"])
                compose_path = Path(os.environ["FAKE_COMPOSE_FILE"])
                log_path = Path(os.environ["FAKE_COMMAND_LOG"])
                runtime_path = Path(os.environ["FAKE_RUNTIME_FILE"])
                recovery_path = Path(os.environ["FAKE_RUNTIME_RECOVERY_FILE"])
                state = json.loads(state_path.read_text())
                args = sys.argv[1:]
                with log_path.open("a") as handle:
                    handle.write("docker " + " ".join(args) + "\\n")

                def save():
                    state_path.write_text(json.dumps(state))

                def compose_images():
                    result = {}
                    service = None
                    for line in compose_path.read_text().splitlines():
                        match = re.fullmatch(r"  ([A-Za-z0-9_-]+):", line)
                        if match:
                            service = match.group(1)
                        elif service and line.startswith("    image: "):
                            result[service] = line.split(":", 1)[1].strip()
                    return result

                if args[:1] == ["pull"]:
                    if args[1] not in state["images"]:
                        raise SystemExit(1)
                    print(args[1])
                elif args[:2] == ["image", "inspect"]:
                    image = args[2]
                    item = state["images"].get(image)
                    if not item:
                        raise SystemExit(1)
                    fmt = args[args.index("--format") + 1]
                    if fmt == "{{.Id}}":
                        print(item["id"])
                    elif "Architecture" in fmt:
                        print(item["platform"])
                    elif "org.opencontainers.image.revision" in fmt:
                        print(item["revision"])
                    else:
                        raise SystemExit(f"unsupported image format: {fmt}")
                elif args[:1] == ["inspect"]:
                    container = args[1]
                    item = state["containers"].get(container)
                    if not item:
                        raise SystemExit(1)
                    fmt = args[args.index("--format") + 1]
                    if fmt == "{{.State.Running}}":
                        print(str(item["running"]).lower())
                    elif fmt == "{{.Image}}":
                        print(item["id"])
                    elif fmt == "{{.Config.Image}}":
                        print(item["image"])
                    elif "org.opencontainers.image.revision" in fmt:
                        print(item["revision"])
                    else:
                        raise SystemExit(f"unsupported container format: {fmt}")
                elif args[:1] == ["compose"]:
                    service = args[-1]
                    images = compose_images()
                    image = images[service]
                    image_item = state["images"][image]
                    container = {
                        "new-api-a": "new-api-ai-a",
                        "new-api-b": "new-api-ai-b",
                    }[service]
                    state["containers"][container] = {
                        "running": True,
                        "image": image,
                        "id": image_item["id"],
                        "revision": image_item["revision"],
                    }
                    save()
                    recovery_polls = int(
                        os.environ.get("FAKE_RUNTIME_RECOVERY_POLLS", "0")
                    )
                    if recovery_polls:
                        slot = service[-1]
                        runtime = json.loads(runtime_path.read_text())
                        runtime[slot]["status"] = "DOWN"
                        runtime_path.write_text(json.dumps(runtime))
                        recovery = json.loads(recovery_path.read_text())
                        recovery[slot] = recovery_polls
                        recovery_path.write_text(json.dumps(recovery))
                else:
                    raise SystemExit("unsupported docker command: " + " ".join(args))
                """
            ),
        )

    def _write_fake_curl(self):
        self._write_executable(
            "curl",
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import json
                import os
                import sys
                from pathlib import Path

                url = sys.argv[-1]
                runtime = json.loads(Path(os.environ["FAKE_RUNTIME_FILE"]).read_text())
                docker = json.loads(Path(os.environ["FAKE_DOCKER_FILE"]).read_text())
                if os.environ.get("FAKE_PUBLIC_FAIL") == "1" and url.startswith("https://"):
                    raise SystemExit(22)
                if ":3012/" in url:
                    slot = "a"
                elif ":3013/" in url:
                    slot = "b"
                elif ":3010/" in url or url.startswith("https://"):
                    slots = [name for name in ("a", "b") if runtime[name]["weight"] == 100]
                    if len(slots) != 1:
                        raise SystemExit(22)
                    slot = slots[0]
                else:
                    raise SystemExit(22)
                container = docker["containers"][f"new-api-ai-{slot}"]
                if not container["running"] or runtime[slot]["status"] != "UP":
                    raise SystemExit(22)
                """
            ),
        )

    def run_deploy(self, *args, extra_env=None):
        env = self.env.copy()
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [str(DEPLOY), *args],
            cwd=self.app,
            env=env,
            text=True,
            capture_output=True,
        )

    def prepare_candidate(self, digest="c", release="release-c"):
        digest_value = digest * 64
        result = self.run_deploy(
            "prepare",
            f"ghcr.io/hechuyi/new-api-rtoc@sha256:{digest_value}",
            f"sha256:{digest_value}",
            digest * 40,
            release,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_dispatch_rejects_arbitrary_shell(self):
        env = self.env | {
            "SSH_ORIGINAL_COMMAND": "cd / ; status; id",
            "NEWAPI_DEPLOY_COMMAND": str(self.root / "delegate"),
        }
        result = subprocess.run(
            [str(DISPATCH)], env=env, text=True, capture_output=True
        )
        self.assertEqual(result.returncode, 64)
        self.assertIn("not allowlisted", result.stderr)

    def test_dispatch_accepts_n8n_wrapper_for_status(self):
        delegate = self.root / "delegate"
        delegate.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\"\n")
        delegate.chmod(delegate.stat().st_mode | stat.S_IXUSR)
        env = self.env | {
            "SSH_ORIGINAL_COMMAND": "cd / ; status",
            "NEWAPI_DEPLOY_COMMAND": str(delegate),
        }
        result = subprocess.run(
            [str(DISPATCH)], env=env, text=True, capture_output=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "status")

    def test_status_reports_b_active(self):
        result = self.run_deploy("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["active_slot"], "b")
        self.assertEqual(payload["inactive_slot"], "a")
        self.assertEqual(payload["slots"]["a"]["weight"], 0)
        self.assertEqual(payload["slots"]["b"]["weight"], 100)

    def test_prepare_rejects_mutable_tag(self):
        result = self.run_deploy(
            "prepare",
            "ghcr.io/hechuyi/new-api-rtoc:latest",
            "sha256:" + "3" * 64,
            "c" * 40,
            "release-c",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("immutable", result.stderr)

    def test_prepare_updates_only_inactive_slot(self):
        payload = self.prepare_candidate()
        self.assertEqual(payload["prepared_slot"], "a")
        self.assertEqual(payload["image_digest"], "sha256:" + "c" * 64)
        self.assertEqual(payload["image_id"], "sha256:" + "3" * 64)
        compose = self.compose.read_text()
        self.assertIn(
            "new-api-rtoc@sha256:" + "c" * 64,
            compose,
        )
        self.assertIn(
            "new-api-rtoc@sha256:" + "b" * 64,
            compose,
        )
        runtime = json.loads(self.runtime_file.read_text())
        self.assertEqual(runtime["a"]["weight"], 0)
        self.assertEqual(runtime["b"]["weight"], 100)
        docker = json.loads(self.docker_file.read_text())
        self.assertEqual(
            docker["containers"]["new-api-ai-a"]["id"], "sha256:" + "3" * 64
        )

    def test_prepare_waits_for_haproxy_to_observe_candidate_recovery(self):
        result = self.run_deploy(
            "prepare",
            "ghcr.io/hechuyi/new-api-rtoc@sha256:" + "c" * 64,
            "sha256:" + "c" * 64,
            "c" * 40,
            "release-c",
            extra_env={
                "FAKE_RUNTIME_RECOVERY_POLLS": "2",
                "NEWAPI_DEPLOY_HEALTH_ATTEMPTS": "4",
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        runtime = json.loads(self.runtime_file.read_text())
        self.assertEqual(runtime["a"]["status"], "UP")
        self.assertEqual(runtime["a"]["weight"], 0)
        self.assertEqual(runtime["b"]["weight"], 100)

    def test_cutover_switches_to_prepared_slot(self):
        self.prepare_candidate()
        result = self.run_deploy("cutover", "release-c")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["active_slot"], "a")
        runtime = json.loads(self.runtime_file.read_text())
        self.assertEqual(runtime["a"]["weight"], 100)
        self.assertEqual(runtime["b"]["weight"], 0)

    def test_cutover_failure_restores_previous_weights(self):
        self.prepare_candidate()
        result = self.run_deploy(
            "cutover", "release-c", extra_env={"FAKE_PUBLIC_FAIL": "1"}
        )
        self.assertNotEqual(result.returncode, 0)
        runtime = json.loads(self.runtime_file.read_text())
        self.assertEqual(runtime["a"]["weight"], 0)
        self.assertEqual(runtime["b"]["weight"], 100)

    def test_rollback_returns_to_previous_slot(self):
        self.prepare_candidate()
        cutover = self.run_deploy("cutover", "release-c")
        self.assertEqual(cutover.returncode, 0, cutover.stderr)
        result = self.run_deploy("rollback")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["active_slot"], "b")
        runtime = json.loads(self.runtime_file.read_text())
        self.assertEqual(runtime["a"]["weight"], 0)
        self.assertEqual(runtime["b"]["weight"], 100)

    def test_rollback_reconstructs_overwritten_previous_slot(self):
        self.prepare_candidate()
        cutover = self.run_deploy("cutover", "release-c")
        self.assertEqual(cutover.returncode, 0, cutover.stderr)
        self.prepare_candidate(digest="d", release="release-d")
        result = self.run_deploy("rollback")
        self.assertEqual(result.returncode, 0, result.stderr)
        docker = json.loads(self.docker_file.read_text())
        self.assertEqual(
            docker["containers"]["new-api-ai-b"]["id"], "sha256:" + "2" * 64
        )
        runtime = json.loads(self.runtime_file.read_text())
        self.assertEqual(runtime["b"]["weight"], 100)


if __name__ == "__main__":
    unittest.main()
