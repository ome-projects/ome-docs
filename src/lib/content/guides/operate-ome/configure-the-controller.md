---
title: Configure the controller
description: "Tune the OME controller manager's command-line flags, including reconcile concurrency, leader election and runtime-revision garbage collection."
---

Set the OME controller manager's command-line flags to speed up changes to your InferenceServices, keep its leader stable during API server slowdowns, or apply ConfigMap edits sooner. [Controller manager flags](../../reference/operate-ome/controller-manager-flags.md) lists them all, with their defaults. The chart can also [add environment variables and files to the manager pod](#add-environment-variables-and-files-to-the-manager-pod), such as proxy settings and a private CA bundle.

<div class="prerequisites" markdown>

- OME installed with the `ome-crd` and `ome-resources` charts, as the releases `ome-crd` and `ome` in the namespace `ome`. See [Install OME](../../getting-started/install.md).
- Helm and `kubectl` access to the cluster.
- The values file that you installed `ome-resources` with. If you don't have it, `helm get values ome -n ome` prints the values that you set.
- `jq`, to read the JSON that OME keeps in ConfigMaps.

</div>

## Step 1: Set a flag

Set a flag with its `ome-resources` Helm value when it has one, and with a patch to the Deployment when it doesn't. The Helm value column of [Controller manager flags](../../reference/operate-ome/controller-manager-flags.md) tells you which.

### With a Helm value {since=v1.3}

On v1.2.2, the chart has no values for the manager's flags: set them with a patch, as in [Without a Helm value](#flags-without-a-helm-value).

The chart sets many flags from values under `ome.controller`. This example raises the number of InferenceServices that the manager reconciles in parallel from the chart's 4 to 8:

```yaml title="values.yaml"
ome:
  controller:
    inferenceServiceMaxConcurrentReconciles: 8
```

Keep your other values in the file: `helm upgrade` resets the values that aren't in the file to the chart's defaults. Upgrade with the chart version that you already run, which `helm list -n ome` shows in its `CHART` column:

```bash
helm upgrade --install ome oci://ghcr.io/moirai-internal/charts/ome-resources \
  --namespace ome --version 1.3.0 -f values.yaml
```

Helm reports that the release `ome` has been upgraded. The chart passes the value to the `manager` container of the `ome-controller-manager` Deployment as `--inferenceservice-max-concurrent-reconciles=8`, and the Deployment replaces its pods one at a time. Wait for the rollout:

```bash
kubectl rollout status deployment ome-controller-manager -n ome
```

When the new pods are running, the command ends with:

```output
deployment "ome-controller-manager" successfully rolled out
```

### Without a Helm value {#flags-without-a-helm-value}

To set a flag that has no Helm value, patch the Deployment. This patch appends two flags to the arguments of the `manager` container, the pod's first container:

```bash
kubectl patch deployment ome-controller-manager -n ome --type=json -p='[
  {"op": "add", "path": "/spec/template/spec/containers/0/args/-", "value": "--runtime-revision-retention=5"},
  {"op": "add", "path": "/spec/template/spec/containers/0/args/-", "value": "--runtime-revision-grace-period=72h"}
]'
```

```output
deployment.apps/ome-controller-manager patched
```

The pods roll, as with a Helm value. When a flag appears twice, the manager uses the last one, so a patch can also override a flag that the chart passes, such as `--zap-encoder`.

!!! warning "`helm upgrade` can remove patched flags"
    A `helm upgrade` of the release can set the arguments back to what the chart renders, even when no value changed, and the pods then roll without your flags. After an upgrade, check the flags as in [Step 2](#step-2-check-the-running-flags), and patch again if they're gone. The same goes for environment variables and mounts that you patch in: add those with values instead, as in [Add environment variables and files to the manager pod](#add-environment-variables-and-files-to-the-manager-pod).

## Step 2: Check the running flags

Print the arguments of the `manager` container, one per line:

```bash
kubectl get deployment ome-controller-manager -n ome \
  -o jsonpath='{range .spec.template.spec.containers[?(@.name=="manager")].args[*]}{@}{"\n"}{end}'
```

With the Helm value from Step 1, and the chart's other values at their defaults, the output is:

```output
--metrics-bind-address=:8080
--leader-elect
--webhook
--zap-encoder=console
--inferenceservice-max-concurrent-reconciles=8
--inferencereplica-max-concurrent-reconciles=4
--config-cache-ttl=30s
--accelerator-resources=nvidia.com/gpu,google.com/tpu
--kube-api-qps=100
--kube-api-burst=200
--leader-elect-lease-duration=60s
--leader-elect-renew-deadline=40s
--leader-elect-retry-period=8s
```

The first four arguments are always there, and patched flags come last. Flags missing from the list run with their defaults.

## Tune leader election

The chart runs three replicas of the manager, set by `ome.controller.replicaCount`. They elect a leader through the Lease `ome-controller-manager-leader-lock`, and only the leader runs the controllers. All replicas serve the admission webhooks, the metrics endpoint and the health probes. See which pod leads:

```bash
kubectl get lease ome-controller-manager-leader-lock -n ome -o jsonpath='{.spec.holderIdentity}{"\n"}'
```

```output
ome-controller-manager-7c9d5b8f4-kx2mq_3f6a0c52-9e1b-4d7a-8c2e-5b1f0a9d4e37
```

The part before the `_` is the leader's pod name.

The leader renews the Lease every retry period. A leader that can't renew it within the renew deadline logs `Failed to start manager` with the error `leader election lost` and exits, and Kubernetes restarts the container. A standby takes over once the Lease has gone unrenewed for the lease duration. A leader that stops keeps its Lease until it expires, so when a rollout replaces the leader, reconciling pauses until then.

### Lease timing {since=v1.3}

The chart sets a lease duration of 60 seconds, a renew deadline of 40 seconds and a retry period of 8 seconds. When leaders restart with `leader election lost` during API server slowdowns, raise the three values together:

```yaml title="values.yaml"
ome:
  controller:
    leaderElection:
      leaseDuration: "90s"
      renewDeadline: "60s"
      retryPeriod: "12s"
```

Keep the lease duration longer than the renew deadline, and the renew deadline longer than 1.2 times the retry period. Otherwise the manager logs `Invalid leader election timing` when it starts, and exits. Longer values slow failover: a standby waits up to the lease duration before it takes over.

## Tune reconcile throughput {since=v1.3}

Two limits set how fast the manager works through changes. The worker counts set how many InferenceServices and InferenceReplicas it reconciles in parallel: 4 each with the chart. The rate limit caps the requests that each of the manager's API clients sends to the API server: 100 a second, with bursts of 200.

When you run many InferenceServices and changes take long to reach them, raise the worker counts. A reconcile sends several requests, so raise the rate limit with them:

```yaml title="values.yaml"
ome:
  controller:
    inferenceServiceMaxConcurrentReconciles: 8
    inferenceReplicaMaxConcurrentReconciles: 8
    kubeAPIQPS: 200
    kubeAPIBurst: 400
```

You don't have to set `kubeAPIQPS` and `kubeAPIBurst` together: with one value alone, the other keeps the chart's default, and the manager starts either way. But a `0` doesn't remove a limit: the chart then leaves the flag out, and the manager falls back to 20 requests a second, or a burst of 30 — far below the chart's values.

### Tell whether the manager is throttled

When the rate limit holds a request back for more than a second, the manager logs `Waited before sending request`:

```bash
kubectl logs -n ome -l control-plane=ome-controller-manager --tail=-1 | grep 'Waited before sending request'
```

The lines give the delay, and the request's verb and URL. An empty result means that no request waited that long. When the lines keep appearing, raise `kubeAPIQPS` and `kubeAPIBurst`.

To tell whether the workers keep up, read the metrics of the `inferenceservice` and `inferencereplica` controllers, filtered by the leader's `pod` label. When `controller_runtime_active_workers` stays at `controller_runtime_max_concurrent_reconciles` and `workqueue_depth` keeps growing, raise the worker counts, and the rate limit with them. See [Collect metrics](metrics.md).

## Change ConfigMap settings {#tune-the-config-cache}

Much of OME's configuration lives in the `inferenceservice-config` ConfigMap in the manager's namespace, which the chart writes from its values. To change a setting, set its Helm value and upgrade, as in [Step 1](#with-a-helm-value). The upgrade restarts the manager, and the new leader reconciles all InferenceServices with the new setting.

If you edit the ConfigMap with `kubectl` instead, the next `helm upgrade` can undo the edit. The controllers read the edit within the TTL of their config cache, 30 seconds by default. They apply it to an object the next time they reconcile that object: the edit alone doesn't start a reconcile. To apply it to all InferenceServices at once, restart the manager:

```bash
kubectl rollout restart deployment ome-controller-manager -n ome
```

```output
deployment.apps/ome-controller-manager restarted
```

The manager reads these settings only when it starts, so a `kubectl` edit to one of them needs a restart:

- the `omeAgent`, `multicluster`, `omenativeStatus`, `eventRecorder` and `tpuSliceProvisioning` entries;
- `enableGatewayAPI` in the `ingress` entry;
- `scaleUpPodBatchSize`, `scaleDownPodBatchSize`, `scaleDownRequeueInterval` and `repairBatchSize` in the `lifecycle` entry;
- `maxConcurrency` and `cacheTTL` in the `canaryAnalysis` entry;
- `maxPinnedPlanBytes` in the `rollout` entry;
- `preflight` in the `autoscalerPolicy` entry.

### Set the cache TTL {since=v1.3}

Set the TTL with `ome.controller.configCacheTTL`, as a quoted string such as `"10s"`. Lower it when 30 seconds is too long to wait for a `kubectl` edit. Raise it when the manager's reads of the ConfigMap are a noticeable part of your API server traffic.

`"0"` turns the cache off: all reads go to the API server, and OME rechecks the metric providers that policies name only when something else changes. Prefer a small positive value. Keep the quotes: for an unquoted `0`, the chart leaves out the flag, and the manager keeps its 30-second default.

## Tune the per-object event budget

The manager bounds the Kubernetes Events that it records about any one object. For each object and event type (Normal or Warning), it keeps a bucket of `burstSize` events, which regains one event every `refillInterval`, and it silently drops an event whose bucket is empty. Every emission counts, including a repeat that only raises the count of an event the object already shows. So when new events about an InferenceService stop appearing during a busy rollout, with no error anywhere, the InferenceService has spent its budget: the manager keeps working, and its events return as the bucket refills.

The chart sets the budget with `ome.controller.eventRecorder`, which it writes to the `eventRecorder` entry of the `inferenceservice-config` ConfigMap: a burst of 400 events, refilled one every 5 seconds. Every recorder in the manager uses the same budget, so it also bounds the events about InferenceReplicas, runtimes and OME's other objects, each with its own buckets. It doesn't touch events from other processes, such as the kubelet's and the scheduler's events about pods.

The manager itself has no default: for a field that the entry leaves out, it keeps the default of client-go, its Kubernetes client library — a burst of 25 events, or a refill of one event every five minutes. That's little for an [OMENative](../../concepts/omenative/overview.md) component: OME records an event on the InferenceService when it creates an Instance, when the Instance becomes ready, and when an update of an Instance starts and completes, so one rollout of a component with a few Instances spends the 25, and restarts and repairs draw on the same bucket. Keep both fields set, and raise them when events about one InferenceService still stop during busy periods:

```yaml title="values.yaml"
ome:
  controller:
    eventRecorder:
      burstSize: 800
      refillInterval: "2s"
```

Upgrade with your values file, as in [Step 1](#with-a-helm-value), and check the rendered entry:

```bash
kubectl get configmap inferenceservice-config -n ome -o jsonpath='{.data.eventRecorder}{"\n"}'
```

```output
{"burstSize":800,"refillInterval":"2s"}
```

The manager reads the entry once, when it starts, and the upgrade's restart applies it; a `kubectl` edit to the ConfigMap needs a restart, as [Change ConfigMap settings](#tune-the-config-cache) describes. Both fields must be positive: with a zero or negative `burstSize`, or a `refillInterval` that isn't a positive duration such as `"2s"`, the manager exits at startup. To hand a field back to client-go's default, set it to `null`, and the chart leaves it out of the entry.

## Pace OMENative scale-ups and scale-downs

When an [OMENative](../../concepts/omenative/overview.md) component scales, the manager creates and deletes its pods in bounded batches, one batch per reconcile pass, so one large change doesn't flood the API server or tear through a component at once. Three settings under `ome.controller.lifecycle`, which the chart writes to the `lifecycle` entry of the `inferenceservice-config` ConfigMap, set the budgets:

| Setting | Chart default | What it does |
| --- | --- | --- |
| `scaleUpPodBatchSize` | `100` | Caps the pods that one pass creates for a component. |
| `scaleDownPodBatchSize` | `100` | Caps the delete work that one pass drives for a component: a pod count, or a percentage of the component's current pods, such as `"10%"`. |
| `scaleDownRequeueInterval` | `"5s"` | How often the manager rechecks a component whose deletes are in flight. |

A batch is made of whole [Instances](../../concepts/omenative/instances.md): a pass creates all of an Instance's missing pods together and deletes all of its pods together, and a leader and its workers count as one Instance. A budget can't wedge on an Instance larger than itself: the first Instance of a batch may exceed the budget, and then proceeds alone. Instances past the budget wait for the following passes. The entry's fourth batch setting, `repairBatchSize`, paces crash-loop repairs instead: see [The crash-loop repair](../../concepts/omenative/instance-restart-policy.md#the-crash-loop-repair).

On a scale-down, an Instance costs its remaining pods, live and `Terminating` alike, and `1` when its pods are already gone but its cleanup still runs. A percentage is resolved against the component's footprint — the summed cost of its Instances — on every pass, rounded up and never below 1: `"10%"` bounds a pass to about a tenth of the component however large it grows, and a small component still makes progress. The batch's pods leave the component's Services first and are deleted once the Services stop routing to them, and the next batch starts when the current one is done, so a scale-down that you widen mid-flight finishes the deletes it already owns before it takes more Instances. The budget also caps the leftover [PodGroups](../../concepts/serving/gang-scheduling.md) that one pass deletes when a component is torn down. While deletes are in flight, the manager rechecks the component every `scaleDownRequeueInterval`; watch events on the pods and the other objects it waits on drive progress too, so the interval is a backstop, not a delay.

Write a pod count as a number and a percentage as a quoted string:

```yaml title="values.yaml"
ome:
  controller:
    lifecycle:
      scaleUpPodBatchSize: 50
      scaleDownPodBatchSize: "10%"
```

The `lifecycle` values that you don't set keep the chart's defaults. Upgrade with your values file, as in [Step 1](#with-a-helm-value), and check the rendered entry — with `jq`, since the entry holds other settings too:

```bash
kubectl get configmap inferenceservice-config -n ome -o jsonpath='{.data.lifecycle}' \
  | jq '{scaleUpPodBatchSize, scaleDownPodBatchSize, scaleDownRequeueInterval}'
```

```output
{
  "scaleUpPodBatchSize": 50,
  "scaleDownPodBatchSize": "10%",
  "scaleDownRequeueInterval": "5s"
}
```

The manager reads the entry once, when it starts, and the upgrade's restart applies it; a `kubectl` edit to the ConfigMap needs a restart, as [Change ConfigMap settings](#tune-the-config-cache) describes. While a scale-down runs, the leader logs one `OMENative scale-down wave` line per pass:

```bash
kubectl logs -n ome -l control-plane=ome-controller-manager --tail=-1 | grep 'OMENative scale-down wave'
```

The lines give the configured and the resolved budget (`configuredPodBudget` and `podBudget`), the component's footprint (`podFootprint`), and the Instances the wave drives (`activeInstances`) and holds back (`deferredInstances`). The manager also publishes these signals as metrics: see [Watch a scale-down in the metrics](#watch-a-scale-down-in-the-metrics).

Both batch sizes must be at least 1. The percentage form is digits and a `%`, from `"1%"` to `"100%"`: no sign, no spaces, no decimals — and a quoted `"100"` fails where a plain `100` works, since the manager reads every string as a percentage. The interval must be a positive duration such as `"5s"`. On any other value, the manager exits at startup with `Failed to initialize lifecycle scale configuration`, whose error names the value and the rule, such as `validate lifecycle.scaleDownPodBatchSize: invalid lifecycle.scaleDownPodBatchSize "101%" must be between 1% and 100%`, and the rollout doesn't finish: see [Troubleshooting](#the-manager-rollout-doesnt-finish). To remove a bound instead, set its field to `null`, and the chart leaves it out of the entry: without a batch size, one pass creates or deletes everything at once, and without the interval, the manager doesn't poll deletes in flight — watch events, and the exact deadlines of the [force-delete escalation and the teardown deadline](../omenative/recover-stuck-deletions.md), still wake it.

### Watch a scale-down in the metrics

If Prometheus scrapes the manager, as [Collect metrics](metrics.md) sets up, five series report the same scale-down signals, with history the log lines don't keep. Only the leader records them, so filter by its `pod` label. Cost counts as above: an Instance's remaining pods, or `1` when only its cleanup still runs.

| Metric | Labels | What it shows |
| --- | --- | --- |
| `ome_omenative_scale_down_active_pods` | `namespace`, `isvc`, `component` | A gauge: the summed cost of the batch's Instances — the delete work the current batch drives. |
| `ome_omenative_scale_down_deferred_instances` | `namespace`, `isvc`, `component` | A gauge: the eligible Instances outside the batch, waiting for the passes that follow. |
| `ome_omenative_scale_down_batch_pods` | `component` | A histogram of each batch's cost, recorded once, when the batch is admitted. |
| `ome_omenative_scale_down_instance_duration_seconds` | `component` | A histogram of each Instance's removal: the seconds from its batch admitting the delete to the Instance leaving the component's status, its pods gone and its cleanup done. Time spent deferred doesn't count. |
| `ome_omenative_scale_down_oversized_batch_total` | `component` | A counter of the batches that admit one Instance larger than the resolved budget — the first-Instance exception above. |

The two gauges name the InferenceService in `isvc`, so they locate the component whose Instances wait. Both return to `0` when the component has no scale-down in flight, and the manager removes their series when the component is deleted. The histograms and the counter carry only the component type — `engine`, `decoder` or `router` — summed across InferenceServices, so to single out one service, read the gauges, or the log line above.

When a scale-down is slower than you expect, the series say which lever to pull:

- `ome_omenative_scale_down_active_pods` sits at the resolved budget while `ome_omenative_scale_down_deferred_instances` stays above `0`: the budget is the pace. Raise `scaleDownPodBatchSize`.
- `ome_omenative_scale_down_oversized_batch_total` rises: an Instance cost more than the whole budget, so its batch was that one Instance, alone. The component scales down an Instance at a time however you set the budget — raise it to hold whole Instances, or use the percentage form, which grows with the component.
- `ome_omenative_scale_down_instance_duration_seconds` runs long: the batching isn't the brake — each Instance's own removal is slow, with pods stuck `Terminating` or a drain that doesn't end. See [Recover stuck deletions](../omenative/recover-stuck-deletions.md).

## Pace a prefill-decode roll

When a [rollout group](../../concepts/rollouts-and-traffic/rollout-groups.md) lists both the engine and the decoder, OME rolls the two components in [lockstep batches](../../concepts/rollouts-and-traffic/rollout-groups.md#batches-for-the-engine-and-the-decoder). Two settings under `ome.controller.lifecycle.prefillDecodeRoll`, which the chart writes into the same `lifecycle` entry of the `inferenceservice-config` ConfigMap and turns on by default, add rules to those batches:

| Setting | Chart default | What it does |
| --- | --- | --- |
| `keepRatio` | `true` | Plans the engine's and the decoder's batches together, sized so that the share of each side already moving to the new revision stays within the group's `maintainRatio` tolerance of the other side's share — `ome.controller.coordination.defaultRatioTolerancePercent`, the chart's 5, when the group names none. It applies on the drain-first and the surge path alike, and a move counts from the moment it starts. It also keeps a component that updates single-pod Instances in place in the batches, counting its in-place updates against its `maxUnavailable` step. |
| `firstSide` | `decoder` | Names the side that moves first inside every batch, `engine` or `decoder`: the other side's moves of a batch are admitted once the named side's new Instances of that batch serve. Empty lets both sides start a batch together. |

Under `keepRatio`, a batch that can't fit within the tolerance shrinks, and at least one Instance moves per batch, so a tight tolerance slows a roll and never stops it. Take a group of 12 engines and 8 decoders whose components move 10% at a time — 2 engines and 1 decoder per batch: on steps of their own, five batches in, the engine is 10 of 12 Instances into its roll, 83%, while the decoder is at 5 of 8, 63%. At the tolerance of 5, the planned batches walk the two sides through 2:1, 3:2, 5:3, 6:4, 8:5, 9:6, 11:7 and 12:8 instead, so neither side's share drifts more than 5 points from the other's. A group opts out of the sizing by setting a `maintainRatio` `tolerance` of `100`.

Name the `firstSide` that takes longer to come back, so one side of each batch is down at a time and both sides return close together. The waiting side's `rolloutHold` gate is `Ratio`, with a reason such as `engine batch 1 waits for decoder batch 1 to serve first (0 of 1 serving)`.

The manager has no built-in rules: a field the entry leaves out is off. `keepRatio: false` turns off the paired sizing, and `firstSide: ""` the ordering; without them, the engine and the decoder still roll in lockstep batches, each side sized by its own step and with no order between the sides — unless a side updates single-pod Instances in place, which only `keepRatio` keeps in the batches. To turn both rules off, set the block to `null`, and the chart leaves it out of the entry:

```yaml title="values.yaml"
ome:
  controller:
    lifecycle:
      prefillDecodeRoll: null
```

Upgrade with your values file, as in [Step 1](#with-a-helm-value), and check the rendered entry — with `jq`, since the entry holds other settings too:

```bash
kubectl get configmap inferenceservice-config -n ome -o jsonpath='{.data.lifecycle}' | jq '{prefillDecodeRoll}'
```

```output
{
  "prefillDecodeRoll": null
}
```

With the chart's defaults in place, the same command prints the block with its two fields, `"firstSide": "decoder"` and `"keepRatio": true`. The two settings aren't in [the list that needs a restart](#tune-the-config-cache): the manager reads them through its config cache, so even a `kubectl` edit takes effect within the cache's TTL — unlike the batch sizes in the same entry. A `firstSide` other than `engine`, `decoder` or empty doesn't stop the manager: it reads as empty, so both sides start their batches together while `keepRatio` stands as written, and the manager logs the bad value only at verbosity 1. So when the ordering doesn't apply, check the rendered entry for a typo.

## Pace canary re-checks

During a [canary rollout](../../reference/rollouts/canary-progression.md), the manager drives the canary in reconcile passes, and a waiting pass schedules the next one itself, because a wait can end without anything changing in the cluster: a [timed pause](../../reference/rollouts/canary-progression.md#how-a-step-advances) runs out, a `scaleDownDelaySeconds` wait passes. Two settings under `ome.controller.rollout`, which the chart writes to the `rollout` entry of the `inferenceservice-config` ConfigMap, set how long it waits between checks:

| Setting | Chart default | What it does |
| --- | --- | --- |
| `canaryRequeue` | `"10s"` | How soon the manager re-checks a canary in progress: a step in `Pending` waiting for its new Instances, a held gate — a timed pause, or a manual one waiting for a promote — the [`scaleDownDelaySeconds`](../../reference/rollouts/canary-progression.md#fields) wait before the canary completes, and a rollback moving Instances back. |
| `canaryParkedRequeue` | `"5m"` | How often it revisits a canary parked at [`Failed`](../../reference/rollouts/canary-progression.md#phases), which only an action of yours moves, such as a [rollback](../roll-out-changes/promote-or-roll-back-a-canary.md#roll-back-the-canary) or a fix that gives the group a new revision. |

So with the chart's values, a pause that has run out can hold its step for up to 10 more seconds, and a parked canary is looked at every 5 minutes. What the manager watches doesn't wait for the cadence: a promote or rollback annotation, like any change to the InferenceService's spec, labels or annotations, starts a pass at once. And a step whose [analysis](../../reference/rollouts/canary-analysis.md#when-queries-run) gate holds is re-checked every `interval` of its analysis instead, so sampling keeps its own cadence.

This example re-checks a waiting canary every 5 seconds and revisits a parked one every minute:

```yaml title="values.yaml"
ome:
  controller:
    rollout:
      canaryRequeue: "5s"
      canaryParkedRequeue: "1m"
```

Every re-check is a reconcile pass, so a lower `canaryRequeue` adds API server load when many InferenceServices roll at once; raise it then instead. The `rollout` values that you don't set keep the chart's defaults. Upgrade with your values file, as in [Step 1](#with-a-helm-value), and check the rendered entry — with `jq`, since the entry holds other settings too:

```bash
kubectl get configmap inferenceservice-config -n ome -o jsonpath='{.data.rollout}' \
  | jq '{canaryRequeue, canaryParkedRequeue}'
```

```output
{
  "canaryRequeue": "5s",
  "canaryParkedRequeue": "1m"
}
```

The two settings aren't in [the list that needs a restart](#tune-the-config-cache): the manager reads them through its config cache, so even a `kubectl` edit takes effect within the cache's TTL — unlike `maxPinnedPlanBytes` in the same entry.

With a value empty, or the entry absent — the kustomize manifests don't write one — the manager has no fixed cadence and falls back to its rate-limited requeue: the canary still moves, but on no schedule you can tune. A value that isn't a positive duration, such as `"0s"` or `"3d"`, counts as empty the same way. Unlike a bad `lifecycle` value, it doesn't stop the manager, and nothing logs it — so when a canary re-checks slower than the cadence you set, check the rendered entry for a typo.

## Damp per-revision traffic churn in status

For each [OMENative](../../concepts/omenative/overview.md) component that no canary step drives, OME writes the component's live traffic split into `status.components.<component>.traffic`: the revisions' shares of the component's serving pods, as percents that sum to 100. The weights follow the raw pod counts, so on a large component one pod that blips out of readiness between reconciles — momentarily Pending, or draining — shifts the computed percents, and every shift is another status write for the API server and every watcher of the InferenceService to process. The weights report the split, they don't route it: requests reach every ready Instance either way, so only the report churns.

The setting `ome.controller.coordination.trafficWeightDeadbandPercent`, which the chart writes to the `coordination` entry of the `inferenceservice-config` ConfigMap, puts a per-revision band on that writer. With a band above 0, OME skips the write when the recomputed weights name the same revisions as status and every revision's percent moves by less than the band from the value already written: the move counts as pod-count jitter. The chart's default is `0`: the band is off, and every meaningful change is written. A negative value also turns the band off, and the manager itself has no built-in band — an absent entry reads as 0.

The band never suppresses:

- A revision's entry appearing, disappearing or changing name: a changed revision set is a real topology change, so the weights are always written.
- A `latestRevision` flip on any entry, which marks the revision the component converges toward.
- A `pairingProtocol` change on any entry, which routing consumers pair engine and decoder targets on.
- An entry arriving at exactly `0` or `100` from another value: the end of a roll is never jitter, so a band larger than a roll's last step can't hold the written split at 95/5.
- A move of the band or more on any entry.

Any one of these writes the whole weight set. The band compares each recomputed percent with the one written in status, not with the last one computed, so a roll that keeps moving pods drifts further from the written weights on every pass, crosses the band and is written: a ratio change bigger than the band always lands in status. What you accept is a lagging report — the written weights can sit up to a point under the band away from the live split for as long as the pods hold that split — so keep the band below any difference your dashboards or alerts must see. A suppressed write holds back only the weights: the component's `latestReadyRevision` and `latestRolledoutRevision` fields update independently.

This example treats per-revision swings of up to 4 points as jitter:

```yaml title="values.yaml"
ome:
  controller:
    coordination:
      trafficWeightDeadbandPercent: 5
```

Upgrade with your values file, as in [Step 1](#with-a-helm-value), and check the rendered entry — with `jq`, since the entry also holds `defaultRatioTolerancePercent`, the ratio-tolerance default that [Pace a prefill-decode roll](#pace-a-prefill-decode-roll) describes:

```bash
kubectl get configmap inferenceservice-config -n ome -o jsonpath='{.data.coordination}' | jq '{trafficWeightDeadbandPercent}'
```

```output
{
  "trafficWeightDeadbandPercent": 5
}
```

The entry isn't in [the list that needs a restart](#tune-the-config-cache): the manager reads it through its config cache on every pass, so even a `kubectl` edit takes effect within the cache's TTL. The band gates only this pod-proportional writer. A component that a canary group drives gets its weights from the canary's recorded step, as [Traffic weight in status](../../reference/rollouts/canary-progression.md#traffic-split) describes, and the band never holds those back.

## Recognize other accelerator resources

The `acceleratorResources` entry of the `inferenceservice-config` ConfigMap lists the extended resource names that OME counts as accelerators when it reads a runner container's requests and limits. The count sets the `PARALLELISM_SIZE` environment variable in the engine's and the decoder's runner: the accelerators in each pod times the pods in each replica. The chart writes the entry from `ome.controller.acceleratorResources`, whose default lists only `nvidia.com/gpu`.

A resource that the list leaves out counts as no accelerators: with the default list, a runner that requests only `amd.com/gpu` or `google.com/tpu` gets no `PARALLELISM_SIZE`, so an argument such as `--tp-size=$(PARALLELISM_SIZE)`, as in [Serve a multi-node model](../omenative/serve-a-multi-node-model.md), reaches the model server as that text, unexpanded. To serve on another vendor's accelerators, add their resource name — the exact name, no patterns:

```yaml title="values.yaml"
ome:
  controller:
    acceleratorResources:
      - nvidia.com/gpu
      - amd.com/gpu
```

!!! warning "Adding a name rolls the components that request it"
    The list shapes pod templates, not only the controller. An engine or decoder whose runner requests a resource you add counted as having no accelerators, so its pod template carried no `PARALLELISM_SIZE`; once the name is on the list, OME renders the variable onto the template, and the changed template rolls the component: an [OMENative](../../concepts/omenative/overview.md) component mints a new revision and moves its Instances by its [update strategy](../../concepts/architecture/omenative-update-strategies.md), and in RawDeployment the Deployment replaces its pods. Removing a name rolls the components that gained the variable the same way. Schedule the change like a pod-template change, not like a config tweak.

When a runner requests more than one listed resource, OME counts the first name in the list's order that the runner's resources carry, reading its limit before its request. The variable replaces any value you set in the runner, and [Serving runtimes](../../concepts/runtimes/serving-runtimes.md#the-engine-decoder-and-router) covers when OME leaves it unset. The manager itself has no default list: with the value empty, the chart leaves the entry out of the ConfigMap, and the manager falls back to recognizing only `nvidia.com/gpu` — an empty list doesn't turn recognition off, and a cluster whose values never set the list keeps NVIDIA-only recognition.

Upgrade with your values file, as in [Step 1](#with-a-helm-value), and check the rendered entry:

```bash
kubectl get configmap inferenceservice-config -n ome -o jsonpath='{.data.acceleratorResources}{"\n"}'
```

```output
["nvidia.com/gpu","amd.com/gpu"]
```

The entry isn't in [the list that needs a restart](#tune-the-config-cache): the manager reads it through its config cache, so even a `kubectl` edit takes effect within the cache's TTL, and reaches each component when its InferenceService next reconciles. The upgrade's restart reconciles them all at once.

Don't confuse the list with `ome.controller.quotaAcceleratorResources`, which sets the [`--accelerator-resources`](../../reference/operate-ome/controller-manager-flags.md#controllers) flag: that one scopes [quota admission](accelerator-quota.md#budget-other-accelerators), and a name added there changes no pod template.

## Add environment variables and files to the manager pod

Three values under `ome.controller` add environment variables and files to the manager pod, such as proxy settings or a private CA bundle. All three default to empty lists, which leave the rendered Deployment unchanged:

- `extraEnv`: environment variables for the `manager` container, written like a container's `env`, so `valueFrom` works too. The chart appends them after the `POD_NAMESPACE` and `SECRET_NAME` variables that it sets itself.
- `extraVolumes`: volumes for the pod, written like a Pod's `volumes`, appended after the chart's webhook certificate volume.
- `extraVolumeMounts`: their mounts in the `manager` container, written like a container's `volumeMounts`.

Because they're values, an upgrade with your values file keeps them, where the next upgrade can undo a `kubectl` patch to the Deployment.

This example sends the manager's outbound requests through a corporate proxy and trusts a private CA. First create a ConfigMap from your bundle file, `ca-bundle.crt` here:

```bash
kubectl create configmap corp-ca-bundle -n ome --from-file=ca-bundle.crt
```

```output
configmap/corp-ca-bundle created
```

Then add the values:

```yaml title="values.yaml"
ome:
  controller:
    extraEnv:
      - name: HTTPS_PROXY
        value: "http://proxy.example.com:3128"
      - name: NO_PROXY
        value: "10.0.0.0/8,.svc,.cluster.local"
      - name: SSL_CERT_FILE
        value: /etc/corp-ca/ca-bundle.crt
    extraVolumes:
      - name: corp-ca
        configMap:
          name: corp-ca-bundle
    extraVolumeMounts:
      - name: corp-ca
        mountPath: /etc/corp-ca
        readOnly: true
```

The proxy variables take Go's standard form: the manager sends requests through the proxy in `HTTPS_PROXY`, except to the hosts, domains and CIDR ranges that `NO_PROXY` lists. `SSL_CERT_FILE` names the file the manager reads root certificates from, in place of the image's default bundle, so put every certificate authority it must trust in the bundle, not only the private one. `SSL_CERT_FILE` doesn't change the connection to the Kubernetes API server, which uses the cluster's CA from the pod's ServiceAccount.

!!! warning "`NO_PROXY` must cover the cluster"
    The manager's requests to the Kubernetes API server honor the proxy variables too. Keep the cluster's service network and internal domains in `NO_PROXY`, as the example's `10.0.0.0/8`, `.svc` and `.cluster.local` do, with the range your cluster uses. Otherwise the manager sends its API requests to the proxy.

Upgrade with your values file, as in [Step 1](#with-a-helm-value), and wait for the rollout. Then print the names of the `manager` container's environment variables:

```bash
kubectl get deployment ome-controller-manager -n ome \
  -o jsonpath='{range .spec.template.spec.containers[?(@.name=="manager")].env[*]}{.name}{"\n"}{end}'
```

```output
POD_NAMESPACE
SECRET_NAME
HTTPS_PROXY
NO_PROXY
SSL_CERT_FILE
```

The chart's two variables come first, and your `extraEnv` entries follow them. The volumes work the same way: the pod has `cert`, the webhook certificate volume, then your `extraVolumes`, and the `manager` container mounts the certificate, then your `extraVolumeMounts`.

## Tune runtime-revision garbage collection

OME keeps the versions of a runtime's spec as ControllerRevisions in the manager's namespace, so that InferenceServices can pin a version. For each runtime name, across namespaces, it keeps the 10 newest revisions and the revisions that InferenceServices use. It marks the others with the annotation `ome.io/gc-eligible-since`, and deletes them 24 hours later. See [Garbage collection](../../concepts/runtimes/runtime-revisions.md#garbage-collection).

`--runtime-revision-retention` and `--runtime-revision-grace-period` change the 10 and the 24 hours. Set them with a patch, as the example in [Without a Helm value](#flags-without-a-helm-value) does. Write days in hours: `72h` for three days. List the revisions, their runtimes and when they were marked:

```bash
kubectl get controllerrevisions -n ome -l ome.io/runtime-of \
  -o custom-columns='NAME:.metadata.name,RUNTIME:.metadata.labels.ome\.io/runtime-of,MARKED:.metadata.annotations.ome\.io/gc-eligible-since'
```

`MARKED` is `<none>` for a revision that OME keeps, and otherwise the time that OME marked it.

## Turn on RolloutPolicy and AutoscalerPolicy {since=v1.3}

!!! note "Alpha"
    RolloutPolicy and AutoscalerPolicy are alpha. Their fields and behavior can change between releases.

Both features are off by default. To turn one on, set `ome.rolloutPolicy.enabled` or `ome.autoscalerPolicy.enabled` to `true` in both charts, and upgrade `ome-crd` first, then `ome-resources`. Then restart the manager, which looks for the CRDs only when it starts. Until then, the InferenceService webhook rejects InferenceServices that reference that kind of policy, with `AutoscalerPolicyFeatureDisabled` or `RolloutPolicyRefUnsupported` in the error. [Rollout policy](../../concepts/rollouts-and-traffic/rollout-policy.md#turn-on-the-feature) and [Autoscaler policy](../../concepts/serving/autoscaler-policy.md#turn-on-the-feature) give the commands and the checks.

!!! danger "Turning a feature off deletes its policies"
    With the value back at `false`, the `ome-crd` chart deletes the feature's CRD, and Kubernetes deletes all AutoscalerPolicies or RolloutPolicies with it. Remove the policy references from your InferenceServices first. Keep the policies' manifests, for example in Git, so that you can apply them again after you turn the feature back on.

## Troubleshooting

### The manager rollout doesn't finish

`kubectl rollout status` keeps waiting when the new pods exit at startup. The Deployment keeps the old pods, so the current leader keeps running. A bad entry in the `inferenceservice-config` ConfigMap can break the leader too, once its config cache expires: with a bad `deploy` entry, InferenceService reconciles fail. Look for the startup errors:

```bash
kubectl logs -n ome -l control-plane=ome-controller-manager --tail=-1 | grep -E \
  -e 'Invalid leader election timing|invalid --multicluster-role' \
  -e 'flag provided but not defined|invalid (boolean )?value ".*" for (flag )?-' \
  -e 'Failed to initialize [a-zA-Z -]+ configuration'
```

| Message | Cause and fix |
| --- | --- |
| `Invalid leader election timing` | The lease values break a rule in [Lease timing](#lease-timing). The error gives the values and the rule. |
| `invalid --multicluster-role` | Set `ome.multicluster.role` to `control-plane`, or leave it empty. |
| `flag provided but not defined`, `invalid value` or `invalid boolean value` | A patched argument is misspelled, isn't in your OME version, or has a bad value, such as `3d` for a duration. |
| `Failed to initialize deployment configuration`, or the same for another entry | That entry of the `inferenceservice-config` ConfigMap is invalid, and the rest of the line says why. For `replicas` in the `deploy` entry, see [Set replica defaults](set-replica-defaults.md). For `Failed to initialize event recorder configuration`, the `eventRecorder` entry breaks a rule in [Tune the per-object event budget](#tune-the-per-object-event-budget). For `Failed to initialize lifecycle scale configuration`, a scale setting in the `lifecycle` entry breaks a rule in [Pace OMENative scale-ups and scale-downs](#pace-omenative-scale-ups-and-scale-downs). |

Fix the value and upgrade again, as in Step 1. Correct a patched argument with `kubectl edit deployment ome-controller-manager -n ome`, which prints `deployment.apps/ome-controller-manager edited` when you save a change.

## Next steps

- [Controller manager flags](../../reference/operate-ome/controller-manager-flags.md): the manager's flags, with their defaults and Helm values.
- [Set up alerting](alerting.md): turn on the chart's alerts for when the control plane stops reconciling or the admission webhook fails.
- [Collect metrics](metrics.md): tune the Prometheus that the chart deploys, which also scrapes the manager's metrics.
- [Run the model agent](model-agent.md): configure the node agent that downloads models and reads their metadata.
- [Set accelerator quotas](accelerator-quota.md): limit the accelerators that InferenceServices can use.
