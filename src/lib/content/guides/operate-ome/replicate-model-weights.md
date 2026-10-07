---
title: Replicate model weights
description: "Copy model weights between Hugging Face, OCI Object Storage and PVCs with the ome-agent replica command, run as a one-shot Kubernetes Job."
---

`ome-agent replica` copies model weights from one storage location to another: seed a private OCI Object Storage mirror from Hugging Face, copy a bucket across regions or tenancies, or fill a PersistentVolumeClaim. Replication involves no custom resource and no controller: the command is a one-shot run of the `ome-agent` binary, driven by a config file, that you usually wrap in a Kubernetes Job. The steps mirror `meta-llama/Llama-3.2-1B-Instruct` from Hugging Face into a bucket, and the sections after them cover the other sources and targets.

<div class="prerequisites" markdown>

- `kubectl` access to a cluster that can run Jobs in the namespace `ome` and pull the ome-agent image, `ghcr.io/moirai-internal/ome-agent:v1.2.2`. To pull it from your own registry, see [Install from a private registry](../../getting-started/private-registries.md). The agent doesn't need OME's controller.
- An OCI Object Storage bucket `model-mirror` in a tenancy whose Object Storage namespace is `mytenancy`, and an OCI identity for the Job's pod — here, the nodes' instance principals — that can read, write, list and delete objects in it.
- A Hugging Face token with access to `meta-llama/Llama-3.2-1B-Instruct`, which is gated.

</div>

## Supported pairs and storage URIs

The config file declares the source and the target as storage URIs. The agent supports exactly six pairs; any other pair fails with `unsupported replication` before anything is copied:

| Source | Target |
| --- | --- |
| `hf://` (Hugging Face) | `oci://` (OCI Object Storage) |
| `hf://` | `pvc://` |
| `oci://` | `oci://` |
| `oci://` | `pvc://` |
| `pvc://` | `oci://` |
| `pvc://` | `pvc://` |

The URI forms are:

- `oci://n/{namespace}/b/{bucket}/o/{prefix}` — every object under the prefix. The agent appends a trailing `/` to a non-empty prefix, so `o/models/llama` doesn't also match `models/llama-2`.
- `hf://{model-id}[@{branch}]` — a Hugging Face repository, such as `hf://meta-llama/Llama-3.2-1B-Instruct`. The branch defaults to `main`. Source only.
- `pvc://{pvc-name}/{sub-path}` or `pvc://{namespace}:{pvc-name}/{sub-path}` — the sub-path is required, and must not contain `..` segments.

The agent never talks to the Kubernetes API and doesn't mount PVCs itself: a `pvc://` URI only names paths under the working directory `local_path`, and the pod spec must mount the claim there. See [PVC sides and `local_path`](#pvc-sides-and-local_path).

!!! note
    The replication agent is distinct from the [model agent](model-agent.md), the DaemonSet that downloads models onto nodes for serving. The replica command copies weights between storage systems, and nothing watches or serves what it writes until you point a model at it.

## Step 1: Write the replication config

A ConfigMap holds the agent's config file. Each side sets its `storage_uri`, and a side whose URI is `oci://` also enables its Object Storage client under `oci`:

```yaml title="replica-config.yaml"
apiVersion: v1
kind: ConfigMap
metadata:
  name: llama-replica-config
  namespace: ome
data:
  ome-agent.yaml: |
    local_path: /workspace

    source:
      storage_uri: "hf://meta-llama/Llama-3.2-1B-Instruct"

    target:
      storage_uri: "oci://n/mytenancy/b/model-mirror/o/models/meta-llama/Llama-3.2-1B-Instruct"
      checksum:
        upload_enabled: true
        algorithm: "md5"
        concurrency: 8
      oci:
        enabled: true
        auth_type: "InstancePrincipal"
        region: "us-chicago-1"
```

`local_path` is the agent's working directory: this copy downloads the full snapshot there before it uploads. The `checksum` block is optional; see [Checksums on OCI targets](#checksums-on-oci-targets). Everything else keeps its default from [Configuration](#configuration).

The token stays out of the ConfigMap. An environment variable named `OME_AGENT_` plus a key's path — uppercased, with dots turned into underscores — sets or overrides that key: `OME_AGENT_HF_TOKEN` sets `hf_token`, and `OME_AGENT_SOURCE_STORAGE_URI` overrides `source.storage_uri`. To override a key under `source.oci` or `target.oci`, such as `target.oci.obo_token`, also put the key in the config file; the variable then replaces its value.

Create the ConfigMap, and a Secret for the token:

```bash
kubectl apply -f replica-config.yaml
```

```output
configmap/llama-replica-config created
```

```bash
kubectl create secret generic hf-token -n ome --from-literal=token=hf_your_token_here
```

```output
secret/hf-token created
```

## Step 2: Run the agent as a Job

The image's entrypoint is the `ome-agent` binary, so the Job's `args` name the `replica` subcommand and its one required flag, `-c, --config`, the path to the config file; without it the run fails with `no config file provided`. `-d, --debug` enables debug mode. The `workspace` volume backs `local_path`, and must hold the full snapshot before upload:

```yaml title="replica-job.yaml"
apiVersion: batch/v1
kind: Job
metadata:
  name: replicate-llama-3-2-1b
  namespace: ome
spec:
  backoffLimit: 2
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: replica
          image: ghcr.io/moirai-internal/ome-agent:v1.2.2
          args: ["replica", "--config", "/config/ome-agent.yaml"]
          env:
            - name: OME_AGENT_HF_TOKEN
              valueFrom:
                secretKeyRef:
                  name: hf-token
                  key: token
          volumeMounts:
            - name: config
              mountPath: /config
            - name: workspace
              mountPath: /workspace
      volumes:
        - name: config
          configMap:
            name: llama-replica-config
        - name: workspace
          emptyDir:
            sizeLimit: 10Gi
```

The agent never retries a failed run on its own: the Job's `backoffLimit` schedules the retries. Apply the file:

```bash
kubectl apply -f replica-job.yaml
```

```output
job.batch/replicate-llama-3-2-1b created
```

## Step 3: Wait for the Job to complete

A pod reports success only after every file uploaded. A failed run exits with code 1, and a run interrupted before completion — a deleted pod, a drained node — fails with `replication interrupted before completion`, so the Job never records success for a partial copy. Wait for it:

```bash
kubectl wait --for=condition=complete job/replicate-llama-3-2-1b -n ome --timeout=30m
```

```output
job.batch/replicate-llama-3-2-1b condition met
```

To watch progress instead, follow the log with `kubectl logs job/replicate-llama-3-2-1b -n ome -f`: the agent logs the model's total size, then each file's upload parts, and a successful run ends with a `Replication completed from HuggingFace to OCI Object Storage for model meta-llama/Llama-3.2-1B-Instruct` message.

Check the Job's count:

```bash
kubectl get job replicate-llama-3-2-1b -n ome \
  -o custom-columns=NAME:.metadata.name,SUCCEEDED:.status.succeeded
```

```output
NAME                     SUCCEEDED
replicate-llama-3-2-1b   1
```

The weights are now under `models/meta-llama/Llama-3.2-1B-Instruct/` in the bucket, one object per snapshot file.

## Configuration

The top-level settings, with their defaults:

| Key | Default | Description |
| --- | --- | --- |
| `local_path` | — (required) | Working directory: scratch space for copies into OCI, and the mount point for PVC sides. |
| `num_connections` | `10` | Parallel transfers for OCI uploads and downloads. Must be greater than 0. |
| `download_size_limit_gb` | `650` | Largest total source size, in GiB, that the agent copies. |
| `enable_size_limit_check` | `true` | Enforce `download_size_limit_gb`. |
| `hf_download_timeout` | `72h` | Overall timeout for a Hugging Face snapshot download. |
| `hf_download_stale_progress_timeout` | `30m` | Fail a Hugging Face download that moves no bytes or files for this long. |
| `target_artifact_reuse_allowed` | `false` | On OCI targets, skip the copy when the target already holds a complete artifact, and coordinate concurrent runs with an upload lock. See [Idempotent reruns](#idempotent-reruns-artifact-reuse-and-the-upload-lock). |
| `artifact_upload_lock_owner_id` | unset | ID that lets retries of one replication adopt the upload lock a killed attempt left behind. |
| `artifact_upload_lock_timeout` | `120h` | How long a run waits on another run's upload lock before treating it as stale. |

Before any data moves, the agent lists the source — the repository's files at the branch, the bucket's objects under the prefix, or the files under the sub-path — and sums their sizes. When the check is on and the total exceeds the limit, the run fails with `Model weights exceed size limit`; when the source holds no files, with `No model weights exist in the model folder`.

A commented sample config ships in the OME repository at [`config/ome-agent/ome-agent.yaml`](https://github.com/ome-projects/ome/blob/main/config/ome-agent/ome-agent.yaml). It also carries keys for the other `ome-agent` subcommands, which the replica command ignores.

### The `source` and `target` blocks

Both sides require `storage_uri`, and each side must enable the client that matches its URI scheme:

- An `oci://` side needs `oci.enabled: true` in its block. Without it, the run fails at startup with `required Source.OCIOSDataStore is nil`, or `Target.OCIOSDataStore` for the target.
- A `pvc://` side needs `pvc.enabled: true`. Without it: `required Source.PVCFileSystem is nil`, or `Target.PVCFileSystem`.
- An `hf://` source needs no flag; the Hub client is always available.

The OCI keys sit under `source.oci` and `target.oci`, so the two sides can use different regions, tenancies and credentials:

| Key | Default | Description |
| --- | --- | --- |
| `enabled` | `false` | Build the Object Storage client for this side. |
| `auth_type` | — (required) | `UserPrincipal`, `InstancePrincipal`, `ResourcePrincipal` or `OkeWorkloadIdentity`. |
| `region` | unset | Region of this side's bucket. |
| `compartment_id` | unset | Compartment OCID. |
| `enable_obo_token` | `false` | Authenticate with an on-behalf-of token. |
| `obo_token` | unset | The OBO token. Required when `enable_obo_token` is `true`. |

A Hugging Face source reads top-level keys:

| Key | Default | Description |
| --- | --- | --- |
| `hf_token` | unset | Token for gated or private repositories. |
| `endpoint` | `https://huggingface.co` | Hub endpoint. |
| `cache_dir` | `/tmp/.cache/huggingface` | Hub cache directory. |
| `max_concurrent_downloads` | `4` | Parallel download workers. A `0` means the default. |
| `enable_dedup` | `false` | Xet chunk deduplication. |

## How each pair moves data

- **`hf://` → `oci://`** downloads the full snapshot at the branch into the scratch directory `<local_path>/replica`, then uploads every file to `<target-prefix>/<relative-path>` with `num_connections` parallel workers. The scratch directory is removed on success.
- **`oci://` → `oci://`** streams object by object through `<local_path>/replica` with `num_connections` workers: each object is downloaded, then uploaded under the target prefix, with the source prefix in its name replaced by the target prefix. The scratch directory is removed only after every object finishes, so it holds the whole artifact.
- **`pvc://` → `oci://`** uploads every file under `<local_path>/<source-sub-path>` to `<target-prefix>/<relative-path>`.
- **`hf://` → `pvc://`** downloads the snapshot directly into `<local_path>/<target-sub-path>`.
- **`oci://` → `pvc://`** downloads each object into `<local_path>/<target-sub-path>` with `num_connections` workers. Each file keeps its full object name as its path under that directory, including the source prefix directories.
- **`pvc://` → `pvc://`** copies the tree under `<local_path>/<source-sub-path>` to `<local_path>/<target-pvc-name>/<target-sub-path>`, preserving file modes.

For `hf://` → `oci://` and `oci://` → `oci://`, give `local_path` room for the entire model: an emptyDir with a sufficient `sizeLimit`, or a scratch PVC. Existing objects and files at the target are overwritten, and the agent never deletes anything else already there — only its own completion marker on OCI targets, as the next sections describe.

## PVC sides and `local_path`

The agent reads and writes the local filesystem, so when a side is `pvc://`, the pod must mount that claim where the agent expects it:

- **PVC source**: the agent reads `<local_path>/<source-sub-path>`. Mount the source claim at `local_path`.
- **PVC target** of an `hf://` or `oci://` source: the agent writes `<local_path>/<target-sub-path>`. Mount the target claim at `local_path`.
- **PVC → PVC**: mount the source claim at `local_path` and the target claim at `<local_path>/<target-pvc-name>`. Both URIs must carry the same namespace — both none, or both the same one — or the run fails with `source PVC and target PVC namespaces do not match`. When source and target URIs are identical, the run is a successful no-op.

Since a pod can only mount claims from its own namespace, run the Job in the claims' namespace, on nodes that can attach them.

## Checksums on OCI targets

For the three pairs with an OCI target, `target.checksum` attaches a checksum of each uploaded file as object metadata, as the config in [Step 1](#step-1-write-the-replication-config) does:

| Key | Default | Description |
| --- | --- | --- |
| `upload_enabled` | `false` | Compute and attach checksums. |
| `algorithm` | unset | `md5` or `sha256`. |
| `concurrency` | unset | Checksums computed at once. Unset or less than 1 means one at a time. |

The checksum is computed from the local file just before its upload, and stored base64-encoded under the object metadata key `opc-meta-md5` or `opc-meta-sha256`. The block is ignored for PVC targets.

## Idempotent reruns: artifact reuse and the upload lock

By default every run re-copies everything. On OCI targets, `target_artifact_reuse_allowed: true` makes reruns idempotent and concurrent runs safe to race, with two sentinel objects directly under the target prefix:

- `.ome-artifact-complete` — written after every file has uploaded.
- `.ome-artifact-upload.lock` — held while a run uploads.

With reuse on, a run first inspects the target. When the completion marker and at least one weight object are present, it skips the copy and succeeds. Otherwise it creates the lock — atomically, so only one run can hold it — deletes any old completion marker, copies, writes the marker, and releases the lock. A run that finds another run's lock polls the target every 30 seconds until the marker appears (it skips and succeeds), the lock disappears (it acquires), or the lock's age passes `artifact_upload_lock_timeout` (it deletes the stale lock and acquires). When none of that happens within `artifact_upload_lock_timeout` of the run's start, the run fails with `timed out waiting for target artifact completion marker`. Neither sentinel is ever copied as model content when the target later serves as a source.

A killed attempt leaves its lock behind. Set `artifact_upload_lock_owner_id` so that retries of the same replication adopt that leftover lock instead of waiting for it to go stale:

```yaml
target_artifact_reuse_allowed: true
artifact_upload_lock_owner_id: "replicate-llama-3-2-1b"
```

Use one ID for all retries of one replication — a Job's retry pods share one through the ConfigMap — and a new ID for each independent replication. The agent only compares IDs, so make sure the earlier attempt has actually stopped before a retry starts with the same one. And give every run that targets the same prefix the same `artifact_upload_lock_timeout`, longer than your longest expected copy: a run with a shorter timeout can delete a lock whose upload is still going.

!!! warning "OCI targets always need delete permission"
    Reuse needs list, read, write and delete permission on the target bucket. Even with reuse off, a run with an OCI target deletes any existing `.ome-artifact-complete` before it uploads, so no reader takes a half-overwritten prefix for complete — and a reuse-off run doesn't write the marker back, so the next reuse-on run copies again.

## Troubleshooting

On a replication error, the agent writes the message to `/dev/termination-log` and exits with code 1, so the message shows on the failed pod:

```bash
kubectl get pods -n ome -l job-name=replicate-llama-3-2-1b \
  -o 'custom-columns=NAME:.metadata.name,MESSAGE:.status.containerStatuses[0].state.terminated.message'
```

```output
NAME                           MESSAGE
replicate-llama-3-2-1b-x7k2p   unsupported replication: OCI → HUGGINGFACE
```

For errors that show no message there, read the pod's log with `kubectl logs job/replicate-llama-3-2-1b -n ome`.

### `unsupported replication`

The URI schemes aren't one of the six pairs in [Supported pairs and storage URIs](#supported-pairs-and-storage-uris) — here a run tried to copy into Hugging Face, which is a source only. Fix the `storage_uri` values.

### `required Source.OCIOSDataStore is nil`

A side's URI is `oci://` but its block doesn't set `oci.enabled: true`. The matching `pvc://` mistake fails with `required Source.PVCFileSystem is nil`, and the target side names `Target` instead. These fail at startup, before the agent runs, so the message is in the pod's log. Enable the client that matches each side's scheme, as in [The `source` and `target` blocks](#the-source-and-target-blocks).

### The run aborts on the size check

`Model weights exceed size limit of 697932185600 bytes` means the source is larger than `download_size_limit_gb`: raise the limit, or set `enable_size_limit_check: false`. `No model weights exist in the model folder` means the source prefix, sub-path or branch names nothing: fix the source `storage_uri`. Both abort before anything is copied, and report only in the log.

### The Job retries after an interruption

A run interrupted before completion fails with `replication interrupted before completion`, and the Job starts another pod within `backoffLimit`. Without artifact reuse, the retry re-copies everything from the start. With reuse on, it skips what already completed, and with `artifact_upload_lock_owner_id` set it adopts the lock the killed attempt left instead of waiting `artifact_upload_lock_timeout` for it to go stale.

### `timed out waiting for target artifact completion marker`

The run waited `artifact_upload_lock_timeout` on another run's upload lock, and the lock neither finished nor went stale. If the other run is still uploading, wait for it or raise the timeout. If the lock belongs to an earlier attempt of this same replication, set `artifact_upload_lock_owner_id` so retries adopt it, as in [Idempotent reruns](#idempotent-reruns-artifact-reuse-and-the-upload-lock).

## Clean up

Delete the Job, the config and the token:

```bash
kubectl delete job replicate-llama-3-2-1b -n ome
kubectl delete configmap llama-replica-config -n ome
kubectl delete secret hf-token -n ome
```

```output
job.batch "replicate-llama-3-2-1b" deleted from ome namespace
configmap "llama-replica-config" deleted from ome namespace
secret "hf-token" deleted from ome namespace
```

The copied weights stay in the bucket. To remove them too, delete the objects under the target prefix with your OCI tooling.

## Next steps

- [Base models](../../concepts/models/base-models.md): point a BaseModel or ClusterBaseModel at the copied weights with an `oci://` or `pvc://` storage URI.
- [Serve models from a PVC](../deploy-models/serve-models-from-pvc.md): serve the weights that a replication put on a claim.
- [Run the model agent](model-agent.md): the DaemonSet that downloads models onto nodes for serving.
- [Share Hugging Face artifacts](shared-hf-artifacts.md): share one downloaded snapshot per node instead of copying between storage systems.
