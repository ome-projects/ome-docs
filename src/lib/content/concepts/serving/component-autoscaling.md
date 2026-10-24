---
title: Component autoscaling
description: Each InferenceService component can carry its own autoscaler block, which picks HPA, KEDA, External or None and the metrics or triggers to scale on.
since: v1.3
---

An `autoscaler` block lets each component of an [InferenceService](inference-services.md) scale on the signal that suits it, from CPU utilization to the requests waiting in vLLM's queue. OME can create a HorizontalPodAutoscaler or a [KEDA](https://keda.sh) ScaledObject for it, leave it to an autoscaler that you run, or hold it at a fixed count. A component with no autoscaling settings gets a HorizontalPodAutoscaler that targets 80% average CPU utilization. The `autoscaler` block and the autoscaling fields in status are alpha, and so are [AutoscalerPolicy](autoscaler-policy.md) and [`spec.scalingPolicy`](#proportional-scaling). Their fields and behavior can change between releases, but the block needs no feature flag.

The examples use the `llama-demo` namespace and the `llama-3-2-1b-instruct` InferenceService from [A minimal InferenceService](inference-services.md#a-minimal-inferenceservice), and assume OME runs in the `ome` namespace.

## Classes

Put the block in `spec.engine`, `spec.decoder` or `spec.router`. Its `class` decides what OME creates, and who sets the replica count:

| `class` | Use it to | What OME creates | Who sets the replica count |
| --- | --- | --- | --- |
| `HPA` | Scale on CPU, memory or another metric from the Kubernetes metrics APIs. | A HorizontalPodAutoscaler, `llama-3-2-1b-instruct-engine`. | The HorizontalPodAutoscaler. |
| `KEDA` | Scale on a Prometheus query or another KEDA trigger, or scale a RawDeployment component to zero. | A ScaledObject, `scaledobject-llama-3-2-1b-instruct-engine`. | KEDA, through a HorizontalPodAutoscaler that it controls. |
| `External` | Scale with an autoscaler that you run. | Nothing. | Your autoscaler. |
| `None` | Run a fixed number of replicas. | Nothing. | OME, which holds the count at `minReplicas`, even after a `kubectl scale`. A [proportional follower](#proportional-scaling) is held at its target instead. |

## HPA

This version of `isvc.yaml` scales the engine between 1 and 4 replicas to keep its average CPU utilization at 70%:

```yaml title="isvc.yaml"
apiVersion: ome.io/v1beta1
kind: InferenceService
metadata:
  name: llama-3-2-1b-instruct
  namespace: llama-demo
spec:
  model:
    name: llama-3-2-1b-instruct
  engine:
    minReplicas: 1
    maxReplicas: 4
    autoscaler:
      class: HPA
      hpa:
        metrics:
          - type: Resource
            resource:
              name: cpu
              target:
                type: Utilization
                averageUtilization: 70
```

OME copies `hpa.metrics` and `hpa.behavior` into the HorizontalPodAutoscaler as they are. Without `metrics`, the default CPU target applies.

Apply the file:

```bash
kubectl apply -f isvc.yaml
```

```output
Warning: Runtime vllm-llama-3-2-1b-instruct will be auto-selected for model llama-3-2-1b-instruct
inferenceservice.ome.io/llama-3-2-1b-instruct configured
```

Check the engine's autoscaling in the InferenceService's status:

```bash
kubectl get inferenceservice llama-3-2-1b-instruct -n llama-demo \
  -o custom-columns='CLASS:.status.components.engine.autoscaler.class,MANAGED-BY:.status.components.engine.autoscaler.managedBy,SOURCE:.status.components.engine.autoscaler.specSource,CURRENT:.status.components.engine.autoscaler.currentReplicas,DESIRED:.status.components.engine.autoscaler.desiredReplicas,KIND:.status.components.engine.scaleTargetRef.kind,TARGET:.status.components.engine.scaleTargetRef.name'
```

```output
CLASS   MANAGED-BY   SOURCE   CURRENT   DESIRED   KIND         TARGET
HPA     ome          isvc     1         1         Deployment   llama-3-2-1b-instruct-engine
```

OME's HorizontalPodAutoscaler now scales the engine's Deployment, which runs 1 replica. [Read the result in status](#read-the-result-in-status) explains each column.

## KEDA

`class: KEDA` needs [KEDA](https://keda.sh) in the cluster. Without it, the InferenceService's ingress and status stop updating, as [KEDA isn't installed](../../guides/scale-and-migrate/scale-to-zero-with-keda.md#keda-isnt-installed) describes. The controller looks for KEDA when it starts, so if you install KEDA after OME, restart the controller:

```bash
kubectl rollout restart deployment ome-controller-manager -n ome
```

```output
deployment.apps/ome-controller-manager restarted
```

This version of `isvc.yaml` scales the engine on the requests waiting in vLLM's queue. The trigger reads them from `ome-prometheus`, the Prometheus server that the `ome-resources` chart deploys:

```yaml title="isvc.yaml"
apiVersion: ome.io/v1beta1
kind: InferenceService
metadata:
  name: llama-3-2-1b-instruct
  namespace: llama-demo
spec:
  model:
    name: llama-3-2-1b-instruct
  engine:
    minReplicas: 1
    maxReplicas: 4
    autoscaler:
      class: KEDA
      keda:
        pollingInterval: 15
        cooldownPeriod: 300
        triggers:
          - type: prometheus
            metricType: AverageValue
            metadata:
              serverAddress: http://ome-prometheus.ome.svc:9090
              query: 'sum(vllm:num_requests_waiting{namespace="llama-demo",inferenceservice="llama-3-2-1b-instruct",component="engine"})'
              threshold: "5"
```

The query sums the waiting requests across the engine's pods. With `metricType: AverageValue`, KEDA divides the sum by the replica count, so it adds replicas to keep about 5 waiting requests per replica. [Collect metrics](../../guides/operate-ome/metrics.md) describes the Prometheus server and the labels it adds.

The engine already has a HorizontalPodAutoscaler from the HPA example. Apply the file anyway: the switch takes one step, because OME hands that HorizontalPodAutoscaler over to KEDA, as [Switch between HPA and KEDA](#switch-between-hpa-and-keda) describes.

OME copies the `keda` fields into the ScaledObject as they are:

| Field | What it holds |
| --- | --- |
| `triggers` | Required. The KEDA triggers to scale on. |
| `pollingInterval`, `cooldownPeriod`, `fallback` | The ScaledObject settings of the same names. |
| `idleReplicaCount` | The replica count while no trigger is active. It must be lower than `minReplicas`. |
| `advanced` | The ScaledObject's `advanced` settings. A `horizontalPodAutoscalerConfig.name` you set takes effect once KEDA controls the HorizontalPodAutoscaler: during [a switch from HPA](#switch-between-hpa-and-keda), OME names the handed-over HorizontalPodAutoscaler first. |

### Switch between HPA and KEDA

Change the component's `class` and apply the file: a switch takes one step, whether the block comes from the InferenceService, its runtime or an [AutoscalerPolicy](autoscaler-policy.md), and the component keeps a working autoscaler throughout. KEDA's admission webhook rejects a ScaledObject while another HorizontalPodAutoscaler scales the same workload, and two HorizontalPodAutoscalers on one workload fight over its replica count, so OME hands its HorizontalPodAutoscaler over instead of running both:

- Moving to `KEDA`, OME creates the ScaledObject with the HorizontalPodAutoscaler's name in `advanced.horizontalPodAutoscalerConfig.name` and KEDA's `scaledobject.keda.sh/transfer-hpa-ownership` annotation, and KEDA takes that HorizontalPodAutoscaler over in place: the same object keeps scaling the component, now under KEDA's control. That covers a running component with no autoscaling settings, which has OME's default HorizontalPodAutoscaler.
- Moving back to `HPA`, OME first checks with a server-side dry run that its replacement HorizontalPodAutoscaler would be accepted, then deletes the ScaledObject and every HorizontalPodAutoscaler KEDA created for it, and creates its own only after they are gone, so two HorizontalPodAutoscalers never scale the component at once.

A switch that waits for KEDA, or for a deletion to finish, isn't a failure: the controller logs `Autoscaler switch in progress` and retries, and the InferenceService's status keeps updating. The `AutoscalerSwitched` condition, next to the scaler's own conditions in `status.components.<component>.autoscaler.conditions`, reports the progress:

| Status | Reason | When |
| --- | --- | --- |
| `True` | `HPAControlledByKEDA` | KEDA controls the HorizontalPodAutoscaler through the ScaledObject, and reports the ScaledObject `Ready`. |
| `True` | `ScaledObjectRemoved` | The ScaledObject and the HorizontalPodAutoscalers KEDA created for it are gone, and OME runs its own HorizontalPodAutoscaler. |
| `False` | `WaitingForKEDA` | KEDA hasn't taken the HorizontalPodAutoscaler over yet. |
| `False` | `ScaledObjectNotReady` | KEDA controls the HorizontalPodAutoscaler, but doesn't report the ScaledObject `Ready`. |
| `False` | `WaitingForScaledObjectDeletion` | The ScaledObject still exists, usually while KEDA finishes deleting it. |
| `False` | `WaitingForHPADeletion` | A HorizontalPodAutoscaler that KEDA created still exists. |

OME writes the condition only for a component that switches, so a component that has always run one class doesn't carry it. After you apply the KEDA version of `isvc.yaml`, read it:

```bash
kubectl get inferenceservice llama-3-2-1b-instruct -n llama-demo \
  -o custom-columns='SWITCHED:.status.components.engine.autoscaler.conditions[?(@.type=="AutoscalerSwitched")].status,REASON:.status.components.engine.autoscaler.conditions[?(@.type=="AutoscalerSwitched")].reason'
```

```output
SWITCHED   REASON
True       HPAControlledByKEDA
```

The HorizontalPodAutoscaler `llama-3-2-1b-instruct-engine` from the HPA example still exists, and its controller is now the ScaledObject:

```bash
kubectl get hpa llama-3-2-1b-instruct-engine -n llama-demo \
  -o jsonpath='{.metadata.ownerReferences[?(@.controller==true)].kind}{"\n"}'
```

```output
ScaledObject
```

OME never takes over or deletes a scaler it didn't create: a HorizontalPodAutoscaler or ScaledObject of yours under a name OME uses makes the switch fail with `is not controlled by expected owner`, as [External and None](#external-and-none) describes.

## External and None

Use `External` to scale a component with an autoscaler that you run, pointed at the object in `status.components.<component>.scaleTargetRef`. [Bring your own autoscaler](../../guides/scale-and-migrate/bring-your-own-autoscaler.md) shows how. A switch to `External` or `None` deletes the autoscaler that OME created. With `External`, OME keeps the current replica count until your autoscaler changes it.

If you already run a HorizontalPodAutoscaler or ScaledObject under the name that OME would use, `HPA` and `KEDA` fail with an error that contains `is not controlled by expected owner`. Delete that object, or keep it with `class: External`.

## What the autoscaler scales

The autoscaler's target depends on the component's [deployment mode](../architecture/deployment-modes.md#the-deployment-modes):

| Mode | What the autoscaler scales |
| --- | --- |
| `RawDeployment` | The Deployment `llama-3-2-1b-instruct-engine`. A replica is a pod. |
| [`OMENative`](../omenative/overview.md) | The InferenceReplica `llama-3-2-1b-instruct-engine`, which holds the component's replicas. Each replica is an [Instance](../omenative/instances.md#what-an-instance-is): one pod, or a leader pod and its workers. |
| `MultiNode` (deprecated) | Nothing, though its status can still show a class. |

!!! warning "LeaderWorkerSet is deprecated"
    The LeaderWorkerSet-backed `MultiNode` mode is deprecated. Use OMENative for multi-node serving instead: see [Move from LeaderWorkerSet to OMENative](../../guides/omenative/move-from-leaderworkerset.md).

On OMENative, a `Resource` or `Pods` metric averages over every pod in every Instance, workers included. [Request a transient scale](../../guides/scale-and-migrate/request-a-transient-scale.md#scaling-alongside-an-autoscaler) says how long a manual change to an OMENative component's count lasts under each class.

## Replica bounds

`minReplicas` and `maxReplicas` sit next to `autoscaler` in the component, and OME passes them to the autoscaler that it creates. A bound that the InferenceService and its runtime leave unset comes from the [replica defaults](../../guides/operate-ome/set-replica-defaults.md). The `ome-resources` chart sets them to 1 to 3 replicas for the engine and the decoder, and 1 to 2 for the router. OME raises a default maximum that's below `minReplicas` to match.

Keep `minReplicas` at or below `maxReplicas`, wherever each is set. Admission checks the pair only when the InferenceService sets both, so a `minReplicas` above the runtime's `maxReplicas` gets through. OME then raises an OMENative component's maximum to match. On a RawDeployment component, the controller logs `minReplicas must not exceed maxReplicas`, and the autoscaler and the InferenceService's status stop updating. [Set replica defaults](../../guides/operate-ome/set-replica-defaults.md#reconciles-fail-with-minreplicas-must-not-exceed-maxreplicas) shows how to find the error.

A RawDeployment component scales to zero with `minReplicas: 0` and a `class: KEDA` block in the InferenceService itself, as [Scale to zero with KEDA](../../guides/scale-and-migrate/scale-to-zero-with-keda.md) shows. An OMENative component keeps at least one Instance, even with `minReplicas: 0`: see [OMENative components can't scale to zero](../../guides/scale-and-migrate/scale-to-zero-with-keda.md#omenative-components-cant-scale-to-zero).

## Which setting wins

OME takes a component's autoscaling block from the first of these places that has one:

| Order | Where OME looks | `specSource` in status |
| --- | --- | --- |
| 1 | The component's `autoscaler` in the InferenceService. | `isvc` |
| 2 | The [AutoscalerPolicy](autoscaler-policy.md) that the component's `autoscalerPolicyRef` names. | `policy` |
| 3 | The `autoscaler` in the runtime's `engineConfig`, `decoderConfig` or `routerConfig`. | `runtime` |
| 4 | The [legacy annotations](../../reference/api/labels-and-annotations.md#legacy-autoscaling), for a RawDeployment component. | `legacy` |
| 5 | The default: `class: HPA` at 80% average CPU utilization. | `default` |

The block comes whole from one place, so a `class: HPA` block without `hpa` gets the default CPU target, even when the runtime lists metrics.

AutoscalerPolicy is off by default: [Turn on the feature](autoscaler-policy.md#turn-on-the-feature) shows how to turn it on. If OME can't use a policy reference, the component keeps its last autoscaler, instead of falling through to the runtime or the default: see [Fail-closed behavior](autoscaler-policy.md#fail-closed-behavior).

## Proportional scaling

`spec.scalingPolicy`, which is alpha, sets how the components scale relative to each other. With `mode: Independent`, the default, each component scales on its own, as the rest of this page describes. With `mode: Proportional`, the components you list as followers stop scaling on their own and track the anchor component's desired Instance count at fixed ratios: scale the anchor, and the followers follow. The schema's third mode, `Pinned`, is reserved: the webhook rejects it with `spec.scalingPolicy.mode "Pinned" is accepted by the API but not implemented; use "Independent" or omit the policy (ScalingModeNotImplemented)`.

The policy comes from the InferenceService, or else from its runtime's [`scalingPolicy`](../runtimes/serving-runtimes.md#scaling-policy), whole, never merged. Proportional scaling works only on components that run on [OMENative](../omenative/overview.md) — set `spec.deploymentMode: OMENative`, or the component's `ome.io/deploymentMode` annotation — and only on components the InferenceService owns, not [referenced replicas](inference-services.md#referenced-replicas).

This version of `llama-pd.yaml`, the prefill-decode InferenceService from [Serve a prefill-decode model](../../guides/omenative/serve-a-prefill-decode-model.md), scales the engine on CPU and makes the decoder follow it at half its count:

```yaml title="llama-pd.yaml"
apiVersion: ome.io/v1beta1
kind: InferenceService
metadata:
  name: llama-pd
  namespace: llama-demo
spec:
  deploymentMode: OMENative
  model:
    name: llama-3-2-1b-instruct
  runtime:
    name: srt-llama-3-2-1b-instruct-pd
  scalingPolicy:
    mode: Proportional
    proportional:
      anchor: engine
      ratios:
        decoder: "0.5"
  engine:
    minReplicas: 2
    maxReplicas: 6
    autoscaler:
      class: HPA
      hpa:
        metrics:
          - type: Resource
            resource:
              name: cpu
              target:
                type: Utilization
                averageUtilization: 70
  decoder:
    minReplicas: 1
    maxReplicas: 4
  router:
    minReplicas: 1
    maxReplicas: 1
```

- `anchor` names the component whose desired count drives the followers, `engine` by default. The anchor scales like any component: here through its own HorizontalPodAutoscaler, but KEDA, an AutoscalerPolicy or the default CPU target work too.
- `ratios` maps each follower to its ratio. A follower's target is the anchor's desired count times the ratio, rounded up: at 4 engine Instances, the decoder's target is 2, and at 5 it's 3. A ratio of `"1"` means parity; a ratio is above 0 and at most 1000, with at most three decimal places.
- A component the ratios leave out, like the router here, scales independently, as usual.

A follower's own [replica bounds](#replica-bounds) still hold: its `minReplicas` is a floor, at least one Instance, and its `maxReplicas` — wherever it's set: the InferenceService, the runtime or the [replica defaults](../../guides/operate-ome/set-replica-defaults.md) — is a ceiling. A moved target isn't an error: the status reports the follower as clamped, the `Ready` condition's message names the bound, as in `decoder target is bounded to 4 Instances by its maxReplicas (the ratio asks for 5)`, and OME records a Warning `ScalingTargetClamped` event with that message when the bound first moves the follower to that count. When neither the follower nor the anchor has a `maxReplicas`, nothing caps the follower's count, and OME records a Warning `ScalingFollowerUnbounded` event: `decoder follows engine with no maxReplicas on either side; set maxReplicas on one of them to bound the decoder count`.

OME alone writes a follower's count:

- A follower with no `autoscaler` block runs with `class: None` instead of the [default HorizontalPodAutoscaler](#which-setting-wins), and OME holds its count at the target, not at `minReplicas`. A `kubectl scale` on the follower's InferenceReplica is overwritten, and [`kubectl ome scale`](../../reference/kubectl-ome/scale.md) refuses every component of the InferenceService while the mode isn't `Independent`.
- The webhook rejects a Proportional policy whose follower sets `autoscalerPolicyRef`, with `decoder follower cannot reference AutoscalerPolicy "queue-depth" when scaling is Proportional: remove the reference or remove decoder from ratios`, or an `autoscaler` with any class but `None`, with `decoder follower autoscaler must have class None when scaling is Proportional`.
- When a competing autoscaler reaches a follower anyway — the InferenceService's policy lists a follower whose block comes from the runtime, say — OME holds every follower at the count it has, sets the policy's `Ready` condition to `False` with reason `FollowerAutoscalerConflict`, and records a Warning event of the same name, such as `decoder follower has a competing KEDA autoscaler from the serving runtime; follower counts are held`.

The webhook also rejects a `proportional` block with mode `Independent`, an empty `ratios`, a ratio that names the anchor or a component the object doesn't declare, and `spec.replicaRefs` with `Proportional scaling requires InferenceService-owned replicas`. A declared deployment mode that isn't OMENative is rejected with a message like `Proportional scaling requires OMENative decoder, not deployment mode "RawDeployment"`; a component whose mode only resolves to something else at reconcile holds with `InvalidPolicy` instead. The same checks run on a runtime's `scalingPolicy`, against `engineConfig`, `decoderConfig` and `routerConfig`. The webhook validates the policy when you create the object and when an update sets or changes it, or changes a component's autoscaler settings or deployment mode, so an unchanged stored policy never blocks an unrelated update.

Whenever the InferenceService or its runtime sets `spec.scalingPolicy`, in every mode, OME reports it in `status.scalingPolicy`:

| Field | What it says |
| --- | --- |
| `mode`, `source` | The mode in effect, and where the policy came from: `isvc` or `runtime`. |
| `anchor`, `anchorDesired` | The anchor, and its desired Instance count. |
| `followers` | Per follower: `component`, `ratio`, `target`, `current` — the count the follower's InferenceReplica asks for now — and `clamped`, `true` when a bound moved the target. |
| `conditions` | The `Ready` condition. |

The `Ready` condition says whether the followers carry the counts the policy asks for:

| Status | Reason | When |
| --- | --- | --- |
| `True` | `Applied` | Every follower's desired count matches its target. |
| `True` | `Independent` | The mode is `Independent`: the components scale independently. |
| `False` | `FollowerPending` | A follower doesn't carry its target yet; the next pass writes it. |
| `False` | `FollowerUnavailable` | A follower's InferenceReplica isn't projected yet. |
| `False` | `AnchorUnavailable` | The anchor's InferenceReplica is missing, or has no desired count yet. The followers keep their counts. |
| `False` | `FollowerAutoscalerConflict` | A follower has a competing autoscaler, which holds every follower. |
| `False` | `InvalidPolicy` | The policy can't be applied: the anchor or a follower isn't declared or isn't on OMENative, or a ratio is invalid. The message says which; a follower it doesn't name keeps following. |
| `False` | `ModeNotImplemented` | A stored policy carries a mode no controller coordinates. |

Apply the file and wait for the InferenceService to be ready, as the guide shows. Then read the policy's status:

```bash
kubectl get inferenceservice llama-pd -n llama-demo \
  -o custom-columns='MODE:.status.scalingPolicy.mode,ANCHOR:.status.scalingPolicy.anchor,DESIRED:.status.scalingPolicy.anchorDesired,FOLLOWER:.status.scalingPolicy.followers[0].component,TARGET:.status.scalingPolicy.followers[0].target,CURRENT:.status.scalingPolicy.followers[0].current,READY:.status.scalingPolicy.conditions[?(@.type=="Ready")].reason'
```

```output
MODE           ANCHOR   DESIRED   FOLLOWER   TARGET   CURRENT   READY
Proportional   engine   4         decoder    2        2         Applied
```

The engine's HorizontalPodAutoscaler has scaled it to 4 Instances, and the decoder follows at 2. Change `ratios`, or let the anchor scale, and the followers move with the next pass.

## Read the result in status

OME reports each component's autoscaling in `status.components.<component>.autoscaler`:

| Field | What it says |
| --- | --- |
| `class` | The class in effect. |
| `managedBy` | Who scales the component: `ome` for `HPA` and `KEDA`, `external` for `External`, and `none` for `None`. |
| `specSource` | Where the block came from: see [Which setting wins](#which-setting-wins). |
| `currentReplicas`, `desiredReplicas` | The autoscaler's current and desired replica counts, when `managedBy` is `ome`. Left out when 0. |
| `lastScaleTime` | When the HorizontalPodAutoscaler last scaled, or when a KEDA trigger was last active. |
| `conditions` | The conditions of the HorizontalPodAutoscaler or the ScaledObject, plus OME's own: [`AutoscalerSwitched`](#switch-between-hpa-and-keda) on a component that switched between `HPA` and `KEDA`, and `AutoscalerResolved` on one with a policy reference. |
| `policy`, `shadowedPolicyRef` | Which AutoscalerPolicy supplied the block, or which one an inline block outranks: see [Conditions and status](autoscaler-policy.md#conditions-and-status). |

`status.components.<component>.scaleTargetRef` names the scaled object by `apiVersion`, `kind` and `name`. [`kubectl ome autoscale`](../../reference/kubectl-ome/autoscale.md) shows these fields for every component, and explains where each setting came from.

## What admission rejects

The InferenceService webhook rejects the following, with the component and the reason in its message, as in `engine: KedaTriggersRequired: class=keda requires at least 1 trigger`:

| The message contains | Cause |
| --- | --- |
| `AutoscalerClassUnknown` | An `autoscaler` block without `class`. |
| `KedaTriggersRequired` | `class: KEDA` without a `keda` block. |
| `HPAMetricMalformed` | An `hpa.metrics` entry that lacks a `type`, or the field for its `type`, as in `pods` for `type: Pods`. |
| `KedaIdleBelowMin` | A `keda.idleReplicaCount` at or above the `minReplicas` set in the InferenceService. |
| `InvalidScaleToZero` | `minReplicas: 0` without a `class: KEDA` block in the InferenceService itself. |
| `must be <= maxReplicas` | A `minReplicas` above `maxReplicas`, both set in the InferenceService. |
| `AutoscalerAnnotationConflict` | An `ome.io/autoscalerClass` annotation on the InferenceService while a component sets an `autoscaler` block. |
| `AutoscalerPolicyFeatureDisabled` | An `autoscalerPolicyRef` while AutoscalerPolicy is off. |

The ServingRuntime and ClusterServingRuntime webhooks check the blocks in `engineConfig`, `decoderConfig` and `routerConfig` the same way, and reject an `autoscalerPolicyRef` in a runtime. [Legacy autoscaling](../../reference/api/labels-and-annotations.md#legacy-autoscaling) lists the legacy annotations' values, and [Attach a policy to an InferenceService](autoscaler-policy.md#attach-a-policy-to-an-inferenceservice) covers policy references. What the webhook rejects in `spec.scalingPolicy` is under [Proportional scaling](#proportional-scaling).

## Upgrade from v1.2.2 {#upgrade-from-v1-2-2}

v1.3 removes the component's `scaleMetric` and `scaleTarget`, and the InferenceService's `spec.kedaConfig`, as [Removed in v1.3](../../reference/api/ome.v1beta1.md#removed-in-v1-3) lists. Move them into an `autoscaler` block:

| v1.2.2 | v1.3 |
| --- | --- |
| A `cpu` or `memory` `scaleMetric`, with its `scaleTarget` | A `Resource` entry in `hpa.metrics`. |
| `promServerAddress`, `customPromQuery` and `scalingThreshold` in `spec.kedaConfig` | `serverAddress`, `query` and `threshold` in the `metadata` of a `prometheus` trigger in `keda.triggers`. |
| `authenticationRef` and `authModes` in `spec.kedaConfig` | The trigger's `authenticationRef`, and `authModes` in its `metadata`. |
| `scalingOperator` in `spec.kedaConfig` | Drop it: the `prometheus` trigger has no such setting. |
| The `ome.io/autoscalerClass: keda` annotation, which stops working in v1.3 | A `class: KEDA` block. Remove the annotation in the same change, because the webhook rejects the two together. |

v1.3 copies `query` as it is, so replace a `%s` from `customPromQuery` with the component's name, such as `llama-3-2-1b-instruct-engine`.

## Next steps

- [Autoscaler policy](autoscaler-policy.md): share one set of autoscaling settings across InferenceServices.
- [Scale to zero with KEDA](../../guides/scale-and-migrate/scale-to-zero-with-keda.md): scale an idle RawDeployment component down to no replicas.
- [Bring your own autoscaler](../../guides/scale-and-migrate/bring-your-own-autoscaler.md): scale a component with an autoscaler that you run, through `class: External`.
- [Request a transient scale](../../guides/scale-and-migrate/request-a-transient-scale.md): ask once for a different replica count on an OMENative component.
- [kubectl ome autoscale](../../reference/kubectl-ome/autoscale.md): show the autoscaling in effect for each component.
- [ComponentAutoscaler](../../reference/api/ome.v1beta1.md#ome-io-v1beta1-ComponentAutoscaler): every field of the `autoscaler` block, in the API reference.
- [Deployment modes and OMENative](../architecture/deployment-modes.md): how each mode runs a component.
