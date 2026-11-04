---
title: Gang scheduling
description: With the scheduler-plugins PodGroup CRD installed, OME creates a PodGroup for each multi-pod OMENative Instance, so a gang-aware scheduler places its leader and workers together or not at all.
since: v1.3
---

Gang scheduling places all the pods of a multi-pod [Instance](../omenative/instances.md) together, or none of them. An [OMENative](../omenative/overview.md) Instance with a leader and workers serves only when all its pods run, and gang scheduling stops some of them from holding GPUs while the rest wait.

When the [scheduler-plugins](https://github.com/kubernetes-sigs/scheduler-plugins) PodGroup CRD is installed, OME creates a PodGroup for each multi-pod Instance of an OMENative engine or decoder, and none for RawDeployment or [MultiNode](../architecture/deployment-modes.md#the-deployment-modes) (deprecated) components. A gang-aware scheduler reads the PodGroups: the alpha [OME scheduler](../scheduling/ome-scheduler.md) or the scheduler-plugins Coscheduling plugin. The default Kubernetes scheduler ignores them and places pods one at a time.

## Install the PodGroup CRD {#when-the-podgroup-crd-is-missing}

No OME chart installs the PodGroup CRD. Install it from scheduler-plugins v0.34.7, the release OME is built against. If you run the OME scheduler, install the CRD version from [Use the OME scheduler](../../guides/operate-ome/ome-scheduler.md) instead. Then restart the controller, which looks for the CRD only when it starts. This example assumes OME runs in the `ome` namespace:

```bash
kubectl apply -f https://raw.githubusercontent.com/kubernetes-sigs/scheduler-plugins/v0.34.7/config/crd/bases/scheduling.x-k8s.io_podgroups.yaml
kubectl rollout restart deployment ome-controller-manager -n ome
```

```output
customresourcedefinition.apiextensions.k8s.io/podgroups.scheduling.x-k8s.io created
deployment.apps/ome-controller-manager restarted
```

## Bring a gang-aware scheduler

A PodGroup holds a gang together only when the scheduler that places the pods reads it. The pods use the scheduler that the [runtime](../runtimes/serving-runtimes.md) or the [InferenceService](inference-services.md) names, and the default scheduler otherwise. The default scheduler can bind part of an Instance while the rest waits, and the bound pods hold their GPUs with nothing serving. Use one of these:

| Scheduler | What it does | `schedulerName` |
| --- | --- | --- |
| [OME scheduler](../scheduling/ome-scheduler.md) | Places each gang in one topology domain, all or nothing. Alpha, and needs Kubernetes 1.35. | `ome-scheduler` |
| scheduler-plugins Coscheduling | Places each gang all or nothing, in your default scheduler or as a second scheduler. | Unset in your default scheduler, or `scheduler-plugins-scheduler` from the scheduler-plugins Helm chart |

The OME scheduler also needs a topology key: the component's `topologyKey`, or its chart's `scheduler.plugin.topologyKey` for every gang. Without either, the gang's pods stay `Pending`. [Use the OME scheduler](../../guides/operate-ome/ome-scheduler.md) installs the scheduler and sets the key.

Set `spec.schedulerName` in the runtime to use one scheduler for all its components:

```yaml
spec:
  schedulerName: ome-scheduler
```

A `schedulerName` in an InferenceService component, or in a runtime component config such as `engineConfig`, overrides it for that component's leader and workers:

```yaml
spec:
  engine:
    schedulerName: ome-scheduler
```

When the pods of a component with PodGroups use the default scheduler, OME records a `MaybeNoGangScheduler` Warning event on the InferenceService. If you added Coscheduling to your default scheduler, ignore it.

## One PodGroup per multi-pod Instance

OME creates an Instance's PodGroup before its pods, and names it `<isvc>-<component>-<index>`. Its `minMember` is the Instance's pod count: the leader plus `worker.size` workers. Its `scheduleTimeoutSeconds` comes from the [gang schedule timeout](#bound-the-gang-schedule-timeout). When the component sets `topologyKey`, the PodGroup carries it in the `ome.io/topology-key` annotation. Once the Instance has pods, the annotation [follows them](#the-topology-annotation-follows-the-instances-pods), not the spec. OME labels the Instance's pods with `scheduling.x-k8s.io/pod-group`, set to the PodGroup's name, which gang-aware schedulers read.

For example, the InferenceService in [Serve a multi-node model](../../guides/omenative/serve-a-multi-node-model.md) runs one engine Instance of a leader and one worker, so OME creates the PodGroup `qwen3-multi-node-engine-0`. Without its owner reference and status, it looks like this:

```yaml
apiVersion: scheduling.x-k8s.io/v1alpha1
kind: PodGroup
metadata:
  name: qwen3-multi-node-engine-0
  namespace: qwen3-multi-node
  labels:
    component: engine
    ome.io/inferenceservice: qwen3-multi-node
    ome.io/instance-index: "0"
    ome.io/managed-by: OMENative
spec:
  minMember: 2
  scheduleTimeoutSeconds: 600
```

List the InferenceService's PodGroups:

```bash
kubectl get podgroups -n qwen3-multi-node -l ome.io/inferenceservice=qwen3-multi-node \
  -o custom-columns=NAME:.metadata.name,MIN-MEMBER:.spec.minMember,TIMEOUT:.spec.scheduleTimeoutSeconds
```

```output
NAME                        MIN-MEMBER   TIMEOUT
qwen3-multi-node-engine-0   2            600
```

List the Instance's pods by their PodGroup label:

```bash
kubectl get pods -n qwen3-multi-node -l scheduling.x-k8s.io/pod-group=qwen3-multi-node-engine-0 -o name
```

```output
pod/qwen3-multi-node-engine-0-leader-0
pod/qwen3-multi-node-engine-0-worker-0
```

### The topology annotation follows the Instance's pods

While an Instance's pods exist, its PodGroup's `ome.io/topology-key` annotation describes those pods, not the newest spec. A pod's affinity is immutable: each worker carries a required affinity that keeps it in its leader's domain under the key the pods were created with. The annotation is the gang's contract with the scheduler, and the [OME scheduler](../scheduling/ome-scheduler.md) releases a gang's domain to pick again when it changes, which its [metrics](../../reference/scheduling/ome-scheduler-configuration.md#metrics) count as a `topology_replan`. Retargeting a live gang's annotation would have the scheduler place replacement pods by the new key while the gang's other pods stay pinned to the old one: a split gang.

So a `topologyKey` edit never retargets a live gang. OME records a new revision, and the new key lands as the [rollout](../architecture/omenative-update-strategies.md) replaces each Instance's pods:

- Under `SurgeThenDrain`, the default, the replacement gang is a new Instance with its own PodGroup, which carries the new key from creation. The old Instance's PodGroup keeps the old key until the Instance drains, and is deleted with it.
- Under a strategy that recreates the pods, the Instance's PodGroup advances to the new key once its pods are gone, before OME creates the replacements.
- A repair stays put: OME refills a lost pod of a live gang with the key the gang runs, so the new pod can land with its peers.

OME proves a live gang's key from the worker-to-leader affinity it generated on the gang's pods, and otherwise keeps the annotation the PodGroup already carries. Affinity you wrote yourself never sets the annotation, since it can express an unrelated relationship. When the component sets `topologyKey` but neither source proves the key of a gang that still has pods, say because the annotation was removed by hand while only the leader runs, OME fails the component's reconcile rather than guess, with an error in the controller's log that says `cannot prove active topology`. It reconciles again when the annotation is restored, or the gang's pods are gone.

## Bound the gang schedule timeout

A gang-aware scheduler holds a gang's placed pods until the whole gang fits, for up to the PodGroup's `scheduleTimeoutSeconds`. At the timeout, it frees their resources for other pods and tries the gang again later.

OME sets `scheduleTimeoutSeconds` to the component's [ready timeout](../../guides/omenative/set-instance-readiness-deadlines.md), `instanceReadyTimeout`, clamped between the `min` and `max` of the controller's `gangScheduleTimeout` setting. The `ome-resources` chart sets both:

```yaml
ome:
  controller:
    lifecycle:
      instanceReadyTimeout: 30m
      gangScheduleTimeout:
        min: 60s
        max: 600s
```

With these values, the 30-minute ready timeout becomes 600 seconds, as in the PodGroup above.

| Ready timeout | `gangScheduleTimeout` | `scheduleTimeoutSeconds` |
| --- | --- | --- |
| Between `min` and `max` | Set | The ready timeout |
| Above `max` | Set | `max` |
| Below `min`, or unset | Set | `min` |
| Set | Unset | The ready timeout: 1800 in the kustomize install |
| Unset | Unset | Unset: the scheduler's default applies |

The scheduler's default is `defaultPermitTimeoutSeconds` for the OME scheduler, 600 in its chart, or `permitWaitingTimeSeconds` for Coscheduling. `gangScheduleTimeout` needs both `min` and `max`, positive, with `min` no larger than `max`. OME treats an invalid one as unset.

[Change the cluster defaults](../../guides/omenative/set-instance-readiness-deadlines.md#change-the-cluster-defaults) shows how to change these settings. OME updates existing PodGroups to the new timeout in place, unlike [the topology annotation](#the-topology-annotation-follows-the-instances-pods), which a live gang's pods hold.

## A gang held to a full domain can fail sooner

Placed members pin their gang. When some of an Instance's pods run in one topology domain and the scheduler reports that the domain can't take the rest — for example, one of its nodes stopped reporting — freeing capacity in another domain doesn't help: each worker's [affinity](#the-topology-annotation-follows-the-instances-pods) keeps it in its leader's domain. Only removing the placed members frees the gang. Ending the attempt does exactly that, and the rebuilt gang can land in a domain that fits.

By default, such a member still waits out the controller's [`unschedulableGracePeriod`](../../guides/omenative/set-instance-readiness-deadlines.md), 15 minutes with the chart, like any other unschedulable pod. The `lifecycle.gangDomainFullGracePeriod` cluster setting, which the chart leaves unset, ends the attempt sooner. Set it with the other [readiness deadlines](../../guides/omenative/set-instance-readiness-deadlines.md#change-the-cluster-defaults), with a unit — OME treats a value it can't parse, or one that isn't above zero, as unset:

```yaml
ome:
  controller:
    lifecycle:
      unschedulableGracePeriod: 15m
      gangDomainFullGracePeriod: 5m
```

Three rules bound the shorter window:

- **Only the scheduler's own reports trigger it.** OME matches the message against the [OME scheduler](../scheduling/ome-scheduler.md)'s reports that placed members hold the gang where it can't complete: the `pinned domain` and `adopted domain` [scheduling events](../../reference/scheduling/ome-scheduler-configuration.md#scheduling-events), and the split-gang report `already has members in multiple topology domains`. The phrases are fixed in OME's code, the same on every cluster. Another scheduler's messages don't match them, so under Coscheduling such a hold waits on `unschedulableGracePeriod`.
- **Terminating pods push it out.** While any pod of the attempt is still terminating, only `unschedulableGracePeriod` applies: a member on its way out may be what fills the domain, and its room frees as it goes.
- **The clock doesn't start over.** Both windows run from the same start: the attempt's placement wait, when it began before the unplaceable pod's own `PodScheduled` transition. A member OME rebuilds after a node loss keeps the attempt's clock, so its first refusal can end the attempt.

When both settings are set, the shorter applies, and `gangDomainFullGracePeriod` also works with `unschedulableGracePeriod` unset. Past the window, OME fails the Instance as the cluster's problem, never the revision's: the failure reason is `Unschedulable`, with the scheduler's message. [When a limit runs out](../../guides/omenative/set-instance-readiness-deadlines.md#when-a-deadline-runs-out) says what OME does next for each operation.

## What you see

| What you see | What it means |
| --- | --- |
| The component's `GangSchedulingUnavailable` condition is `True`, with the reason `PodGroupCRDNotInstalled` | The CRD is missing, or was when the controller started. The default scheduler may place part of an Instance, and the OME scheduler keeps its pods `Pending`. [Install the CRD](#when-the-podgroup-crd-is-missing). |
| An `InstanceFailed` Warning event with the scheduler's message | A pod stayed unschedulable longer than the controller's [`unschedulableGracePeriod`](../../guides/omenative/set-instance-readiness-deadlines.md), 15 minutes with the chart — or longer than `gangDomainFullGracePeriod`, when [the gang was held to a full domain](#a-gang-held-to-a-full-domain-can-fail-sooner). |
| An `InstanceFailed` Warning event that names `PodGroupOwnershipConflict` | Another owner controls a PodGroup with the Instance's PodGroup name. OME fails the Instance and creates none of its pods. |
| A `PodGroupReset` Warning event on the InferenceService | A pod of a running gang failed, so the scheduler-plugins controller marked the PodGroup `Failed`. OME replaced it without failing the Instance. The Instance's [restart policy](../omenative/instance-restart-policy.md) decides what happens next. |

The condition is on the component's InferenceReplica, and in the InferenceService's `status.components.<component>.lifecycle.conditions`. When it's `False`, its reason is `GangSchedulingAvailable`. Read its reason for the engine, here without the CRD:

```bash
kubectl get inferenceservice qwen3-multi-node -n qwen3-multi-node \
  -o jsonpath='{.status.components.engine.lifecycle.conditions[?(@.type=="GangSchedulingUnavailable")].reason}'
```

```output
PodGroupCRDNotInstalled
```

## Next steps

- [The OME scheduler](../scheduling/ome-scheduler.md): how OME's alpha scheduler places each gang in one topology domain.
- [Use the OME scheduler](../../guides/operate-ome/ome-scheduler.md): install the OME scheduler and point your pods at it.
- [Serve a multi-node model](../../guides/omenative/serve-a-multi-node-model.md): run an engine whose Instances are a leader and workers.
- [Set Instance readiness deadlines](../../guides/omenative/set-instance-readiness-deadlines.md): the ready timeout that the gang schedule timeout comes from.
