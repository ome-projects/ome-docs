---
title: Run the model agent
description: Turn on the optional model agent for models stored on the nodes, and configure how it prepares them.
---

The model agent puts the weights of BaseModels and ClusterBaseModels on your nodes. It runs as the DaemonSet `ome-model-agent-daemonset`. On each node, it gets the files of the models that select the node, reads what each model is, and labels the node when the model is `Ready` there. OME runs a model's serving pods only on nodes with that label.

Since v1.3, the agent is optional and off by default. Turn it on to serve [base models](../../concepts/models/base-models.md) stored on the nodes: `hf://`, `oci://`, `modelpack://`, `local://` and `vendor://` models. Until you turn it on, those models stay `In_Transit`, and OME can't serve them. You don't need it for `pvc://` models, or for InferenceServices that name only a [runtime that loads its own weights](../../concepts/serving/inference-services.md#the-runtime).

<div class="prerequisites" markdown>

- OME installed from the `ome-resources` chart in the namespace `ome`, which the agent reports to, and `kubectl` access to the cluster. See [Install OME](../../getting-started/install.md).
- Helm, and the values file you install `ome-resources` with.
- For Step 3, `jq` and a model stored on the nodes. The examples use the ClusterBaseModel `qwen2-5-7b-instruct` from [Base models](../../concepts/models/base-models.md), on three GPU nodes that it selects: `gpu-node-1`, `gpu-node-2` and `gpu-node-3`.

</div>

## Step 1: Turn on the agent

On v1.2.2, the chart always installs the agent, so go to [Step 2](#step-2-check-the-agent-on-each-node). Otherwise, add the value to the values file you install `ome-resources` with:

```yaml title="values.yaml"
modelAgent:
  enabled: true
```

The agent runs on all nodes whose taints it tolerates, GPU nodes included. Each agent pod requests 10 CPUs and `100Gi` of memory, and its priority class, `system-node-critical`, lets Kubernetes evict lower-priority pods to make room. The agent keeps the weights in the node's `/mnt/data/models`, which needs room for the models that select the node. To use fewer nodes or another directory, also set `gpuNodesOnly`, `nodeSelector` or `hostPath` from [Helm values](#helm-values).

The value needs chart version 1.3.0 or later. Upgrade the release with the chart version you already run, 1.3.0 in this example:

```bash
helm upgrade --install ome oci://ghcr.io/moirai-internal/charts/ome-resources \
  --namespace ome --version 1.3.0 -f values.yaml
```

Until v1.3 is released, upgrade from a checkout of `main` instead, as in [Install from source](../../getting-started/install.md#install-from-source).

Helm prints `Release "ome" has been upgraded. Happy Helming!`. Wait for the agent pods to start:

```bash
kubectl rollout status daemonset/ome-model-agent-daemonset -n ome
```

While the pods start, kubectl prints `Waiting for daemon set` lines. When all of them are available, it prints:

```output
daemon set "ome-model-agent-daemonset" successfully rolled out
```

## Step 2: Check the agent on each node

Each node that should hold models needs a ready agent pod. List the agent pods with their nodes:

```bash
kubectl get pods -n ome -l app.kubernetes.io/component=ome-model-agent-daemonset \
  -o 'custom-columns=NAME:.metadata.name,NODE:.spec.nodeName,READY:.status.containerStatuses[0].ready'
```

```output
NAME                              NODE         READY
ome-model-agent-daemonset-4xkqz   gpu-node-1   true
ome-model-agent-daemonset-9mfpt   gpu-node-2   true
ome-model-agent-daemonset-tq7wd   gpu-node-3   true
```

If a node is missing, or a pod shows `false`, see [Troubleshooting](#troubleshooting).

## Step 3: Check where a model is ready

Show the label of `qwen2-5-7b-instruct` on all nodes:

```bash
kubectl get nodes \
  -o 'custom-columns=NODE:.metadata.name,STATE:.metadata.labels.models\.ome\.io/clusterbasemodel\.qwen2-5-7b-instruct'
```

```output
NODE         STATE
gpu-node-1   Ready
gpu-node-2   Ready
gpu-node-3   Updating
```

The model is ready on `gpu-node-1` and `gpu-node-2`. On `gpu-node-3`, the agent is still downloading or checking the files, and the label turns `Ready`, or `Failed`, when it's done. Nodes outside the model's selector show `<none>`. For a BaseModel, the label key is `models.ome.io/{namespace}.basemodel.{name}`. Long names are shortened with a hash, as [Node labels and status](../../concepts/models/base-models.md#node-labels-and-status) describes.

The agent also records each model in a ConfigMap named after the node, in `ome`, under the key `clusterbasemodel.{name}` or `{namespace}.basemodel.{name}`. In the JSON entry, `status` matches the label, `config` holds the model's details once it's ready, and `progress` shows how far a Hugging Face download has got: see [Watch a Hugging Face download](#watch-a-hugging-face-download). Show the name and state from the entry on `gpu-node-1`:

```bash
kubectl get configmap gpu-node-1 -n ome \
  -o jsonpath='{.data.clusterbasemodel\.qwen2-5-7b-instruct}' | jq '{name, status}'
```

```output
{
  "name": "qwen2-5-7b-instruct",
  "status": "Ready"
}
```

The ConfigMap is the agent's record of the node, not what it works from: the agent keeps the same records in memory while it runs, and heals the ConfigMap from them. It checks the ConfigMap when it starts and every five minutes after that, recreates it when it's gone, and puts back each missing record with the model's `status` and `config` as it last wrote them. A write for any model recreates a missing ConfigMap right away, and the next five-minute pass fills in the rest.

So when a cleanup script — or your own `kubectl delete` — removes the ConfigMap or one of its records while the agent runs, nothing needs doing: the models' files and labels on the node stay as they are, nothing downloads again, and the records are back within five minutes, without an agent restart. Until they are, the model's `status.nodesReady` can drop the node, since OME builds it from these ConfigMaps: see [Node labels and status](../../concepts/models/base-models.md#node-labels-and-status). A restored record has no `progress`; a download that's still running writes it again as it reports progress.

Two cases heal differently. The records of models that [share a Hugging Face copy](shared-hf-artifacts.md#how-sharing-works) come back needing a check: those models are `Failed` on the node until the agent validates the shared copy, then get their saved state back — the repair that section describes. And the healing restores what the running agent has written, so a ConfigMap lost while no agent runs — OME deletes it when the node leaves the cluster — comes back as the next agent pod checks the node's models again: for shared copies, see [Losing the node's records](shared-hf-artifacts.md#losing-the-nodes-records).

!!! warning "Don't edit the ConfigMap"
    The healing restores records that are missing; it doesn't check records that are still there. A record you change by hand stands, and OME reads it as the node's state. Treat the ConfigMap as the agent's own, like the [`_artifacts`](shared-hf-artifacts.md#step-2-check-the-shared-copy-on-the-node) directory and the [`.hf-artifact-locks` directories](#hf-artifact-locks).

## Configure the agent {#step-2-change-a-setting}

Set the agent's values under `modelAgent` in your values file, and upgrade the release as in [Step 1](#step-1-turn-on-the-agent).

### Helm values

| Value | Default | What it does |
| --- | --- | --- |
| `enabled` | `false` | Since v1.3. Installs the agent. |
| `gpuNodesOnly` | `false` | When `true`, runs the agent only on nodes with the `gpuNodeLabel` label. |
| `gpuNodeLabel` | `key: nvidia.com/gpu.present`, `value: "true"` | The node label that `gpuNodesOnly` selects. |
| `nodeSelector` | `{}` | More node labels that the agent's nodes must have. |
| `tolerations` | `nvidia.com/gpu` taints with the effect `NoSchedule` | The agent pod's tolerations. |
| `affinity` | Not set | The agent pod's affinity. |
| `resources` | 10 CPUs and `100Gi` of memory, requested and limited | The agent container's resources on each node. |
| `hostPath` | `/mnt/data/models` | The node directory for models, which must hold each model's `path`. Set `/raid/models` to apply the models in `config/models/`. |
| `extraVolumes`, `extraVolumeMounts` | `[]` | More volumes for the agent pod, and their mounts, for more host directories of models. |
| `env` | `{}` | The agent's [environment variables](#environment-variables). |
| `image` | `model-agent` from `global.hub`, at the chart's OME version | The agent's image: `hub`, `repository`, `tag` and `pullPolicy`. |
| `imagePullSecrets` | `global.imagePullSecrets` | Secrets for pulling the image. |
| `priorityClassName` | `system-node-critical` | The agent pod's priority class. |
| `modelVerificationConcurrency` | `0` | Since v1.3. How many `oci://` files the agent verifies at once, across all models. `0` means 2, the number of download workers. |
| `health.port` | `8080` | The probes' port. Leave it at `8080`, the port the agent listens on. |

!!! warning "A change to the agent's pod restarts every agent"
    The DaemonSet replaces the agent pods one node at a time. Each new agent checks its models again. Until it's done, their labels are `Updating`, and OME places no new serving pods for them on that node. Pods that already run there keep running. An `hf://` download that the replacement interrupts isn't lost: the new agent re-fetches only what's missing, as [What an interrupted `hf://` download keeps](#interrupted-hf-downloads) describes.

### Environment variables

Set the agent's environment variables in `modelAgent.env`, a map of names to values:

```yaml title="values.yaml"
modelAgent:
  enabled: true
  env:
    LOG_LEVEL: debug
```

| Variable | What it does |
| --- | --- |
| `LOG_LEVEL` | The log level: `debug`, `info`, `warn` or `error`. `info` by default. |
| `HF_TOKEN` | The Hugging Face token for models without one of their own. |
| `ENDPOINT` | The Hugging Face endpoint, `https://huggingface.co` by default. |
| `MAX_CONCURRENT_DOWNLOADS` | How many files of a Hugging Face model download at once, 4 by default. |
| `LLMMAN_HOST` | Where the `llmman serve` daemon for [`modelpack://` models](#modelpack-models) listens, as `[scheme://]host[:port]`. `http://127.0.0.1:17434` by default. A wildcard host, such as `0.0.0.0`, is read as loopback. |
| `OME_LLMMAN_BIN` | The `llmman` binary for [`modelpack://` models](#modelpack-models), when it isn't `llmman` on the agent's `PATH`. |

The chart writes these values into the DaemonSet in plain text, so give each model its token through its Secret instead: see [Credentials](../../concepts/models/base-models.md#credentials). Other variables named after the agent's command-line flags have no effect, such as `DOWNLOAD_RETRY`, which the chart's values.yaml suggests.

## How downloads run {#workers-and-queues}

The chart runs two download workers, so at most two models download at once on a node, and the others wait their turn. After a restart, the agent checks its models again, and downloads go first: the agent re-checks an `oci://` model that was `Ready` before the restart, and that still has its files on the node, only when no download is waiting. Checks of `hf://` models, and of models that weren't `Ready` or that lost their files, queue up with the downloads, so a new model's download can wait behind them.

The chart also runs one high-priority worker, for work that shouldn't wait behind those downloads. Deletes go to the front of its queue: see [How a delete stops a download](#how-a-delete-stops-a-download). A new `oci://` model also starts on it: the worker checks whether the model can reuse files already on the node, and re-queues it for the two download workers when it needs a download after all, so at most two models still download at once.

The agent doesn't retry a failed model by itself. It tries again when [its pod restarts](#a-model-stays-updating), or when [a change to the model](../../concepts/models/base-models.md#choose-the-nodes) makes the nodes process it again.

When two `oci://` models name the same files, one downloads them, and the other reuses them once they're ready, or downloads them itself after 30 minutes. The waiting model re-checks every 30 seconds on the high-priority worker, so the reuse waits only for the other model's download, not for a free download worker.

With `storage.downloadPolicy: ReuseIfExists`, models that use the same Hugging Face files share one copy on each node: see [Share Hugging Face artifacts](shared-hf-artifacts.md).

### Watch a Hugging Face download

While an `hf://` model downloads, its entry in the node's ConfigMap from [Step 3](#step-3-check-where-a-model-is-ready) carries `progress`, the download's latest report: the agent writes the newest report there every 30 seconds, and once more when the download stops. Read it on `gpu-node-3`, where the model is still `Updating`:

```bash
kubectl get configmap gpu-node-3 -n ome \
  -o jsonpath='{.data.clusterbasemodel\.qwen2-5-7b-instruct}' | jq .progress
```

```output
{
  "phase": "downloading",
  "totalBytes": 16331279104,
  "completedBytes": 7349075712,
  "totalFiles": 14,
  "completedFiles": 9,
  "speedBytesPerSec": 193257683.2,
  "lastUpdated": "2026-11-04T09:21:37Z"
}
```

`completedBytes` over `totalBytes` is the download's percentage — 45% here.

| Field | What it holds |
| --- | --- |
| `phase` | `scanning` while the download lists the snapshot's files — the totals can still be `0` — then `downloading`, and `finalizing` once every file is done. |
| `totalBytes`, `totalFiles` | The bytes and files of the whole snapshot, files already on the node included, not just what's left to download. |
| `completedBytes`, `completedFiles` | How much of that is done. A file that's already in the `path` at its full size counts as soon as the download reaches it, so [a retry](#interrupted-hf-downloads) jumps ahead by the files it kept. |
| `speedBytesPerSec` | Bytes per second between the last two reports: the speed over roughly the last 30 seconds, not over the whole download. `0` when no bytes arrived between them. |
| `lastUpdated` | When the download last reported, as an RFC3339 timestamp. |

The download reports only when it moves — bytes arrive, a file finishes, or the phase changes — and a 30-second pass that finds no new report writes nothing. So while the transfer is healthy, `lastUpdated` stays under about a minute old, and a `lastUpdated` several minutes old on a model that's still `Updating` means the download is moving no bytes or files: read the agent's log on the node, as [Troubleshooting](#troubleshooting) shows.

Only `hf://` downloads write `progress`: a model downloading its own copy under the default `AlwaysDownload` policy, and the one model downloading the copy that [`ReuseIfExists` models share](shared-hf-artifacts.md#how-sharing-works) — the models waiting on that copy show none. `oci://` and `modelpack://` downloads never write it.

When the download ends and the model's `status` on the node turns `Ready` or `Failed`, the same write clears `progress`, and the command above prints `null`. A vanished `progress` marks the end of the download, not a lost record.

### What an interrupted `hf://` download keeps {#interrupted-hf-downloads}

With the default `downloadPolicy`, `AlwaysDownload`, an interrupted `hf://` download keeps the files that finished. When the node's agent pod is replaced or evicted mid-download, they stay in the model's `path`, the model's label on the node stays `Updating`, and nothing marks the model `Failed` or deletes the partial download.

The next pass re-fetches only what's missing. The new agent pod checks the node's models again, the download queues up again, and it skips every file that's already in the `path` at its full size, without reading it again. The files that were mid-transfer — up to [`MAX_CONCURRENT_DOWNLOADS`](#environment-variables) of them — start over from their first byte: the agent can't resume partway through a file. So losing an agent pod near the end of a long download costs those files, not the finished ones.

That holds as long as the retry downloads the same commit. A node [resolves an unpinned revision again](../../concepts/models/base-models.md#pin-the-hf-revision) every time it processes the model, so once `main` moves, the retry downloads the newly resolved commit instead. To keep every pass on one snapshot, pin the commit in the URI, as in `hf://Qwen/Qwen2.5-7B-Instruct@<commit>`.

This is the contract of the default policy, where each model's copy is its own. For the shared copy that `ReuseIfExists` models use, see [Share Hugging Face artifacts](shared-hf-artifacts.md#how-sharing-works).

### How a delete stops a download

When you delete a model, the agent stops the model's work on each node, so a node doesn't spend hours downloading weights you deleted:

- A download that's waiting its turn never starts: the delete drops it from the queue.
- The agent cancels the model's running download as soon as it sees the deletion. A Hugging Face download stops right away. Since v1.3, an `oci://` download stops too: the agent starts no new files, lets a file under 200 MiB finish, and stops the multipart transfer of a larger file mid-file, removing its temporary files. A request to object storage that has already started keeps its retries and returns first, so the stop can wait for one request. On v1.2.2, the agent can't stop an `oci://` download once it has started: it runs to completion.

A canceled download doesn't mark the model `Failed` on the node. The agent then deletes the files that reached the node and removes the model's label, following the rules in [Delete a model](../../concepts/models/base-models.md#deleting-a-model), and the model's deletion finishes once every node reports its copy deleted. That cleanup never waits for a free download worker: the delete runs on the [high-priority worker](#workers-and-queues), ahead of that worker's other queued work, even while both download workers spend hours on other models' downloads.

### The `.hf-artifact-locks` directories {#hf-artifact-locks}

The agent keeps lock files in directories named `.hf-artifact-locks`: one appears next to the `path` of each `hf://`, `oci://` and `modelpack://` model on the node, whatever the model's `downloadPolicy`. With the paths directly under `modelAgent.hostPath`, that's one directory for all models, `/mnt/data/models/.hf-artifact-locks`. [Sharing](shared-hf-artifacts.md) keeps one next to `_artifacts` too.

A download or deletion holds the model's lock file while it works on the files, and a task that finds the lock held waits and tries again. The locks are file locks on the node's disk, so they hold across agent pods: while [a replaced agent pod](#step-2-change-a-setting) is still finishing a download, the new pod waits instead of writing to the same `path`.

The lock files are empty, and hold no lock once their operation ends. Deleting a model leaves them and their directory in place. Don't create, change or delete anything in these directories: removing a lock file in use can let two operations write to or delete the same model's files at once.

### TensorRT-LLM models

For an `oci://` model with `modelFormat.name: tensorrtllm` whose type is `Serving` — the default — the agent downloads only the objects built for its node's GPU, whose names contain `/{gpu}/`, such as `/H100/`. It finds the GPU name by looking up the node's `node.kubernetes.io/instance-type` label in the ConfigMap `model-agent-config-map`, and uses an unlisted instance type as it is. When no object matches, the model fails on the node.

The type is `Serving` unless `additionalMetadata.type` in the model's spec sets another value, and the agent filters only when it's exactly `Serving`. With any other value, such as `additionalMetadata.type: Base`, there's no filter: every node the model selects downloads every object under the URI's prefix — the builds for all GPUs — and the model can't fail with `no suitable objects found for shape`, even when no object matches its node's GPU. So leave `additionalMetadata.type` unset on a TensorRT-LLM model that should download only its node's objects. [Sharing](shared-hf-artifacts.md#how-sharing-works) reads the type the same way: only a serving TensorRT-LLM model keeps a copy of its own instead of sharing a Hugging Face snapshot.

Since v1.3, `modelAgent.instanceTypeMap` doesn't change the ConfigMap, and the map lacks `BM.GPU.B300.8`, so a model with objects under `/B300/` fails on B300 nodes. This is a known bug. Edit the ConfigMap's `instance-type-map` key instead, and run `kubectl rollout restart daemonset/ome-model-agent-daemonset -n ome`. After each `helm upgrade`, check the key, and do both again if your entry is gone. On v1.2.2, add the instance type to `modelAgent.instanceTypeMap`.

### ModelPack models

A `modelpack://` model is a [CNCF ModelPack](https://github.com/modelpack/model-spec) artifact in a container registry. The agent doesn't talk to the registry itself. It asks a running [`llmman serve`](https://github.com/llmmanorg/llmman) daemon to pull the artifact, runs `llmman resolve --no-pull` to find where the daemon put the files, and copies them to the model's `path`, with hard links when the daemon's store is on the same filesystem, so the files cost their bytes once. From there, the model behaves like an `hf://` or `oci://` model: the agent reads its files, as [What OME learns from the model](../../concepts/models/base-models.md#what-ome-learns-from-the-model) describes, labels the node, and [deletes the files](../../concepts/models/base-models.md#deleting-a-model) when you delete the model.

This ClusterBaseModel pulls the artifact `ghcr.io/myorg/llama-3-70b:v1`:

```yaml title="model.yaml"
apiVersion: ome.io/v1beta1
kind: ClusterBaseModel
metadata:
  name: llama-3-70b
spec:
  storage:
    storageUri: modelpack://ghcr.io/myorg/llama-3-70b:v1
    path: /mnt/data/models/myorg/llama-3-70b
```

OME ships neither the daemon nor the binary, so provide both to the agent on every node that the model selects:

- A running `llmman serve` daemon. The agent finds it through `LLMMAN_HOST`, `http://127.0.0.1:17434` by default.
- The `llmman` binary, on the agent's `PATH` or at the path in `OME_LLMMAN_BIN`.

Set both variables in `modelAgent.env`: see [Environment variables](#environment-variables). The agent copies the files from the path that `llmman resolve` reports, and fails when that path doesn't exist in its pod, so mount the binary and the daemon's store into the agent pod with `extraVolumes` and `extraVolumeMounts`.

The daemon does the registry work, authentication included, so the model's `storage.key` and `storage.parameters` play no role: give the daemon the registry's credentials in its own configuration. When the daemon isn't reachable, the binary is missing, or the pull fails, the model fails on the node: see [Troubleshooting](#troubleshooting).

## Metrics {#health-checks-and-metrics}

The agent serves Prometheus metrics at `/metrics` on port 8080. Most have the labels `model_type`, `namespace` and `name`. `model_type` is `BaseModel` or `ClusterBaseModel`, and `namespace` is empty for a ClusterBaseModel:

| Metric | Type | What it records |
| --- | --- | --- |
| `model_agent_downloads_success_total` | Counter | Models that became `Ready` on the node. |
| `model_agent_downloads_failed_total` | Counter | Models that failed on the node while the agent got their files. |
| `model_agent_download_duration_seconds` | Histogram | How long models took to become `Ready`. The buckets stop at 51.2 seconds, so longer downloads land in `+Inf`. |
| `model_agent_download_bytes_total` | Counter | Bytes of the `oci://` models the agent verified, including files already on the node. |
| `model_agent_verifications_total` | Counter | Verifications of `oci://` downloads, with a `result` label: `success` or `failure`. |
| `model_agent_md5_checksum_failed_total` | Counter | Failed verifications. |
| `model_agent_verification_duration_seconds` | Histogram | How long verifications took. |
| `model_agent_rate_limit_total` | Counter | Hugging Face rate-limit errors. |
| `model_agent_rate_limit_wait_seconds` | Histogram | An estimated wait per Hugging Face rate-limit error. The agent doesn't measure the wait: every observation is a fixed 30 seconds. |

Since v1.3, `podMonitor.enabled: true` creates the PodMonitor `ome-model-agent` for the Prometheus Operator, which needs the Operator's `monitoring.coreos.com/v1` CRDs. `podMonitor.interval`, `podMonitor.scrapeTimeout` and `podMonitor.additionalLabels` tune it. [Collect metrics](metrics.md) covers the other ways to scrape the agent.

## Troubleshooting

The agent logs what it does with each model. Find the agent pod on a node, here `gpu-node-3`:

```bash
kubectl get pods -n ome -l app.kubernetes.io/component=ome-model-agent-daemonset \
  --field-selector spec.nodeName=gpu-node-3 -o name
```

```output
pod/ome-model-agent-daemonset-tq7wd
```

Then read its log lines about the model:

```bash
kubectl logs -n ome pod/ome-model-agent-daemonset-tq7wd | grep qwen2-5-7b-instruct
```

Look for the line that ends in `status to Updating before download`, where the agent starts on the model, and for errors after it.

### The model stays `In_Transit` or `Failed`

A model stays `In_Transit` while no node has it `Ready` or `Failed`:

- The agent is off: turn it on as in [Step 1](#step-1-turn-on-the-agent).
- The model's nodes lack an agent pod: see [A node never gets the label](#a-node-never-gets-the-label).
- The nodes are still downloading it: see [A model stays `Updating`](#a-model-stays-updating).

When the model is `Failed`, read the log of the agent on a node where its label is `Failed`:

| Cause | Log line |
| --- | --- |
| An `oci://` file differs from its size or MD5 in object storage, or changed during the download. | `MD5 or size mismatch for`, then `All download attempts failed for model` |
| The agent can't read the model's [Secret](../../concepts/models/base-models.md#credentials), which a ClusterBaseModel keeps in `ome`, so a gated model fails. | `Failed to retrieve secret` |
| A TensorRT-LLM model has no objects for the node's GPU: see [TensorRT-LLM models](#tensorrt-llm-models). | `no suitable objects found for shape` |
| A `modelpack://` model can't be pulled: no `llmman serve` daemon answers at `LLMMAN_HOST`, or the `llmman` binary is missing. See [ModelPack models](#modelpack-models). | `Failed to pull ModelPack artifact for model` |
| Hugging Face answered with an error or a rate limit. | The error |

After you fix the cause, restart the agent pod on the node, as in [A model stays `Updating`](#a-model-stays-updating). For an `oci://` model, the new agent downloads only the mismatched files. For an `hf://` model, it skips the files already in the `path` at their full size and downloads the rest: see [What an interrupted `hf://` download keeps](#interrupted-hf-downloads).

### A model stays `Updating`

A model's label can stay `Updating` because:

- The agent restarted: see the warning in [Configure the agent](#step-2-change-a-setting).
- The model is waiting its turn, or still downloading. For a Hugging Face model, `progress` in the node's ConfigMap shows how far it has got, and a `lastUpdated` several minutes old there means the download has stalled: see [Watch a Hugging Face download](#watch-a-hugging-face-download).
- Another `oci://` model is downloading the same files. The log has a `Waiting for artifact operation` line every 30 seconds: see [How downloads run](#workers-and-queues).
- The agent has no downloader for the model's scheme, `s3://`, `az://`, `gs://` or `github://`, and logs `unknown storage type`. See [Where the weights come from](../../concepts/models/base-models.md#where-the-weights-come-from).
- The agent can't parse the `oci://` URI, and logs `Failed to get target directory path for model` with the reason. Use the form from the same section.

After you fix the cause, delete the agent pod on the node. Its replacement processes all the models on the node again:

```bash
kubectl delete pod -n ome ome-model-agent-daemonset-tq7wd
```

kubectl confirms the deletion, and the DaemonSet starts a new agent pod on the node.

### A node never gets the label

When the model's label never appears on a node:

- The node has no agent pod: check the list from [Step 2](#step-2-check-the-agent-on-each-node). The agent's `tolerations`, `gpuNodesOnly`, `nodeSelector`, `affinity` and `resources` decide where it fits: see [Helm values](#helm-values).
- The model doesn't select the node. Compare its `storage.nodeSelector` and `storage.nodeAffinity` with the node's labels: see [Choose the nodes](../../concepts/models/base-models.md#choose-the-nodes).
- The node's labels changed after the agent started. The agent doesn't watch its node's labels, so restart its pod, as in [A model stays `Updating`](#a-model-stays-updating).
- It's a `pvc://` model, which needs no label.

### The agent pod isn't ready

The pod is ready once it can write to its models directory, `modelAgent.hostPath`. Ask a pod from [Step 2](#step-2-check-the-agent-on-each-node) for its readiness through the API server:

```bash
kubectl get --raw "/api/v1/namespaces/ome/pods/ome-model-agent-daemonset-tq7wd:8080/proxy/healthz?verbose"
```

```output
[+]model-agent-health ok
healthz check passed
```

When the check fails, kubectl reports an error, and the answer ends in `reason withheld`. Ask the same pod for `/healthz/model-agent-health` to see the reason.

When the pod keeps restarting, check that `modelAgent.health.port` is `8080`, and check the `path` of your `hf://` and `oci://` models. The agent crashes on each node that an `oci://` model with no `path` field selects. It also crashes on all nodes while an `hf://` or `oci://` model with no `path` is being deleted. This is a known bug: give every `hf://` and `oci://` model a `path`, and see [The agent keeps restarting](configure-model-artifact-retention.md#the-agent-keeps-restarting).

### The model's status doesn't list its nodes

When the model's label is on the nodes but its `status.nodesReady` stays empty, check that OME runs in the namespace `ome`. The agent always reports to `ome`, and no Helm value changes that, so install OME there.

## Clean up

Since v1.3, you can turn the agent off. First, delete the BaseModels and ClusterBaseModels stored on the nodes, and wait until they're gone. Each model's deletion waits for the agent on its nodes, as [Delete a model](../../concepts/models/base-models.md#deleting-a-model) describes. Then remove `enabled: true` from the values, and upgrade the release as in [Step 1](#step-1-turn-on-the-agent).

## Next steps

- [Share Hugging Face artifacts](shared-hf-artifacts.md): share one copy of a Hugging Face model's files between models.
- [Keep downloaded model files](configure-model-artifact-retention.md): keep a model's files on the nodes after you delete it.
- [Serve models from node-local storage](../deploy-models/serve-models-from-local-storage.md): serve weights already on your nodes' disks.
- [Base models](../../concepts/models/base-models.md): how a model chooses its nodes, and what OME learns from it.
- [Collect metrics](metrics.md): scrape the agent and the rest of OME.
