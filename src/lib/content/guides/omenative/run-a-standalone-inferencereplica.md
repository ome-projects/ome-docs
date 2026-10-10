---
title: Run a standalone InferenceReplica
navLabel: Run a standalone replica
description: "Create an InferenceReplica directly, with no parent InferenceService: give it a model, a runtime or rendered pod templates, then scale, update and pause the pod set yourself."
since: v1.3
---

An [InferenceReplica](../../reference/api/ome.v1beta1.md#ome-io-v1beta1-InferenceReplica) is one pod set on [OMENative](../../concepts/omenative/overview.md): N [Instances](../../concepts/omenative/instances.md) of the same pod templates, with one revision history and one rollout. OME writes one for each OMENative component of an InferenceService. You can also create one directly, with no parent InferenceService: a standalone replica, whose spec, scaling and rollouts stay with you. Run one as a serving pool you manage yourself, or put an InferenceService's stable names and routes in front of it with [referenced replicas](../../concepts/serving/inference-services.md#referenced-replicas).

Here you create a standalone engine replica that serves a model, check what OME runs for it, and scale it.

<div class="prerequisites" markdown>

- OME v1.3 or later, installed with the `ome-resources` chart, and `kubectl`, with the rights to create namespaces and InferenceReplicas. A standalone replica is governed by RBAC. See [Install OME](../../getting-started/install.md).
- The ClusterBaseModel `llama-3-2-1b-instruct` in the `Ready` state, and the ClusterServingRuntime `vllm-llama-3-2-1b-instruct`. [A minimal InferenceService](../../concepts/serving/inference-services.md#a-minimal-inferenceservice) shows how to create both from the OME repository, with the model agent and the Hugging Face token the model needs.
- A node where the model is `Ready`, with a free NVIDIA GPU, 10 CPUs and 30 GiB of memory for each engine pod, and room for a second pod for Step 3 and for updates.

</div>

## One template source

A replica renders its pods from exactly one template source. The webhook rejects a spec that sets none, or `runners` next to a reference:

| Source | What renders |
| --- | --- |
| `spec.modelRef` | The pods that serve the model, rendered from the model and a runtime: the one `spec.runtimeRef` names, or the one OME [selects for the model](../../concepts/models/base-models.md#how-runtimes-match-the-model) when it's absent. The model's kind defaults to `ClusterBaseModel`; set `kind: BaseModel` for one in the replica's namespace. [Fine-tuned weights](../../concepts/models/fine-tuned-weights.md) and overlays render as they do on an InferenceService. |
| `spec.runtimeRef` alone | The runtime's piece for `spec.component` — its `engineConfig` for an engine replica — as it is, with no model mounted: no model volumes, no `MODEL_PATH` and no model-ready node selector. The kind defaults to `ClusterServingRuntime`; set `kind: ServingRuntime` for one in the replica's namespace. |
| `spec.runners` | Pod templates you rendered yourself, one per role: a single `default` runner with `size: 1`, or a `leader` (`size: 1`) and a `worker` (`size` ≥ 1) for a multi-pod Instance. OME treats each template as opaque: it stamps the Instance labels and the `OME_RUNNER` and `OME_LEADER_ADDRESS` variables, and mounts nothing. |

With `modelRef` or `runtimeRef`, the controller renders the pod templates in memory on every pass and never stores them in your spec. The live model and runtime render, so there's no [runtime pinning](../../concepts/runtimes/runtime-revisions.md): the webhook rejects `spec.runtimeRef.autoSync: false` and `spec.runtimeRef.revision`. The runtime's pod spec places the pods — node selector, affinity and resources come from it — so a second accelerator pool is a second runtime.

## Step 1: Create the replica

Create a namespace:

```bash
kubectl create namespace llama-demo
```

```output
namespace/llama-demo created
```

Save this InferenceReplica as `llama-pool.yaml`. It serves the model with the runtime OME selects for it, as one engine Instance:

```yaml title="llama-pool.yaml"
apiVersion: ome.io/v1beta1
kind: InferenceReplica
metadata:
  name: llama-pool
  namespace: llama-demo
spec:
  component: engine
  modelRef:
    name: llama-3-2-1b-instruct
  replicas: 1
```

`spec.component` is the role the pod set fills — `engine`, `decoder` or `router` — and is immutable: to change it, recreate the replica. `spec.replicas` counts Instances. The name becomes the prefix of the replica's pod and Service names, so it must match `[a-z]([-a-z0-9]*[a-z0-9])?`, and it must not collide with an InferenceService: see [What the webhook rejects](#what-the-webhook-rejects).

Create the replica:

```bash
kubectl apply -f llama-pool.yaml
```

```output
inferencereplica.ome.io/llama-pool created
```

Wait for it to be ready. Pulling the vLLM image and loading the model can take several minutes:

```bash
kubectl wait --for=condition=Ready inferencereplica/llama-pool -n llama-demo --timeout=30m
```

```output
inferencereplica.ome.io/llama-pool condition met
```

## Step 2: Check what OME runs

List the InferenceReplicas, `irep` for short. The columns count Instances:

```bash
kubectl get inferencereplicas -n llama-demo
```

```output
NAME         COMPONENT   DESIRED   CURRENT   READY   AVAILABLE   AGE
llama-pool   engine      1         1         1       1           8m
```

[`kubectl ome get inferencereplicas`](../../reference/kubectl-ome/get.md#inferencereplicas-columns) shows more, including a `PARENT` column that's `-` for a standalone replica.

The pods are named `{replica}-{component}-{index}-{runner}-{ordinal}`, and their `ome.io/inferenceservice` label carries the replica's own name, since there's no InferenceService. List the pod with its `ome.io/serving` readiness gate, which is `True` while the pod is in rotation:

```bash
kubectl get pods -n llama-demo -l ome.io/inferenceservice=llama-pool \
  -o 'custom-columns=NAME:.metadata.name,SERVING:.status.conditions[?(@.type=="ome.io/serving")].status'
```

```output
NAME                            SERVING
llama-pool-engine-0-default-0   True
```

OME also creates the component's headless Service, which the rotation and the pods' DNS use, and PodGroups for multi-pod Instances when the PodGroup CRD is installed, for [gang scheduling](../../concepts/serving/gang-scheduling.md):

```bash
kubectl get services -n llama-demo -o custom-columns=NAME:.metadata.name,TYPE:.spec.type,CLUSTER-IP:.spec.clusterIP
```

```output
NAME                         TYPE        CLUSTER-IP
llama-pool-engine-headless   ClusterIP   None
```

That's all: the stable Service, the route, the PodDisruptionBudget and the autoscaler of an InferenceService component come from the InferenceService, which a standalone replica doesn't have. To get stable names and routing in front of the pods, [front the replica with an InferenceService](../../concepts/serving/inference-services.md#referenced-replicas); to reach them directly, create your own Service over the `ome.io/inferenceservice=llama-pool` label.

## Step 3: Scale the replica

Whatever targets the replica's scale subresource writes `spec.replicas`. Scale it by hand:

```bash
kubectl scale inferencereplica llama-pool -n llama-demo --replicas=2
```

```output
inferencereplica.ome.io/llama-pool scaled
```

The new Instance takes the lowest free index, here 1:

```bash
kubectl get inferencereplica llama-pool -n llama-demo
```

```output
NAME         COMPONENT   DESIRED   CURRENT   READY   AVAILABLE   AGE
llama-pool   engine      2         2         2       2           12m
```

!!! note "0 runs one Instance"
    A standalone replica doesn't scale to zero: `spec.replicas` unset or `0` runs one Instance.

To autoscale, point your own HorizontalPodAutoscaler or KEDA ScaledObject at the scale subresource, with a `scaleTargetRef` of `apiVersion: ome.io/v1beta1`, `kind: InferenceReplica` and the replica's name, as [Bring your own autoscaler](../scale-and-migrate/bring-your-own-autoscaler.md) shows for an InferenceService component. `status.labelSelector` serves the HPA's pod lookup. Only the InferenceService controller turns `spec.autoscaler` into an HPA or a ScaledObject, so on a standalone replica the webhook rejects the `HPA` and `KEDA` classes; an `External` scaler targets the scale subresource directly, so that class is accepted.

## How updates roll out

A change that alters the rendered pod templates mints a new revision, and the replica rolls its Instances to it by `spec.lifecycle.updateStrategy` — [SurgeThenDrain](../../concepts/architecture/omenative-update-strategies.md#surgethendrain) when unset, which brings each Instance's new pods up and serving before the old ones drain. Because a `modelRef` or `runtimeRef` replica renders from the live objects, a change to the referenced model or runtime rolls the replica too: OME watches them, and there's no pinning. On a `runners` replica, you apply the new templates yourself.

You hold and steer a rollout with the same fields the InferenceService controller writes on the replicas it projects:

- `spec.paused: true` stops updates, new Instances and migrations; `spec.pauseMode: Freeze` also stops Instance repair. In-flight operations resume on unpause.
- `spec.pacing.partition` keeps that many of the lowest-indexed Instances on their current revision; it must not exceed `spec.replicas`.
- `spec.pacing.rollbackToRevision` rolls every Instance back to a named ControllerRevision of this replica.
- `spec.minReadySeconds` makes a newly `Ready` pod wait before it counts as `Available`, which [paces drains and promotions](../roll-out-changes/pace-rollouts-with-min-ready-seconds.md). On a replica it's this top-level field: the webhook rejects the nested `spec.lifecycle.minReadySeconds`, which nothing reads here.

Failed Instances work as on any OMENative component: set the `ome.io/reset-instances` annotation on the replica to [rebuild them](reset-failed-instances.md) once you've fixed the cause. Manual [Instance migration](../../concepts/omenative/migration-and-transient-scale.md) isn't available: it surges through a per-revision Service that only an InferenceService creates, so the webhook rejects a migration request on a standalone replica.

## What the webhook rejects

OME's validating webhook checks every InferenceReplica create and update. It tells the forms apart by the InferenceService controller owner reference and `spec.parentRef`: a replica with either is projected, and rejects your spec writes with a message that ends in `edit the InferenceService instead`. On a standalone replica, which has neither, it rejects:

| You set | Why |
| --- | --- |
| `spec.parentRef` | Only the InferenceService controller sets it, on the replicas it projects. Omit it; it's also immutable. |
| Annotations copied from a projected replica | Only the InferenceService controller writes the `ome.io/composed-fields` annotation: drop a copied one. Drop a copied `ome.io/controller-write: "true"` too: on a cluster with no controller identity configured in `inferenceservice-config`, it marks the write as the controller's, and the controller creates only the replicas it projects. |
| `spec.placementExecution` or `spec.placementReplicaLimit` | Written only by the InferenceService controller. |
| `spec.runners` together with `spec.modelRef` or `spec.runtimeRef`, or none of the three | A replica renders from [exactly one template source](#one-template-source). |
| `spec.runtimeRef.autoSync: false` or `spec.runtimeRef.revision` | A replica renders the live runtime and keeps no snapshot of it, so a pin isn't honored. |
| A `spec.runtimeRef` whose runtime has no piece for `spec.component` | The replica couldn't render, and no retry resolves it. A runtime that doesn't exist yet is admitted: the replica reports `RuntimeNotFound` and renders when it appears. |
| A `spec.runtimeRef` whose runtime [inherits from](../../concepts/runtimes/runtime-inheritance.md) a runtime that doesn't exist | The runtime can't be resolved. |
| `spec.autoscaler.class: HPA` or `KEDA` | Only the InferenceService controller creates a scaler from the block. Target the scale subresource with your own HPA or ScaledObject, or use `External`. |
| `spec.lifecycle.minReadySeconds` | Not read on a replica. Set `spec.minReadySeconds`. |
| An `ome.io/migration-request-v1-*` annotation | Manual migration needs the per-revision Service that only an InferenceService creates. |
| A name that doesn't match `[a-z]([-a-z0-9]*[a-z0-9])?` | The name is the prefix of the replica's pod and Service names. |
| A name an InferenceService's replicas would collide with | The webhook rejects the name of an existing InferenceService, and a name that is one plus an `-engine`, `-decoder` or `-router` suffix, because that InferenceService's projected replicas would share pod and Service names with yours. |
| A change to `spec.component` | Immutable for every writer. Recreate the replica to change the role. |

The webhook also checks every writer's shape: an autoscaler block that doesn't validate, `spec.pacing.partition` above `spec.replicas`, and a runner template whose `volumeMounts` name a volume the pod doesn't declare. Deleting a replica is never gated.

## Troubleshooting

### The replica is not Ready

When the replica has no pod templates it can run, its `Ready` condition is `False` and a Warning event carries the same reason and message. Read them with `kubectl describe inferencereplica llama-pool -n llama-demo`:

| Reason | What it says |
| --- | --- |
| `TemplateSourceMissing` | The spec sets neither `runners` nor `modelRef` or `runtimeRef`. |
| `ModelNotFound` | No BaseModel or ClusterBaseModel carries the referenced name, or the model isn't ready yet. |
| `ModelDisabled` | The referenced model is disabled. |
| `RuntimeNotFound` | The named runtime doesn't exist, in the replica's namespace or at cluster scope. |
| `RuntimeDisabled` | The named runtime is disabled. |
| `RuntimeSelectionFailed` | No runtime serves the model, or the named runtime can't serve it. |
| `RuntimePieceMissing` | The runtime no longer declares the piece for `spec.component`, such as an `engineConfig` dropped after you created the replica. |
| `RenderFailed` | The render of the pod templates failed, or the controller isn't configured to render from references. |

OME leaves the replica's existing pods as they are, and watches the referenced model and runtime: fix the referenced object, and the replica renders without you touching it. For an Instance that fails after rendering, such as an image that can't be pulled, see [The Instance fails](serve-a-model-on-omenative.md#the-instance-fails): a standalone replica retries and holds revisions the same way.

### A RuntimeCompatibilityAdvisory warning

When `spec.runtimeRef` names a runtime that doesn't declare support for the model in `spec.modelRef`, the replica still renders, and OME records a Warning event that starts with `Runtime <runtime> does not declare support for model <model>` and ends with `proceeding because the runtime was named explicitly`. Serving may still fail at load time: check the runtime's `supportedModelFormats` against the model, or drop `spec.runtimeRef` and let OME select a supporting runtime.

## Clean up

Delete the replica and the namespace. OME drains the Instances before it deletes their pods:

```bash
kubectl delete inferencereplica llama-pool -n llama-demo
kubectl delete namespace llama-demo
```

```output
inferencereplica.ome.io "llama-pool" deleted from llama-demo namespace
namespace "llama-demo" deleted
```

If a pod stays `Terminating`, such as on a node that's gone, see [Recover stuck deletions](recover-stuck-deletions.md). The model and the runtime are cluster-scoped and stay.

## Next steps

- [Referenced replicas](../../concepts/serving/inference-services.md#referenced-replicas): front standalone replicas with an InferenceService's stable names, routing and status.
- [Instances](../../concepts/omenative/instances.md): the phases, operations and incarnations of the Instances in your replica.
- [OMENative update strategies](../../concepts/architecture/omenative-update-strategies.md): how each strategy replaces an Instance's pods, and when to pick which.
- [Bring your own autoscaler](../scale-and-migrate/bring-your-own-autoscaler.md): an HPA on the scale subresource that OME publishes.
- [`kubectl ome get`](../../reference/kubectl-ome/get.md#inferencereplicas-columns): list replicas with their parent, revisions and lifecycle condition.
