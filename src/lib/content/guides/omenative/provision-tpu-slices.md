---
title: Provision TPU slices on demand
navLabel: Provision TPU slices
description: "Serve an OMENative component on GKE TPU node pools that provision slices on demand: OME creates a Slice for each Instance's pods, holds the pods until it's ready, and releases it when no pod needs it."
since: v1.3
---

Serve an [OMENative](../../concepts/omenative/overview.md) component on GKE TPU node pools whose slices are provisioned on demand. You describe the pools once, in the operator's settings, and opt the component in with an annotation. For the pods of each [Instance](../../concepts/omenative/instances.md), OME then creates a Slice, the cluster-scoped GKE object that binds one TPU partition, waits for GKE to make it ready, creates the pods confined to it, and deletes the Slice once no pod needs it, which hands the capacity back.

You'll turn provisioning on for one accelerator, serve a single-pod engine on a four-chip slice, and watch OME create its Slice. An engine with a leader and workers takes the same steps with a multi-host topology: see [Multi-host slices](#multi-host-slices).

<div class="prerequisites" markdown>

- OME v1.3 or later, installed with the `ome-crd` and `ome-resources` charts, as the releases `ome-crd` and `ome` in the namespace `ome`. See [Install OME](../../getting-started/install.md). You need Helm for Step 1, and `kubectl` with the rights to create namespaces and InferenceServices and to read Slices.
- A GKE cluster that grants TPU slices through Slice objects: `kubectl get crd slices.accelerator.gke.io` finds the CRD, and a TPU node pool provisions slices on demand. The pool's nodes carry the accelerator, topology and provision-only labels from Step 1.
- A ClusterBaseModel that is `Ready` on the pool's nodes, and a ClusterServingRuntime that serves it on TPU chips. The examples call them `qwen3-0-6b` and `srt-qwen3-tpu`, and assume the runtime's engine container is named `ome-container`, like the catalog's.
- The [kubectl ome](../../reference/kubectl-ome/overview.md) plugin, to read why a pod waits.

</div>

## How OME provisions slices

OME provisions slices only for OMENative components, and only on node pools that carry the provision-only label. Every other pool holds static slices, which OME never provisions or releases: pods there schedule as usual, even when they opt in.

- One Slice serves the pods of one Instance. The leader and workers of a multi-pod Instance share it. A single-pod Instance gets one Slice per pod, so during an update's surge the new pod gets its own Slice while the pod it replaces keeps its own.
- The Slice's shape comes from what the pods already declare: the accelerator and topology node labels they select, and the TPU chips their containers request. The chips must fill the topology exactly: see [Multi-host slices](#multi-host-slices).
- OME creates an Instance's pods only once their Slice is ready: its `Ready` condition's reason is one of the configured `readyStates`. Each pod gets a node selector on the slice-name label, which confines it to the Slice's nodes. With `slice.readyTimeout` set, a Slice that has its partition but stays out of a ready state longer than the timeout is released and provisioned again, once no pod holds it.
- OME deletes a ready Slice that no pod holds when no Instance wants it anymore: after a scale-down, a shape change, or when one of its hosts can't take the pods. While pods of another workload hold TPU chips on the Slice's hosts, OME keeps it, because deleting it would deactivate the partition under them: see [A SliceReleaseDeferred event appears](#a-slicereleasedeferred-event-appears). Deleting the Slice is what returns the capacity; GKE can hold it behind a finalizer while it releases the partition.

A Slice is named after the component's [InferenceReplica](../../reference/api/ome.v1beta1.md#ome-io-v1beta1-InferenceReplica), cut to fit, then the Instance's index, the pod's ordinal and an 8-character hash of the owner's identity, such as `qwen3-tpu-engine-0-0-8f3a21c7`. The `ome.io/slice-owner-uid` label alone makes a Slice OME's: OME never adopts, modifies or deletes a Slice without it. OME also labels its Slices with `ome.io/managed-by: OMENative`, the Instance's `ome.io/instance-index`, the pod's `ome.io/pod-ordinal` and the owner kind and name labels from Step 1, and annotates them with the owner as `ome.io/slice-owner: {namespace}/{name}`.

## Step 1: Turn on slice provisioning

Describe the pools in the `ome.controller.tpuSliceProvisioning` Helm value. This example is the `ome-resources` chart's with a two-host topology and a ten-minute ready timeout added, for GKE dynamic slicing with single-host and two-host Ironwood slices:

```yaml title="values.yaml"
ome:
  controller:
    tpuSliceProvisioning:
      chipResource: google.com/tpu
      nodeLabels:
        accelerator: cloud.google.com/gke-tpu-accelerator
        topology: cloud.google.com/gke-tpu-topology
        slice: cloud.google.com/gke-tpu-slice
      provisionOnly:
        key: cloud.google.com/gke-accelerator-topology-mode
        value: PROVISION_ONLY
      accelerators:
        tpu7x:
          sliceType: tpu7x
          chipsPerHost: 4
          topologies: ["2x2x1", "2x2x2"]
      slice:
        ownerKindLabel: cloud.google.com/slice-owner-kind
        ownerNameLabel: cloud.google.com/slice-owner-name
        annotations:
          cloud.google.com/managed-by: slice-scheduler
          slice.gke.io/retry-on-failure: "true"
        readyStates: [ACTIVE, ACTIVE_DEGRADED]
        readyTimeout: 10m
```

| Field | What it sets |
| --- | --- |
| `chipResource` | The extended resource that pods request TPU chips under. |
| `nodeLabels.accelerator`, `nodeLabels.topology` | The node labels that pods select to declare a slice's shape. The accelerator value keys `accelerators`. |
| `nodeLabels.slice` | The node label that carries the name of the slice a node belongs to. GKE sets it on the nodes; OME sets it as a node selector on the pods. Never select it yourself. |
| `provisionOnly` | The node label that marks a pool whose slices are provisioned on demand. |
| `accelerators.{name}` | How slices of one accelerator are provisioned: the Slice object's `sliceType`, the chips one host has, and the `topologies` OME may provision. Write each topology in its canonical form, such as `2x2x1`, and as a whole number of hosts. |
| `slice.ownerKindLabel`, `slice.ownerNameLabel` | Labels that trace a Slice to the InferenceReplica it serves. They don't decide which Slices OME may delete: only `ome.io/slice-owner-uid` does. |
| `slice.annotations` | Annotations written on every Slice OME creates, such as the ones that hand it to GKE's slice scheduler. Required: `{}` for none. |
| `slice.readyStates` | The `Ready` condition reasons under which pods may bind to a Slice. |
| `slice.readyTimeout` | Optional. How long a Slice that has its partition may stay out of a ready state before OME releases it and provisions the slot again, once no pod holds it: see [A SliceReadyTimeout event appears](#a-slicereadytimeout-event-appears). A positive duration, such as `10m`. Unset, OME never times a Slice out. |

!!! warning "The required fields have no defaults"
    The chart's default, `{}`, provisions no slices. Once you set the block, every field the table doesn't mark optional is required, with no default in the binary, and the manager reads the block only when it starts: a field that is missing or invalid, such as a `readyTimeout` that isn't a positive duration, stops the manager from starting. See [The manager doesn't start after the upgrade](#the-manager-doesnt-start-after-the-upgrade).

Upgrade the release with the chart version that you already run, keeping your other values in the file, as [Configure the controller](../operate-ome/configure-the-controller.md#with-a-helm-value) explains:

```bash
helm upgrade --install ome oci://ghcr.io/moirai-internal/charts/ome-resources \
  --namespace ome --version 1.3.0 -f values.yaml
```

The chart writes the block to the `tpuSliceProvisioning` entry of the `inferenceservice-config` ConfigMap, and the manager's pods roll, because the Deployment carries a checksum of the ConfigMap. Wait for the rollout:

```bash
kubectl rollout status deployment ome-controller-manager -n ome
```

```output
deployment "ome-controller-manager" successfully rolled out
```

Check the entry:

```bash
kubectl get configmap inferenceservice-config -n ome -o jsonpath='{.data.tpuSliceProvisioning}'
```

```output
{"accelerators":{"tpu7x":{"chipsPerHost":4,"sliceType":"tpu7x","topologies":["2x2x1","2x2x2"]}},"chipResource":"google.com/tpu","nodeLabels":{"accelerator":"cloud.google.com/gke-tpu-accelerator","slice":"cloud.google.com/gke-tpu-slice","topology":"cloud.google.com/gke-tpu-topology"},"provisionOnly":{"key":"cloud.google.com/gke-accelerator-topology-mode","value":"PROVISION_ONLY"},"slice":{"annotations":{"cloud.google.com/managed-by":"slice-scheduler","slice.gke.io/retry-on-failure":"true"},"ownerKindLabel":"cloud.google.com/slice-owner-kind","ownerNameLabel":"cloud.google.com/slice-owner-name","readyStates":["ACTIVE","ACTIVE_DEGRADED"],"readyTimeout":"10m"}}
```

## Step 2: Opt the engine in

Create a namespace for the InferenceService:

```bash
kubectl create namespace qwen3-tpu
```

```output
namespace/qwen3-tpu created
```

Save this InferenceService as `qwen3-tpu-isvc.yaml`:

```yaml title="qwen3-tpu-isvc.yaml"
apiVersion: ome.io/v1beta1
kind: InferenceService
metadata:
  name: qwen3-tpu
  namespace: qwen3-tpu
spec:
  deploymentMode: OMENative
  model:
    name: qwen3-0-6b
  runtime:
    name: srt-qwen3-tpu
  engine:
    minReplicas: 1
    maxReplicas: 1
    annotations:
      ome.io/tpu-slice-provisioning: "true"
    nodeSelector:
      cloud.google.com/gke-tpu-accelerator: tpu7x
      cloud.google.com/gke-tpu-topology: 2x2x1
    tolerations:
      - key: google.com/tpu
        operator: Exists
        effect: NoSchedule
    runner:
      name: ome-container
      resources:
        requests:
          google.com/tpu: 4
        limits:
          google.com/tpu: 4
```

- The annotation `ome.io/tpu-slice-provisioning: "true"`, in the component's `annotations`, opts the component's pods in. It reaches their pod template, where OME reads it. Each component opts in on its own, and any other value leaves provisioning off.
- The `nodeSelector` declares the shape with the labels from Step 1: a `2x2x1` slice of `tpu7x`, which is 4 chips on one host.
- The runner requests the slice's 4 chips, so the pod fills it exactly. OME rejects a shape the pods can't fill: see [Troubleshooting](#the-inferenceservice-records-componentreconcileerror).
- GKE taints TPU nodes with `google.com/tpu`, so keep the toleration unless the runtime already carries one.
- The `nodeSelector`, the toleration and the chip requests can also live in the runtime's `engineConfig`; the InferenceService's values win.

Create the InferenceService:

```bash
kubectl apply -f qwen3-tpu-isvc.yaml
```

```output
inferenceservice.ome.io/qwen3-tpu created
```

OME creates the InferenceReplica `qwen3-tpu-engine` for the engine. Its Instance has no pods yet, so `READY` is `0`:

```bash
kubectl get inferencereplica qwen3-tpu-engine -n qwen3-tpu
```

```output
NAME               COMPONENT   DESIRED   CURRENT   READY   AVAILABLE   AGE
qwen3-tpu-engine   engine      1         1         0       0           15s
```

## Step 3: Watch the Slice and the pod

OME created a Slice for Instance 0's pod. List OME's Slices with their provisioning state, the `Ready` condition's reason:

```bash
kubectl get slices.accelerator.gke.io -l ome.io/managed-by=OMENative \
  -o 'custom-columns=NAME:.metadata.name,TYPE:.spec.type,TOPOLOGY:.spec.topology,STATE:.status.conditions[?(@.type=="Ready")].reason'
```

```output
NAME                            TYPE    TOPOLOGY   STATE
qwen3-tpu-engine-0-0-8f3a21c7   tpu7x   2x2x1      ACTIVE
```

Your hash differs, and `STATE` is `<none>` until GKE reports one. While the state isn't one of the configured `readyStates`, OME withholds the pod: the Instance stays in `Creating` with the hold `CapacityProvisioning` in the `op hold` row of `kubectl ome instance status qwen3-tpu 0 --component engine -n qwen3-tpu`, and its last failure explains the wait, such as `slice qwen3-tpu-engine-0-0-8f3a21c7 is waiting for partition assignment`. The wait doesn't count against the Instance's [readiness deadline](set-instance-readiness-deadlines.md).

Once the Slice is ready, OME creates the pod. Wait for the InferenceService:

```bash
kubectl wait --for=condition=Ready inferenceservice/qwen3-tpu -n qwen3-tpu --timeout=30m
```

```output
inferenceservice.ome.io/qwen3-tpu condition met
```

The pod is confined to the Slice by a node selector on the slice-name label, which OME set for you:

```bash
kubectl get pod qwen3-tpu-engine-0-default-0 -n qwen3-tpu \
  -o jsonpath='{.spec.nodeSelector.cloud\.google\.com/gke-tpu-slice}'
```

```output
qwen3-tpu-engine-0-0-8f3a21c7
```

Send the engine a request as in [Serve a model on OMENative](serve-a-model-on-omenative.md#step-4-send-a-request).

## Multi-host slices

For a slice that spans hosts, give the engine a `leader` and a `worker`, as [Serve a multi-node model](serve-a-multi-node-model.md) shows, and declare the shape on both: the Instance's pods share one Slice. On `tpu7x` from Step 1, a `2x2x2` topology is 8 chips on two 4-chip hosts: select it on the leader and the worker, with `worker.size: 1` and 4 chips requested in each pod.

The pods of one Instance must fill their topology exactly, because GKE grants a slice whole and a TPU session must request exactly the slice's topology. OME checks that:

- every pod of the Instance selects the same accelerator and topology, and the topology is in the accelerator's `topologies`;
- every pod requests the same chip count under `chipResource`, summed over its containers from the limit, or the request when a container has no limit;
- a pod's chips fit on one host: the count divides `chipsPerHost`;
- together, the pods request exactly the topology's chips.

By default, an update surges: the new pods come up on a Slice of their own while the old pods keep theirs, so an update briefly needs the capacity of one more slice per updating Instance.

## Metrics

The manager reports provisioning on its metrics endpoint, labeled by `slice_type` and `topology`. The created, released, release-deferred and duration series of every configured type and topology start at zero when the manager starts. [Collect metrics](../operate-ome/metrics.md) shows how to scrape them.

| Metric | What it reports |
| --- | --- |
| `ome_tpu_slice_created_total` | Slices the controller created. |
| `ome_tpu_slice_released_total` | Slices the controller provisioned that the API server has removed, whoever deleted them. A Slice GKE holds behind a finalizer counts once GKE lets it go. Counted from the leader's watch. |
| `ome_tpu_slice_release_deferred_total` | Releases of Slices the controller provisioned that it kept back because pods of another workload hold chips on the Slice's hosts: see [A SliceReleaseDeferred event appears](#a-slicereleasedeferred-event-appears). The reconcile retries, and each deferred attempt counts. |
| `ome_tpu_slice_create_failures_total` | Creates that failed, with a `reason` label: the API server's status reason, such as `Forbidden` or `Invalid`; `OwnershipConflict` when another owner's Slice has the name; or `Unknown`. The reconcile retries, and each failed attempt counts. |
| `ome_tpu_slice_provision_duration_seconds` | A histogram of the seconds from creating a Slice to first seeing it ready. Only Slices the current leader created are timed. |
| `ome_tpu_slices` | A gauge of the Slices the controller provisioned that exist now, by `state`: `terminating`, `orphaned` when the InferenceReplica it was provisioned for no longer exists, `ready`, or `pending` in any other state, a failed one included. Reported by the leader. |

## Troubleshooting

### The InferenceService records ComponentReconcileError

When a change gives an opted-in Instance pods that can't fill a provisionable slice, the InferenceReplica admission webhook rejects OME's write of the engine's InferenceReplica, and the denial lands on the InferenceService as a `ComponentReconcileError` Warning event. Read it:

```bash
kubectl get events -n qwen3-tpu --field-selector reason=ComponentReconcileError \
  -o jsonpath='{range .items[*]}{.message}{"\n"}{end}'
```

The message ends with the webhook's reason, which starts with `pods cannot be placed on a provisioned slice`:

| The reason ends with | Fix |
| --- | --- |
| `topology 4x4x4 is not provisionable for accelerator "tpu7x"; provisionable: 2x2x1, 2x2x2` | Select a listed topology, or add this one to the accelerator's `topologies` in Step 1's values. |
| `accelerator "tpu9x" has provision-only nodes but no provisioning configuration` | Add the accelerator under `accelerators`, or select a configured one. |
| `pods select tpu7x 2x2x1 and tpu7x 2x2x2; the pods of one instance share one slice`, or `1 of 2 pods select topology 2x2x1; the pods of one instance share one slice` | Give the leader and the worker the same accelerator and topology selectors. |
| `pods request 4 and 2 chips; every pod on a slice must request the same count` | Request the same chip count in every pod of the Instance. |
| `2 pods x 4 chips do not fill topology 2x2x1: it is 4 chips on 1 hosts, which takes 1 pods` | Match the pod count and the chip requests to the topology, as [Multi-host slices](#multi-host-slices) describes. |
| `pod 0 selects slice "my-slice"; a provision-only pool's slice is provisioned for the pods` | Remove the slice-name label from your node selectors: OME sets it. |

The webhook rejects only a write that introduces the problem, and admits the InferenceReplica when it can't read the nodes. The controller then repeats the check before it creates pods: it withholds them and records a `SliceDemandInvalid` Warning event on the InferenceService, with the same reason.

### Pods wait for a slice

The Instance stays in `Creating` with no pods, and its operation holds on `CapacityProvisioning`, as in [Step 3](#step-3-watch-the-slice-and-the-pod). The Instance's last failure says what the Slice is waiting for, and `kubectl describe slices.accelerator.gke.io qwen3-tpu-engine-0-0-8f3a21c7` shows GKE's `Ready` condition with its message. The deadline clock is stopped either way, and what ends the wait depends on what the Slice waits for:

- A Slice waiting for its partition waits for capacity, which a new Slice would wait for too, so OME waits as long as it takes, with or without `slice.readyTimeout`: free capacity in the pool.
- A Slice that has its partition but doesn't reach a ready state is recycled after `slice.readyTimeout`, when you set it: see [A SliceReadyTimeout event appears](#a-slicereadytimeout-event-appears). Without it, OME waits as long as the Slice isn't ready: fix what GKE's `Ready` condition reports.

### A SliceReadyTimeout event appears

With `slice.readyTimeout` set in Step 1's values, a Slice that got its partition but has stayed out of a ready state for longer than the timeout is recycled once no pod holds it: OME withholds the pods, records the Warning event on the InferenceService, deletes the Slice and provisions the slot again. The new Slice gets a new partition, so a one-off Slice stuck short of ready heals on its own. The wait is measured from the `Ready` condition's last transition, or from the Slice's creation while GKE reports no condition. The delete itself waits while pods of another workload hold chips on the Slice's hosts: see [A SliceReleaseDeferred event appears](#a-slicereleasedeferred-event-appears). Read the event:

```bash
kubectl get events -n qwen3-tpu --field-selector reason=SliceReadyTimeout \
  -o jsonpath='{range .items[*]}{.message}{"\n"}{end}'
```

```output
OMENative component=engine instance=0 withheld: slice qwen3-tpu-engine-0-0-8f3a21c7 is ACTIVATING for 11m3s, longer than the 10m0s ready timeout; it is released and provisioned again once no pod holds it
```

A Slice still waiting for its partition is never timed out, however long it waits: a new Slice would wait for the same capacity. A Slice whose pods are already running is left alone. When every replacement runs past the timeout too, the recycling repeats until you fix what GKE's `Ready` condition reports.

### A SliceHostUnavailable event appears

A ready Slice that no pod holds has a node the pods can't be scheduled on: cordoned, not ready, or unreachable, and the pods don't tolerate the matching taint. OME withholds the pods, records the Warning event on the InferenceService, deletes the Slice and provisions the slot again, so a one-off broken host heals on its own. The delete itself waits while pods of another workload hold chips on the Slice's hosts: see [A SliceReleaseDeferred event appears](#a-slicereleasedeferred-event-appears). Read the event:

```bash
kubectl get events -n qwen3-tpu --field-selector reason=SliceHostUnavailable \
  -o jsonpath='{range .items[*]}{.message}{"\n"}{end}'
```

```output
OMENative component=engine instance=0 withheld: slice qwen3-tpu-engine-0-0-8f3a21c7 is ACTIVE, but node gke-tpu-pool-node-3 is cordoned; it is released and provisioned again once no pod holds it
```

A Slice whose pods are already running is left alone. When every replacement Slice comes up broken too, fix the node pool.

### A SliceReleaseDeferred event appears

OME has a Slice to release, after a scale-down, a shape change, a recycle or a deletion, but pods of another workload hold TPU chips on its hosts, so it keeps the Slice: deleting it would deactivate the partition under those pods. OME records the Warning event on the component's InferenceReplica and retries on each reconcile, and it releases the Slice once the pods are gone. Read the event:

```bash
kubectl get events -n qwen3-tpu --field-selector reason=SliceReleaseDeferred \
  -o jsonpath='{range .items[*]}{.message}{"\n"}{end}'
```

```output
TPU slice qwen3-tpu-engine-0-0-8f3a21c7 is kept: pods of other workloads hold chips on its hosts (tpu-batch/indexer-0); it is released once they are gone
```

A pod holds the Slice's hosts while it is bound to one of its nodes, requests chips under `chipResource`, isn't confined to this Slice by the slice-name node selector, and hasn't succeeded or failed. The message names each one as `{namespace}/{name}`: wait for them to finish, or remove them where they come from. Each deferred attempt counts on `ome_tpu_slice_release_deferred_total`: see [Metrics](#metrics).

### A SliceOwnershipConflict event appears

An object that isn't the component's InferenceReplica holds a Slice at the name OME needs, so OME withholds the slot's pods and records the Warning event. OME never adopts, modifies or deletes a Slice without its owner's `ome.io/slice-owner-uid` label. See whose it is with `kubectl get slices.accelerator.gke.io qwen3-tpu-engine-0-0-8f3a21c7 --show-labels`, then rename the InferenceService or remove the conflicting Slice where it came from.

### No Slices are created

The pods exist but stay `Pending`, or nothing happens at all, and `kubectl get slices.accelerator.gke.io -l ome.io/managed-by=OMENative` lists nothing. Check, in order:

1. The `tpuSliceProvisioning` entry is set: Step 1's ConfigMap check prints the block, not an empty result.
2. The manager found the Slice CRD when it started. A manager that didn't logs this, and provisions nothing until you restart it with `kubectl rollout restart deployment ome-controller-manager -n ome`:

    ```bash
    kubectl logs -n ome -l control-plane=ome-controller-manager --tail=-1 | grep "won't provision TPU slices"
    ```

    ```output
    The InferenceReplica controller won't provision TPU slices because the accelerator.gke.io/v1beta1 Slice CRD is not available; pods are created without slices until it is installed and the manager restarts.
    ```

3. The component's `annotations` carry `ome.io/tpu-slice-provisioning: "true"`, exactly.
4. A node with both the accelerator label's value and the provision-only label exists. Without one, OME treats the pool as static and creates the pods unconfined, which isn't an error: `kubectl get nodes -l cloud.google.com/gke-tpu-accelerator=tpu7x,cloud.google.com/gke-accelerator-topology-mode=PROVISION_ONLY` must list the pool's nodes.

### The manager doesn't start after the upgrade

`kubectl rollout status` keeps waiting, and the new pods' logs show `Failed to initialize TPU slice provisioning configuration`, with every field that is missing or invalid. Fix the values and upgrade again. [The manager rollout doesn't finish](../operate-ome/configure-the-controller.md#the-manager-rollout-doesnt-finish) explains how the Deployment behaves meanwhile.

## Clean up

Delete the InferenceService and its namespace. OME releases each Instance's Slices as part of removing it, so the deletion waits while pods of other workloads hold chips on their hosts (see [A SliceReleaseDeferred event appears](#a-slicereleasedeferred-event-appears)) and until GKE lets them go:

```bash
kubectl delete inferenceservice qwen3-tpu -n qwen3-tpu
kubectl delete namespace qwen3-tpu
```

```output
inferenceservice.ome.io "qwen3-tpu" deleted from qwen3-tpu namespace
namespace "qwen3-tpu" deleted
```

Check that no Slice of the engine remains:

```bash
kubectl get slices.accelerator.gke.io -l cloud.google.com/slice-owner-name=qwen3-tpu-engine
```

```output
No resources found
```

When a deleting InferenceReplica runs past its [teardown deadline](recover-stuck-deletions.md) with a release still failing, OME lets the deletion finish anyway and records a Warning that ends with `delete the slices labeled ome.io/slice-owner-uid={uid} by hand`, with the InferenceReplica's UID. Delete them with that label selector.

The model and the runtime stay. To delete the model, follow [Clean up](../../getting-started/serve-your-first-model.md#clean-up) in Serve your first model.

## Next steps

- [Serve a multi-node model](serve-a-multi-node-model.md): set up the leader and workers that share a multi-host slice.
- [Set Instance readiness deadlines](set-instance-readiness-deadlines.md): bound how long an Instance may take once its slice is ready.
- [Instances](../../concepts/omenative/instances.md): the phases, operations and holds this guide reads.
- [Collect metrics](../operate-ome/metrics.md): scrape the manager's metrics, the slice series included.
