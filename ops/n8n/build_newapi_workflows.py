#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path


OUTPUT = Path(__file__).with_name("newapi-workflows.json")
SSH_CREDENTIAL = {
    "sshPrivateKey": {
        "id": "RTOCNewAPIDeploySSH001",
        "name": "RTOC NewAPI Deploy SSH",
    }
}
GITHUB_CREDENTIAL = {
    "httpHeaderAuth": {
        "id": "RTOCNewAPIGitHubActions001",
        "name": "RTOC NewAPI GitHub Actions",
    }
}


def node(node_id, name, node_type, version, position, parameters, credentials=None):
    result = {
        "parameters": parameters,
        "id": node_id,
        "name": name,
        "type": node_type,
        "typeVersion": version,
        "position": list(position),
    }
    if credentials:
        result["credentials"] = credentials
    return result


def manual(node_id, position):
    return node(
        node_id,
        "Manual Trigger",
        "n8n-nodes-base.manualTrigger",
        1,
        position,
        {},
    )


def set_raw(node_id, name, position, value):
    return node(
        node_id,
        name,
        "n8n-nodes-base.set",
        3.4,
        position,
        {
            "mode": "raw",
            "jsonOutput": json.dumps(value, separators=(",", ":")),
            "options": {},
        },
    )


def code(node_id, name, position, source):
    return node(
        node_id,
        name,
        "n8n-nodes-base.code",
        2,
        position,
        {
            "mode": "runOnceForAllItems",
            "language": "javaScript",
            "jsCode": source.strip(),
        },
    )


def ssh(node_id, name, position, command):
    return node(
        node_id,
        name,
        "n8n-nodes-base.ssh",
        1,
        position,
        {
            "authentication": "privateKey",
            "resource": "command",
            "operation": "execute",
            "command": command,
            "cwd": "/",
        },
        SSH_CREDENTIAL,
    )


def connect_linear(nodes):
    connections = {}
    for current, following in zip(nodes, nodes[1:]):
        connections[current["name"]] = {
            "main": [
                [
                    {
                        "node": following["name"],
                        "type": "main",
                        "index": 0,
                    }
                ]
            ]
        }
    return connections


def workflow(workflow_id, version_id, name, nodes):
    return {
        "id": workflow_id,
        "name": name,
        "active": False,
        "nodes": nodes,
        "connections": connect_linear(nodes),
        "settings": {
            "executionOrder": "v1",
            "saveExecutionProgress": True,
            "saveManualExecutions": True,
            "callerPolicy": "workflowsFromSameOwner",
        },
        "staticData": None,
        "meta": None,
        "pinData": None,
        "versionId": version_id,
        "tags": [],
    }


def status_workflow():
    nodes = [
        manual("newapi-v2-status-trigger", (0, 0)),
        ssh("newapi-v2-status-ssh", "SSH: NewAPI Status", (280, 0), "status"),
    ]
    return workflow(
        "RTOCNewAPIStatusV2",
        "9bbcc1c7-e480-4db6-9e04-b646ff22f792",
        "RTOC New API / 00 Status",
        nodes,
    )


def build_and_prepare_workflow():
    validate_request = r"""
const v = $input.first().json;
if (v.confirm !== 'PREPARE') throw new Error('confirm must be PREPARE');
if (!/^[0-9a-f]{40}$/.test(v.commitSha)) {
  throw new Error('commitSha must be an exact 40-character lowercase SHA');
}
const now = Date.now();
const execution = String($execution.id).replace(/[^A-Za-z0-9._-]/g, '-');
const buildRequestId = `newapi-${v.commitSha.slice(0, 12)}-${execution}-${now}`;
const releaseId = `newapi-${v.commitSha.slice(0, 12)}-${now}`;
const resumeUrl = $execution.resumeUrl;
const queryIndex = resumeUrl.indexOf('?');
const suffix = '/newapi-build-result';
const callbackUrl = queryIndex === -1
  ? `${resumeUrl}${suffix}`
  : `${resumeUrl.slice(0, queryIndex)}${suffix}${resumeUrl.slice(queryIndex)}`;
return [{
  json: {
    commitSha: v.commitSha,
    buildRequestId,
    releaseId,
    callbackUrl,
    requestedAt: new Date(now).toISOString(),
  },
}];
"""
    validate_manifest = r"""
const request = $('Validate Build Request').first().json;
const callback = $input.first().json;
const manifest = callback.body ?? callback;
if (manifest.status === 'failed') {
  if (manifest.build_request_id !== request.buildRequestId) {
    throw new Error('failed build_request_id does not match this execution');
  }
  if (manifest.commit_sha !== request.commitSha) {
    throw new Error('failed commit_sha does not match the requested commit');
  }
  throw new Error(
    `GitHub build failed in run ${manifest.workflow_run_id}, attempt ${manifest.workflow_attempt}`,
  );
}
if (manifest.schema_version !== 1) throw new Error('invalid schema_version');
if (manifest.service !== 'newapi') throw new Error('invalid service');
if (manifest.build_request_id !== request.buildRequestId) {
  throw new Error('build_request_id does not match this execution');
}
if (manifest.commit_sha !== request.commitSha) {
  throw new Error('commit_sha does not match the requested commit');
}
if (manifest.platform !== 'linux/amd64') throw new Error('invalid platform');
if (manifest.tests !== 'passed') throw new Error('tests did not pass');
if (manifest.image !== 'ghcr.io/hechuyi/new-api-rtoc') {
  throw new Error('unexpected image repository');
}
if (!/^sha256:[0-9a-f]{64}$/.test(manifest.image_digest)) {
  throw new Error('invalid image_digest');
}
if (!/^sha256:[0-9a-f]{64}$/.test(manifest.image_id)) {
  throw new Error('invalid image_id');
}
if (manifest.image_id !== manifest.image_digest) {
  throw new Error('image_id must match image_digest');
}
if (!/^sha256:[0-9a-f]{64}$/.test(manifest.image_config_digest)) {
  throw new Error('invalid image_config_digest');
}
if (!Number.isSafeInteger(manifest.workflow_run_id) || manifest.workflow_run_id < 1) {
  throw new Error('invalid workflow_run_id');
}
if (!Number.isSafeInteger(manifest.workflow_attempt) || manifest.workflow_attempt < 1) {
  throw new Error('invalid workflow_attempt');
}
const immutableImage = `${manifest.image}@${manifest.image_digest}`;
const command = `prepare ${immutableImage} ${manifest.image_id} ${manifest.commit_sha} ${request.releaseId}`;
return [{ json: { ...request, manifest, immutableImage, command } }];
"""
    verify_prepared = r"""
const beforeResult = $('SSH: Status Before Build').first().json;
const prepareResult = $('SSH: Prepare Candidate').first().json;
const statusResult = $input.first().json;
if (beforeResult.code !== 0) {
  throw new Error(`status before build failed: ${beforeResult.stderr}`);
}
if (prepareResult.code !== 0) {
  throw new Error(`candidate prepare failed: ${prepareResult.stderr}`);
}
if (statusResult.code !== 0) {
  throw new Error(`status after prepare failed: ${statusResult.stderr}`);
}
const before = JSON.parse(beforeResult.stdout);
const prepare = JSON.parse(prepareResult.stdout);
const after = JSON.parse(statusResult.stdout);
const release = $('Validate Release Manifest').first().json;
if (!before.ok || !prepare.ok || !after.ok) throw new Error('deployment command failed');
if (before.active_slot !== after.active_slot) {
  throw new Error('active slot changed during prepare');
}
for (const slot of ['a', 'b']) {
  if (before.slots[slot].weight !== after.slots[slot].weight) {
    throw new Error(`slot ${slot} weight changed during prepare`);
  }
}
if (after.prepared_release !== release.releaseId) {
  throw new Error('prepared release does not match requested release');
}
if (after.prepared_slot !== before.inactive_slot) {
  throw new Error('candidate was not prepared in the inactive slot');
}
return [{
  json: {
    ok: true,
    state: 'AWAITING_APPROVAL',
    release: release.releaseId,
    commit_sha: release.commitSha,
    image: release.immutableImage,
    image_digest: release.manifest.image_digest,
    image_id: prepare.image_id,
    image_config_digest: release.manifest.image_config_digest,
    workflow_run_id: release.manifest.workflow_run_id,
    active_slot: after.active_slot,
    prepared_slot: after.prepared_slot,
    weights: {
      a: after.slots.a.weight,
      b: after.slots.b.weight,
    },
  },
}];
"""
    nodes = [
        manual("newapi-v2-build-trigger", (0, 0)),
        set_raw(
            "newapi-v2-build-parameters",
            "Build Parameters",
            (260, 0),
            {"confirm": "CHANGE_ME", "commitSha": "CHANGE_ME_40_HEX"},
        ),
        code(
            "newapi-v2-build-validate",
            "Validate Build Request",
            (520, 0),
            validate_request,
        ),
        ssh(
            "newapi-v2-build-status-before",
            "SSH: Status Before Build",
            (800, 0),
            "status",
        ),
        node(
            "newapi-v2-build-dispatch",
            "GitHub: Dispatch Exact Build",
            "n8n-nodes-base.httpRequest",
            4.4,
            (1080, 0),
            {
                "method": "POST",
                "url": (
                    "https://api.github.com/repos/hechuyi/new-api-rtoc/"
                    "actions/workflows/rtoc-backend-image.yml/dispatches"
                ),
                "authentication": "genericCredentialType",
                "genericAuthType": "httpHeaderAuth",
                "sendHeaders": True,
                "specifyHeaders": "keypair",
                "headerParameters": {
                    "parameters": [
                        {
                            "name": "Accept",
                            "value": "application/vnd.github+json",
                        },
                        {
                            "name": "X-GitHub-Api-Version",
                            "value": "2022-11-28",
                        },
                    ]
                },
                "sendBody": True,
                "contentType": "json",
                "specifyBody": "json",
                "jsonBody": (
                    "={{ { ref: 'main', inputs: { "
                    "commit_sha: $('Validate Build Request').first().json.commitSha, "
                    "build_request_id: $('Validate Build Request').first().json.buildRequestId, "
                    "callback_url: $('Validate Build Request').first().json.callbackUrl "
                    "} } }}"
                ),
                "options": {
                    "response": {
                        "response": {
                            "fullResponse": True,
                            "responseFormat": "text",
                            "outputPropertyName": "body",
                        }
                    },
                    "timeout": 30000,
                },
            },
            GITHUB_CREDENTIAL,
        ),
        node(
            "newapi-v2-build-wait",
            "Wait for Trusted Build Callback",
            "n8n-nodes-base.wait",
            1.1,
            (1360, 0),
            {
                "resume": "webhook",
                "incomingAuthentication": "none",
                "httpMethod": "POST",
                "responseCode": 202,
                "responseMode": "onReceived",
                "limitWaitTime": True,
                "limitType": "afterTimeInterval",
                "resumeAmount": 45,
                "resumeUnit": "minutes",
                "options": {
                    "noResponseBody": True,
                    "webhookSuffix": "newapi-build-result",
                },
            },
        ),
        code(
            "newapi-v2-build-manifest",
            "Validate Release Manifest",
            (1640, 0),
            validate_manifest,
        ),
        ssh(
            "newapi-v2-build-prepare",
            "SSH: Prepare Candidate",
            (1920, 0),
            "={{ $json.command }}",
        ),
        ssh(
            "newapi-v2-build-status-after",
            "SSH: Status After Prepare",
            (2200, 0),
            "status",
        ),
        code(
            "newapi-v2-build-verify",
            "Verify Candidate Prepared",
            (2480, 0),
            verify_prepared,
        ),
    ]
    return workflow(
        "RTOCNewAPIBuildV2",
        "21440f14-2376-4733-84cd-8f11831c5d8d",
        "RTOC New API / 01 Build and Prepare",
        nodes,
    )


def cutover_workflow():
    validate_cutover = r"""
const v = $('Cutover Parameters').first().json;
if (v.confirm !== 'CUTOVER') throw new Error('confirm must be CUTOVER');
if (!/^[A-Za-z0-9._:@/+%-]+$/.test(v.release)) {
  throw new Error('invalid release identifier');
}
const status = JSON.parse($input.first().json.stdout);
if (!status.ok) throw new Error('status failed');
if (status.prepared_release !== v.release) {
  throw new Error('prepared_release does not match requested cutover');
}
if (!['a', 'b'].includes(status.prepared_slot)) {
  throw new Error('prepared slot is missing');
}
const release = v.release;
return [{ json: { command: `cutover ${release}`, release, before: status } }];
"""
    verify_cutover = r"""
const request = $('Validate Cutover Request').first().json;
const result = JSON.parse($('SSH: Cut Over Candidate').first().json.stdout);
const after = JSON.parse($input.first().json.stdout);
if (!result.ok || !after.ok) throw new Error('cutover command failed');
if (after.active_slot !== request.before.prepared_slot) {
  throw new Error('prepared slot did not become active');
}
if (after.active_slot === request.before.active_slot) {
  throw new Error('active slot did not change');
}
if (after.slots[after.active_slot].weight !== 100) {
  throw new Error('new active slot weight is not 100');
}
if (after.slots[request.before.active_slot].weight !== 0) {
  throw new Error('previous active slot weight is not 0');
}
return [{
  json: {
    ok: true,
    state: 'ACTIVE',
    release: request.release,
    active_slot: after.active_slot,
    previous_slot: request.before.active_slot,
    weights: {
      a: after.slots.a.weight,
      b: after.slots.b.weight,
    },
  },
}];
"""
    nodes = [
        manual("newapi-v2-cutover-trigger", (0, 0)),
        set_raw(
            "newapi-v2-cutover-parameters",
            "Cutover Parameters",
            (260, 0),
            {"confirm": "CHANGE_ME", "release": "CHANGE_ME"},
        ),
        ssh(
            "newapi-v2-cutover-status-before",
            "SSH: Status Before Cutover",
            (520, 0),
            "status",
        ),
        code(
            "newapi-v2-cutover-validate",
            "Validate Cutover Request",
            (800, 0),
            validate_cutover,
        ),
        ssh(
            "newapi-v2-cutover-command",
            "SSH: Cut Over Candidate",
            (1080, 0),
            "={{ $json.command }}",
        ),
        ssh(
            "newapi-v2-cutover-status-after",
            "SSH: Status After Cutover",
            (1360, 0),
            "status",
        ),
        code(
            "newapi-v2-cutover-verify",
            "Verify Cutover",
            (1640, 0),
            verify_cutover,
        ),
    ]
    return workflow(
        "RTOCNewAPICutoverV2",
        "dc2e671a-ff63-4458-b6bc-f93d796e861a",
        "RTOC New API / 02 Cut Over Prepared Candidate",
        nodes,
    )


def rollback_workflow():
    validate_rollback = r"""
const v = $input.first().json;
if (v.confirm !== 'ROLLBACK') throw new Error('confirm must be ROLLBACK');
return [{ json: { command: 'rollback' } }];
"""
    verify_rollback = r"""
const before = JSON.parse($('SSH: Status Before Rollback').first().json.stdout);
const result = JSON.parse($('SSH: Roll Back').first().json.stdout);
const after = JSON.parse($input.first().json.stdout);
if (!before.ok || !result.ok || !after.ok) throw new Error('rollback command failed');
if (before.active_slot === after.active_slot) {
  throw new Error('active slot did not change during rollback');
}
if (after.slots[after.active_slot].weight !== 100) {
  throw new Error('rollback target weight is not 100');
}
if (after.slots[before.active_slot].weight !== 0) {
  throw new Error('rolled back slot weight is not 0');
}
return [{
  json: {
    ok: true,
    state: 'ROLLED_BACK',
    active_slot: after.active_slot,
    previous_slot: before.active_slot,
    weights: {
      a: after.slots.a.weight,
      b: after.slots.b.weight,
    },
  },
}];
"""
    nodes = [
        manual("newapi-v2-rollback-trigger", (0, 0)),
        set_raw(
            "newapi-v2-rollback-parameters",
            "Rollback Parameters",
            (260, 0),
            {"confirm": "CHANGE_ME"},
        ),
        code(
            "newapi-v2-rollback-validate",
            "Validate Rollback Request",
            (520, 0),
            validate_rollback,
        ),
        ssh(
            "newapi-v2-rollback-status-before",
            "SSH: Status Before Rollback",
            (800, 0),
            "status",
        ),
        ssh(
            "newapi-v2-rollback-command",
            "SSH: Roll Back",
            (1080, 0),
            "rollback",
        ),
        ssh(
            "newapi-v2-rollback-status-after",
            "SSH: Status After Rollback",
            (1360, 0),
            "status",
        ),
        code(
            "newapi-v2-rollback-verify",
            "Verify Rollback",
            (1640, 0),
            verify_rollback,
        ),
    ]
    return workflow(
        "RTOCNewAPIRollbackV2",
        "605e6b64-0dc0-40cb-9ab5-71733362d0f1",
        "RTOC New API / 03 Roll Back",
        nodes,
    )


def build_workflows():
    return [
        status_workflow(),
        build_and_prepare_workflow(),
        cutover_workflow(),
        rollback_workflow(),
    ]


def render():
    return json.dumps(
        build_workflows(),
        ensure_ascii=True,
        indent=2,
        sort_keys=True,
    ) + "\n"


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--stdout", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args()
    generated = render()
    if args.stdout:
        sys.stdout.write(generated)
        return 0
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text() != generated:
            print(f"{OUTPUT} is not current", file=sys.stderr)
            return 1
        return 0
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(generated)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
