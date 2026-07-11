import json
import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BUILDER = REPO_ROOT / "ops/n8n/build_newapi_workflows.py"
WORKFLOWS = REPO_ROOT / "ops/n8n/newapi-workflows.json"


class NewApiN8nWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = subprocess.run(
            ["python3", str(BUILDER), "--stdout"],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
        )
        if result.returncode != 0:
            raise AssertionError(result.stderr)
        cls.workflows = json.loads(result.stdout)
        cls.by_name = {workflow["name"]: workflow for workflow in cls.workflows}

    def test_defines_exactly_four_disabled_operator_workflows(self):
        self.assertEqual(
            set(self.by_name),
            {
                "RTOC New API / 00 Status",
                "RTOC New API / 01 Build and Prepare",
                "RTOC New API / 02 Cut Over Prepared Candidate",
                "RTOC New API / 03 Roll Back",
            },
        )
        self.assertTrue(all(not workflow["active"] for workflow in self.workflows))
        self.assertTrue(
            all(
                workflow["settings"]["executionOrder"] == "v1"
                for workflow in self.workflows
            )
        )

    def test_all_workflows_are_manual_and_have_linear_connections(self):
        for workflow in self.workflows:
            triggers = [
                node
                for node in workflow["nodes"]
                if node["type"].endswith("Trigger")
            ]
            self.assertEqual(len(triggers), 1, workflow["name"])
            self.assertEqual(
                triggers[0]["type"],
                "n8n-nodes-base.manualTrigger",
                workflow["name"],
            )
            for source in workflow["connections"].values():
                outputs = source["main"]
                self.assertEqual(len(outputs), 1, workflow["name"])
                self.assertLessEqual(len(outputs[0]), 1, workflow["name"])

    def test_ssh_nodes_use_only_the_newapi_forced_command_credential(self):
        for workflow in self.workflows:
            ssh_nodes = [
                node
                for node in workflow["nodes"]
                if node["type"] == "n8n-nodes-base.ssh"
            ]
            self.assertTrue(ssh_nodes, workflow["name"])
            for node in ssh_nodes:
                credential = node["credentials"]["sshPrivateKey"]
                self.assertEqual(credential["id"], "RTOCNewAPIDeploySSH001")
                self.assertEqual(credential["name"], "RTOC NewAPI Deploy SSH")
                self.assertEqual(node["parameters"]["cwd"], "/")

    def test_status_invokes_only_the_status_command(self):
        workflow = self.by_name["RTOC New API / 00 Status"]
        ssh_nodes = [
            node
            for node in workflow["nodes"]
            if node["type"] == "n8n-nodes-base.ssh"
        ]
        self.assertEqual(len(ssh_nodes), 1)
        self.assertEqual(ssh_nodes[0]["parameters"]["command"], "status")

    def test_build_and_prepare_validates_and_correlates_release_manifest(self):
        workflow = self.by_name["RTOC New API / 01 Build and Prepare"]
        nodes = {node["name"]: node for node in workflow["nodes"]}

        request_code = nodes["Validate Build Request"]["parameters"]["jsCode"]
        self.assertIn("confirm !== 'PREPARE'", request_code)
        self.assertIn("/^[0-9a-f]{40}$/", request_code)
        self.assertIn("buildRequestId", request_code)
        self.assertIn("releaseId", request_code)
        self.assertIn("newapi-build-result", request_code)
        self.assertIn("callbackUrl", request_code)

        dispatch = nodes["GitHub: Dispatch Exact Build"]
        self.assertEqual(dispatch["parameters"]["method"], "POST")
        self.assertIn(
            "/actions/workflows/rtoc-backend-image.yml/dispatches",
            dispatch["parameters"]["url"],
        )
        self.assertEqual(
            dispatch["credentials"]["httpHeaderAuth"]["id"],
            "RTOCNewAPIGitHubActions001",
        )
        body = dispatch["parameters"]["jsonBody"]
        self.assertIn("commit_sha", body)
        self.assertIn("build_request_id", body)
        self.assertIn("callbackUrl", body)

        wait = nodes["Wait for Trusted Build Callback"]
        self.assertEqual(wait["type"], "n8n-nodes-base.wait")
        self.assertEqual(wait["parameters"]["resume"], "webhook")
        self.assertEqual(wait["parameters"]["httpMethod"], "POST")
        self.assertTrue(wait["parameters"]["limitWaitTime"])

        manifest_code = nodes["Validate Release Manifest"]["parameters"]["jsCode"]
        self.assertIn("manifest.status === 'failed'", manifest_code)
        for required in (
            "schema_version",
            "build_request_id",
            "commit_sha",
            "image_digest",
            "image_id",
            "image_config_digest",
            "linux/amd64",
            "tests",
            "workflow_run_id",
        ):
            self.assertIn(required, manifest_code)
        self.assertIn("prepare ${immutableImage}", manifest_code)
        verify_code = nodes["Verify Candidate Prepared"]["parameters"]["jsCode"]
        self.assertIn("prepare.image_id", verify_code)
        self.assertIn("image_config_digest", verify_code)
        self.assertIn("statusResult.code !== 0", verify_code)

        self.assertLess(
            self._node_index(workflow, "SSH: Status Before Build"),
            self._node_index(workflow, "GitHub: Dispatch Exact Build"),
        )
        self.assertLess(
            self._node_index(workflow, "Validate Release Manifest"),
            self._node_index(workflow, "SSH: Prepare Candidate"),
        )
        self.assertLess(
            self._node_index(workflow, "SSH: Prepare Candidate"),
            self._node_index(workflow, "SSH: Status After Prepare"),
        )

    def test_cutover_and_rollback_require_explicit_confirmation(self):
        cutover = self.by_name[
            "RTOC New API / 02 Cut Over Prepared Candidate"
        ]
        cutover_code = self._node(cutover, "Validate Cutover Request")[
            "parameters"
        ]["jsCode"]
        self.assertIn("confirm !== 'CUTOVER'", cutover_code)
        self.assertIn("prepared_release", cutover_code)
        self.assertIn("cutover ${release}", cutover_code)
        self.assertLess(
            self._node_index(cutover, "SSH: Status Before Cutover"),
            self._node_index(cutover, "SSH: Cut Over Candidate"),
        )

        rollback = self.by_name["RTOC New API / 03 Roll Back"]
        rollback_code = self._node(rollback, "Validate Rollback Request")[
            "parameters"
        ]["jsCode"]
        self.assertIn("confirm !== 'ROLLBACK'", rollback_code)
        self.assertLess(
            self._node_index(rollback, "SSH: Status Before Rollback"),
            self._node_index(rollback, "SSH: Roll Back"),
        )

    def test_generated_file_is_current_and_contains_no_excluded_changes(self):
        generated = json.dumps(
            self.workflows,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        ) + "\n"
        self.assertEqual(WORKFLOWS.read_text(), generated)
        lowered = generated.lower()
        self.assertNotIn("nginx", lowered)
        self.assertNotIn("utls", lowered)
        self.assertNotIn("new-api-runtime-proxy", lowered)

    @staticmethod
    def _node(workflow, name):
        return next(node for node in workflow["nodes"] if node["name"] == name)

    @classmethod
    def _node_index(cls, workflow, name):
        order = cls._connection_order(workflow)
        return order.index(name)

    @staticmethod
    def _connection_order(workflow):
        node_names = {node["name"] for node in workflow["nodes"]}
        targets = {
            target["node"]
            for connection in workflow["connections"].values()
            for output in connection["main"]
            for target in output
        }
        starts = node_names - targets
        if len(starts) != 1:
            raise AssertionError(f"expected one start node: {starts}")
        order = []
        current = starts.pop()
        while current:
            order.append(current)
            outputs = workflow["connections"].get(current, {}).get("main", [[]])
            current = outputs[0][0]["node"] if outputs and outputs[0] else None
        if set(order) != node_names:
            raise AssertionError("workflow is not one linear chain")
        return order


if __name__ == "__main__":
    unittest.main()
