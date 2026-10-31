---
title: Traffic map
description: "A TrafficMap is the alpha routing table that OME writes for a multi-cluster InferenceService: each workload cluster's share of the requests, and the reasons behind it."
status: preview
since: v1.3
---

[Placement](../../reference/api/ome.v1beta1.md#ome-io-v1beta1-PlacementSpec) decides which [workload clusters](../../reference/api/ome.v1beta1.md#ome-io-v1beta1-WorkloadCluster) run a multi-cluster [InferenceService](../serving/inference-services.md). Multi-cluster placement and routing are alpha, still in development, and off by default. With routing on, OME writes a TrafficMap that gives each of those clusters a weight. A publisher writes the weights into your global gateway, by default as a [Gateway API](https://gateway-api.sigs.k8s.io/) HTTPRoute. Read the map to see why a cluster gets less traffic, or none, and whether the publisher has written the latest weights.

## Turn on routing

Routing runs only on the control-plane cluster, and it's off by default. Turn it on in that cluster's `ome-resources` values:

```yaml
ome:
  multicluster:
    enabled: true
    role: control-plane
    config:
      routing:
        enabled: true
```

`helm upgrade` restarts the manager to apply them. If you edit the `inferenceservice-config` ConfigMap instead, restart the manager yourself.

Routing then writes the maps. To publish them to your gateway, set the `endpoint` values in [Publish a global endpoint](../../guides/multi-cluster/publish-a-global-endpoint.md#step-1-configure-publishing). Until you do, `Published` is `False` with reason `Withdrawn`.

OME writes a map, with the same name and namespace, for each InferenceService that sets `spec.placement` — set at create and [fixed for the life of the service](#placement-is-fixed-at-create). The obsolete annotations `ome.io/accelerator-requirements` and `ome.io/cluster-selector` don't opt a service in: OME rejects them on write, and a stored InferenceService that still carries one gets no map, only a `validate placement intent:` error in the manager's log. See [When the map stops updating](#where-it-comes-from). To opt an InferenceService out, set `spec.routing.enabled: false`. Setting it to `true` doesn't turn routing on when the installation has it off.

OME deletes the map when the InferenceService stops being routed, for example because you turn routing off. When you delete the InferenceService, the finalizer `ome.io/trafficmap-publisher` holds the map until the publisher has removed the route. Placement keeps the InferenceService's copies on the workload clusters until the map is gone.

## Placement is fixed at create

Choose a service's placement when you create it: `spec.placement.mode` is fixed for the life of the InferenceService. The InferenceService validator refuses an update that names another mode, adds a `spec.placement` block to a service created without one, or removes the block, with `spec.placement.mode is fixed for the life of the service and cannot change from Split to All; delete and recreate the service to place it another way`. The refusal names both modes, a missing block as `unset (no spec.placement block)` and a block without a mode as `unset` — the rule reads both as no mode. An update that keeps the mode isn't refused by this rule: the `clusterAffinity` terms and the `split` counts stay editable.

The validator enforces the rule whether or not the installation is multi-cluster: adding `spec.placement` to a live service is refused on a single-cluster installation too. The one cluster without the validator is the control-plane cluster, where [the control-plane role turns it off](../../reference/operate-ome/controller-manager-flags.md#what-the-control-plane-role-turns-off): nothing refuses the edit there, and the mode is fixed all the same — delete and recreate the service there too.

To move a running service onto multi-cluster placement, off it, or to another mode, delete the InferenceService and recreate it with the `spec.placement` you want. Placement reads only the control-plane cluster's InferenceServices, so a service that moves onto it from another cluster is recreated on the control-plane cluster, with the block.

## Read a TrafficMap

The examples use the InferenceService `llama-chat` in `llama-demo`, placed in mode `Split` on the workload clusters `cluster-a` and `cluster-b`.

To see a map, run `kubectl get trafficmap`, or its short names `tm` and `tmap`, on the control-plane cluster:

```bash
kubectl get trafficmap llama-chat -n llama-demo
```

```output
NAME         MODE    PUBLISHED   ROUTABLE   OVERRIDE   REASON     AGE
llama-chat   Split   True        True       False      Routable   3h
```

`MODE` is the InferenceService's placement mode: `Single` runs it on one cluster, `All` on every cluster that admits it, and `Split` divides a fixed replica count across clusters, in the shares that [its placement weights set](#how-a-split-placement-divides-the-replicas). The other columns come from the map's [conditions](#conditions).

[`kubectl ome get trafficmaps`](../../reference/kubectl-ome/get.md#trafficmaps-columns) lists the maps too, with their entry counts. `-o wide` adds the route and whether the publisher is current.

This is the map with four replicas ready on `cluster-a` and two on `cluster-b`, without its conditions and `status.publisher`:

```yaml
apiVersion: ome.io/v1beta1
kind: TrafficMap
metadata:
  name: llama-chat
  namespace: llama-demo
  generation: 4
spec:
  service: llama-chat
  mode: Split
  observedISVCGeneration: 7
  entries:
    - cluster: cluster-a
      endpoint: http://llama-chat.llama-demo.cluster-a.example.com
      weight: 2
      healthy: true
      capacity:
        allocated: 4
        ready: 4
        source: ControlPlane
    - cluster: cluster-b
      endpoint: http://llama-chat.llama-demo.cluster-b.example.com
      weight: 1
      healthy: true
      capacity:
        allocated: 2
        ready: 2
        source: ControlPlane
status:
  sourceUID: 0f8e2c1a-5b7d-4e39-9c61-2d4a8b3f7e10
  published: true
  observedTrafficMapGeneration: 4
  gatewayRef:
    group: gateway.networking.k8s.io
    kind: HTTPRoute
    name: llama-chat-global
    namespace: llama-demo
```

Each entry is a home of the InferenceService: a workload cluster that has admitted it and reports an endpoint for it. Condition reasons call the entries homes, as in `AllHomesUnready`. The entries hold these fields:

| Field | What it holds |
| --- | --- |
| `cluster` | The WorkloadCluster's name. |
| `endpoint` | The URL of the InferenceService's copy on that cluster. |
| `weight` | The cluster's share of the requests, relative to the other entries, from 0 through 1,000,000. |
| `healthy` | `true` when the cluster can serve and no probe gates it: a ready replica or, when `reportedWeight` is set, a positive `reportedWeight` in place of the ready-replica test. A healthy entry can still have weight 0. |
| `capacity.allocated` | The replicas that placement admitted on the cluster, lowered to `capacity.reported` when that's lower. When `reportedWeight` is set, it's diagnostic only. |
| `capacity.ready` | The ready replicas. They cap the weight unless `reportedWeight` is set. |
| `capacity.factor` | The capacity factor, present only when you set one and no `reportedWeight` replaces it: a reported weight already includes the hardware. |
| `capacity.source` | `Endpoint` when a capacity report lowered `allocated` or `reportedWeight` is set, otherwise `ControlPlane`. |
| `capacity.reported` | While capacity polling is on in `Ceiling` mode: the ceiling that the cluster reports, kept even when it's too high to lower `allocated`. See [Watch the capacity state](../../guides/multi-cluster/tune-routing-capacity-polling.md#step-3-watch-the-capacity-state). |
| `capacity.reportedWeight` | While capacity polling is on in [Reported mode](../../guides/multi-cluster/tune-routing-capacity-polling.md#reported-mode): the cluster's accepted report, which replaces its replica-based weight. Missing means no current report; `"0"` means the cluster reports that it can serve nothing. |
| `capacity.fallbackReason` | While capacity polling is on: why the cluster has no current report. See [A home falls back to its admitted replicas](../../guides/multi-cluster/tune-routing-capacity-polling.md#a-home-falls-back-to-its-admitted-replicas). |
| `probe` | The routing health probe's last verdict for the cluster, present once a configured probe has run. See [Check the probe state](../../guides/multi-cluster/routing-health-probes.md#step-3-check-the-probe-state). |
| `drainRefs` | The IDs of the drains that hold the cluster at weight 0. |

The map leaves out `allocated` and `ready` when they're 0. [`TrafficMapEntry`](../../reference/api/ome.v1beta1.md#ome-io-v1beta1-TrafficMapEntry) in the API reference describes every field.

With capacity polling in [Reported mode](../../guides/multi-cluster/tune-routing-capacity-polling.md#reported-mode), `spec.capacityMode` is `Reported`; unset means `Ceiling`. The mode binds the publisher too: a table in which any entry carries a `reportedWeight` is published at once, and the gateway's earlier weights never replace it, so a cluster that reports 0 loses its traffic right away. Without one, the publisher follows the same rules as in `Ceiling` mode, where it can keep the gateway's last positive weights while an all-zero table can still be the control plane's own replica counts lagging behind.

Treat the map as read-only: OME undoes a hand edit right away.

## How OME sets the weights

OME works out an entry's weight in five steps:

1. It counts the replicas that can serve: the lower of `capacity.allocated` and `capacity.ready`, times the cluster's capacity factor, which is 1 unless you set one. In [Reported mode](../../guides/multi-cluster/tune-routing-capacity-polling.md#reported-mode), a cluster with a `capacity.reportedWeight` uses it instead, as it is: no replica count caps it, and no factor scales it. For an InferenceService with an engine and a decoder, both counts are the lower of the two [components'](../serving/inference-services.md#components) counts; when [a Split's weights](#how-a-split-placement-divides-the-replicas) give the clusters different mixes, a decoder count is first scaled to its engine equivalent in the service's engine-to-decoder ratio, rounded down.
2. An entry that a routing health probe gates gets 0, with one exception that [Routable](#routable) describes.
3. It reduces the weights to their smallest whole-number ratio, so 4 and 2 become 2 and 1. A weight can't go above 1,000,000: the bound on [the entry's `weight` field](../../reference/api/ome.v1beta1.md#ome-io-v1beta1-TrafficMapEntry), which matches the largest weight a Gateway API backend takes, so [the publisher](#published) applies the weights verbatim. When the reduced ratio's largest weight is above the bound, OME scales every weight down so the largest is exactly 1,000,000 and rounds the other positive weights up: no positive weight scales to 0, and a weight of 0 stays 0. A ratio of 3,000,000,000 to 1 publishes as 1,000,000 and 1 — the clusters keep their order, and the small cluster keeps at least weight 1, above its exact share.
4. An entry whose cluster placement is draining gets 0. See [A placement move drains a cluster first](#a-placement-move-drains-a-cluster-first).
5. An entry that a drain holds gets 0. The other weights stay as they are.

Divide a weight by the sum of the weights for its share: in the example, `cluster-a` gets two thirds of the requests.

Five settings change the weights:

| Setting | What it does | How to use it |
| --- | --- | --- |
| Placement weights | In mode `Split`, set each cluster's share of the replicas — the counts the steps above start from. | [How a Split placement divides the replicas](#how-a-split-placement-divides-the-replicas) |
| Capacity factor | Counts a cluster's replicas for more or less. | [Weight traffic for heterogeneous clusters](../../guides/multi-cluster/weight-traffic-for-heterogeneous-clusters.md) |
| Capacity polling | Can lower a cluster's `capacity.allocated` to what the cluster reports — or, in Reported mode, replace its count with the report. | [Tune routing capacity polling](../../guides/multi-cluster/tune-routing-capacity-polling.md) |
| Routing health probe | Holds a cluster at 0 after repeated failures. | [Configure routing health probes](../../guides/multi-cluster/routing-health-probes.md) |
| Drain | Holds a cluster at 0, whatever its capacity. | [Drain a workload cluster](../../guides/multi-cluster/drain-a-workload-cluster.md) |

## How a Split placement divides the replicas

A `Split` placement runs a fixed number of replicas: [`split.replicas`](../../reference/api/ome.v1beta1.md#ome-io-v1beta1-SplitSpec), or the engine's `minReplicas` when `split.replicas` is unset. Placement divides that count across the matched clusters, and each cluster's share sets the replicas behind its entry's `capacity.allocated` and `capacity.ready` — the counts [the weights above follow](#how-ome-sets-the-weights). Every matched cluster gets an equal share until a [`clusterAffinity` term](../../reference/api/ome.v1beta1.md#ome-io-v1beta1-ClusterAffinityTerm) sets [`weights`](../../reference/api/ome.v1beta1.md#ome-io-v1beta1-ClusterAffinityWeights). This service is split 2:1 and gets [the map above](#read-a-trafficmap), four replicas on `cluster-a` and two on `cluster-b`:

```yaml title="llama-chat.yaml"
apiVersion: ome.io/v1beta1
kind: InferenceService
metadata:
  name: llama-chat
  namespace: llama-demo
spec:
  model:
    name: llama-3-1-8b-instruct
  runtime:
    name: srt-llama-3-1-8b-instruct
  engine:
    minReplicas: 6
    maxReplicas: 6
  placement:
    policy: ClusterAffinity
    mode: Split
    clusterAffinity:
      - matchFields:
          - key: metadata.name
            operator: In
            values: [cluster-a]
        weights:
          default: 2
      - matchFields:
          - key: metadata.name
            operator: In
            values: [cluster-b]
        weights:
          default: 1
```

Weights are ratios, each 1 or more, with no upper bound: `4` and `2` divide the same way as `2` and `1`. A term weighs every cluster it matches, and once any term weighs a component, a cluster matched by several terms adds their weights — a matching term that gives the component none counts as 1. The shares come out in whole replicas: each cluster gets its share rounded down, and the replicas left over go to the largest remainders, ties to the first cluster by name. A share small enough to round to zero leaves the cluster with nothing, and its weights go unused — [the condition below](#the-placementcomponentshares-condition) names them. A share that `split.maxReplicasPerCluster` can't hold isn't trimmed to fit: placement refuses the whole plan.

The accepted plan records the weights it divided by, so the shares don't follow a cluster relabeling that placement hasn't accepted: the engine's weight is in the cluster's [`allocation.weight`](../../reference/api/ome.v1beta1.md#ome-io-v1beta1-CandidateAllocationStatus), and the other components' in `allocation.componentMatches`. To read the accepted weights and shares:

```bash
kubectl get inferenceservice llama-chat -n llama-demo \
  -o jsonpath='{range .status.placement.candidates[*]}{.cluster} weight={.allocation.weight} replicas={.allocation.desiredReplicas}{"\n"}{end}'
```

```output
cluster-a weight=2 replicas=4
cluster-b weight=1 replicas=2
```

### Each component's share

A `weights` block weighs each component by name, with `default` for every component the term doesn't name. For a component the block doesn't cover, a term falls back:

| Component | A term's weight for it |
| --- | --- |
| Engine | `weights.engine`, then `weights.default`. |
| Decoder | `weights.decoder`, then `weights.default`, then the engine's. |
| Router | `weights.router`, then the engine's. |

The engine is the unit placement counts, so its shares are the split itself; a service without an engine counts decoders instead. The other components divide their own counts over the clusters that hold engines, each of those clusters getting at least one — a count smaller than their number refuses the plan:

- **The decoder follows the engine's shares.** Its count — `decoder.minReplicas`, or one decoder for each engine replica when it's unset — divides in proportion to the engine's shares when the decoder has no weights of its own, and also when its weights are proportional to the engine's: decoder weights `6` and `3` beside engine weights `2` and `1` still follow. Only weights in a different ratio split it independently.
- **The router isn't divided at all** until a term sets `weights.router`. Without one, every cluster runs the service's full router policy, `router.minReplicas` to `router.maxReplicas` on each. When any term sets `weights.router`, placement divides `router.minReplicas` across the clusters by the router weights, so the validator requires a positive `router.minReplicas`.

When `split.replicas` sets a count other than `engine.minReplicas`, a declared `decoder.minReplicas` and a divided `router.minReplicas` scale with it, in their declared ratio to `engine.minReplicas`, rounded up.

A decoder split by its own weights gives the clusters different engine-to-decoder mixes. Each entry's `capacity.allocated` and `capacity.ready` then count what the cluster can serve in the service's own engine-to-decoder ratio: a cluster rich in engines but short of decoders counts fewer replicas than it has engines, and its weight drops with them.

### What the validator refuses

The InferenceService validator refuses a weights block that can't work. On the control-plane cluster, where [the control-plane role turns the validator off](../../reference/operate-ome/controller-manager-flags.md#what-the-control-plane-role-turns-off), the same checks run in placement instead: the service's `PlacementInputValid` condition turns `False` with reason `InvalidPlacementIntent` and the same message, and placement accepts no new plan.

| The spec | The refusal |
| --- | --- |
| `weights` on a term in any mode but `Split`, `SplitByCapacity` included | `spec.placement.clusterAffinity[0]: weights is permitted only in Split mode` |
| A weight for a component the service doesn't declare | `spec.placement.clusterAffinity[0].weights.decoder names a component the service does not declare` |
| `weights.router` without a positive `router.minReplicas` | `spec.placement.clusterAffinity weights.router divides router.minReplicas across clusters; set a positive router.minReplicas` |
| A `spec.placement.policy` other than `ClusterAffinity` | `spec.placement.policy must be ClusterAffinity; the ComponentAffinity policy was removed, set each component's weight in the weights block of each clusterAffinity term` |

A term can also carry the deprecated scalar `weight`, which means `weights.default`. It's the one weight a manager without weights support reads, so a rollback to such a manager keeps the engine shares set there. Outside mode `Split` it's refused like the block, with `weight is permitted only in Split mode`. The API server itself rejects an empty `weights` block, a weight below 1, and the scalar next to the fields it duplicates, with `weight means weights.default; it may sit beside weights.decoder and weights.router, but not beside weights.default or weights.engine`.

### The PlacementComponentShares condition

On a `Split` InferenceService whose terms set `weights`, placement writes the condition `PlacementComponentShares` into the service's `status.conditions` whenever it has something to report: a decoder or a divided router, or a weight that goes unused. It's advisory — it never gates placement — and its status is `True`:

| Reason | What it means |
| --- | --- |
| `FollowEngineShares` | Every divided component follows the engine's shares. |
| `OwnWeights` | At least one component is split by its own weights. |

The message names each component's rule, such as `decoder follows the engine shares` or `router is split by its own weights`, and ends with the weights that do nothing because their cluster got no engines, such as `unused because the cluster holds no engine: router weight on cluster-b`. OME removes the condition from a service whose terms set no weights, and from an engine-only service whose every weight is in use.

For example, the same split for a service with a decoder that follows the engines and one router per cluster:

```yaml
spec:
  decoder:
    minReplicas: 6
    maxReplicas: 6
  router:
    minReplicas: 2
    maxReplicas: 2
  placement:
    policy: ClusterAffinity
    mode: Split
    clusterAffinity:
      - matchFields:
          - key: metadata.name
            operator: In
            values: [cluster-a]
        weights:
          default: 2
          router: 1
      - matchFields:
          - key: metadata.name
            operator: In
            values: [cluster-b]
        weights:
          default: 1
          router: 1
```

`default` covers the decoder, so it follows the engines: four decoders on `cluster-a`, two on `cluster-b`. The router weights divide `router.minReplicas` evenly, one router on each. The condition reports both rules:

```bash
kubectl get inferenceservice llama-chat -n llama-demo \
  -o jsonpath='{range .status.conditions[?(@.type=="PlacementComponentShares")]}{.reason}: {.message}{"\n"}{end}'
```

```output
OwnWeights: decoder follows the engine shares; router is split by its own weights
```

## A placement move drains a cluster first

Placement can zero a weight on its own, with no drain of yours. When a plan change retires one of a `Split` or `All` placement's homes, placement asks that cluster to drain first: in the control-plane InferenceService's `status.placement.candidates`, the cluster's [`allocation.drainRequested`](../../reference/api/ome.v1beta1.md#ome-io-v1beta1-CandidateAllocationStatus) turns `true`, and the map's next table holds its entry at 0.

Placement removes the InferenceService's copy from the cluster only after the map shows the zero. Until then the entry looks intact apart from its weight: it keeps its endpoint, `healthy: true` and its positive `capacity.allocated` and `capacity.ready`, and it gets no `drainRefs` — those list only the drains of the `ome.io/traffic-drain` annotation. That's the designed order of a move, not a fault, and there's nothing to undrain: the zero stays until the copy is gone and the entry leaves the table. To tell a draining cluster from a bug, read the allocations:

```bash
kubectl get inferenceservice llama-chat -n llama-demo \
  -o jsonpath='{range .status.placement.candidates[*]}{.cluster}={.allocation.drainRequested}{"\n"}{end}'
```

```output
cluster-a=true
cluster-b=
```

Placement is draining `cluster-a`, and `cluster-b` keeps its traffic. When a plan drains every cluster that has capacity, [Routable](#routable) turns `False` with reason `NoRoutableCapacity`, and the entries keep showing that capacity.

A `Single` move to another cluster also drains the cluster it leaves, but the map never holds its entry at 0: the status update that requests the drain also makes the new cluster the plan's winner, and in `Single` mode a cluster that placement is draining leaves the table. Placement removes the copy only after the entry is gone, so the retiring cluster's replicas outlive its entry; until they're removed, the allocations above show the cluster with `drainRequested: true`.

## Why a cluster's weight is zero

Compare the entry with these rows, and take the first that matches:

| What the entry shows | Why the weight is 0 |
| --- | --- |
| `drainRefs` lists an ID | A drain holds the cluster at 0, whatever its capacity. Remove the drain by its ID, as [Drain a workload cluster](../../guides/multi-cluster/drain-a-workload-cluster.md) shows. |
| No `drainRefs`, `healthy: true`, and `capacity.allocated` and `capacity.ready` above 0 | Placement is draining the cluster: [a placement move removes the cluster's copy only after this zero is in the map](#a-placement-move-drains-a-cluster-first), so the entry keeps its counts until then. The cluster's entry in `status.placement.candidates` shows `allocation.drainRequested: true`, and there's no drain to remove. |
| `capacity.reportedWeight: "0"` | The cluster reports that it can serve nothing. In [Reported mode](../../guides/multi-cluster/tune-routing-capacity-polling.md#reported-mode) the report is the cluster's capacity, so its ready replicas can't restore the weight; a positive report does. |
| No `capacity.ready`, and `healthy: false` | The cluster has no ready replica, or the InferenceService's copy there isn't `IngressReady` yet. |
| No `capacity.allocated`, `capacity.source: Endpoint` and `capacity.reported: 0` | The cluster reports that it can serve nothing, although its replicas are ready. |
| No `capacity.allocated`, and `capacity.source: ControlPlane` | Placement admitted no replicas on the cluster. |
| `probe.gated: true`, and `healthy: false` | The probe gated the cluster after repeated failures. `probe.message` holds the last one. |

If every entry has weight 0, or the table is empty, the [Routable](#routable) condition names the cause for the whole map.

## Conditions

`Routable`, `OverrideActive` and `CapacityFallback` describe the table. `Published` says whether the publisher has written it to the gateway.

`kubectl describe` also shows `PublicationFallback`, which is `False` with reason `Unsupported` after the built-in publisher publishes successfully. It's `Unknown` while a publish is under way or failing. When `Published` has reason `Withdrawn`, `Unpublished` or `Finalizing`, `PublicationFallback` is `False` with one of those reasons too.

### Routable

`Routable` says whether a gateway can send the InferenceService's requests anywhere:

| Status | Reason | What it means |
| --- | --- | --- |
| `True` | `Routable` | At least one entry has a positive weight. |
| `True` | `AllHomesProbeFailed` | The probe gates every cluster, and `allFailedPolicy: PreserveTraffic` keeps the weights that the clusters' capacity gives them. |
| `False` | `NotPlaced` | The InferenceService's `status.placement.phase` isn't `Placed`, so the table is empty. |
| `False` | `NoAddressableHome` | The InferenceService is placed, but no admitted cluster reports an endpoint, so the table is empty. |
| `False` | `TrafficDrain` | Drains hold every cluster that had a positive weight. |
| `False` | `AllHomesUnready` | No cluster has a ready replica, and none has a `capacity.reportedWeight`. |
| `False` | `NoRoutableCapacity` | No cluster that the probe leaves open, and that placement isn't draining, has capacity: `capacity.allocated` and `capacity.ready` both above 0, or a positive `capacity.reportedWeight` in place of them. When [a placement move drains](#a-placement-move-drains-a-cluster-first) every cluster that has capacity, the entries keep those counts. |
| `False` | `AllHomesProbeFailed` | The probe gates every cluster, and its `allFailedPolicy` is `Drain`. |

### OverrideActive

`OverrideActive` says whether drains hold any entry:

| Status | Reason | What it means |
| --- | --- | --- |
| `True` | `OverridesApplied` | At least one drain holds an entry. |
| `False` | `OverridesPending` | The InferenceService has drains, but none names a cluster in the table. `kubectl ome traffic drain` doesn't check the name against the map, so look for a misspelling. |
| `False` | `NoOverrides` | The InferenceService has no drains. |

### CapacityFallback

`CapacityFallback` is `True` when capacity polling is on and an entry has no current report:

| Status | Reason | What it means |
| --- | --- | --- |
| `True` | `EndpointCapacityUnavailable` | At least one entry has no current report, and its `capacity.fallbackReason` says why. The entry uses the control plane's count — in [Reported mode](../../guides/multi-cluster/tune-routing-capacity-polling.md#reported-mode), only once its cached reports expire too. |
| `False` | `EndpointCapacityAvailable` | Every entry has a current capacity report. |
| `False` | `CapacityPollingDisabled` | Capacity polling is off. |
| `False` | `NoCapacityTargets` | The table is empty, so there's nothing to poll. |

### Published

The built-in publisher writes the table into an HTTPRoute, by default `llama-chat-global` in the InferenceService's namespace, and `status.gatewayRef` names it. The route has a backend for each entry, at the entry's weight, entries at weight 0 included. [How publishing works](../../guides/multi-cluster/publish-a-global-endpoint.md#how-publishing-works) lists what else it writes.

`Published` means that the publisher wrote the route. The HTTPRoute's own status says whether the gateway accepted it, as [Check the publication](../../guides/multi-cluster/publish-a-global-endpoint.md#step-2-check-the-publication) shows.

| Status | Reason | What it means |
| --- | --- | --- |
| `True` | `Published` | The publisher wrote the current table. |
| `False` | `Transitioning` | The publisher is writing the current table. |
| `False` | `Withdrawn` | One of `globalGateway`, `backendPort` or the InferenceService's global hostname is missing, so the publisher deleted its route. See [Withdrawn](../../guides/multi-cluster/publish-a-global-endpoint.md#published-is-false-with-reason-withdrawn). |
| `False` | `InvalidPlan` | The table can't become a route, for example because it has more than 16 entries. See [InvalidPlan](../../guides/multi-cluster/publish-a-global-endpoint.md#published-is-false-with-reason-invalidplan). |
| `False` | `InvalidOptions` | `spec.routing.publisher.options` sets options, and the built-in publisher takes none. |
| `False` | `ClaimRejected` | The publisher can't claim the route or one of its objects, for example because another route already publishes the global hostname. See [ClaimRejected](../../guides/multi-cluster/publish-a-global-endpoint.md#published-is-false-with-reason-claimrejected). |
| `False` | `ApplyFailed` | Writing or deleting the route, or one of its objects, failed. See [ApplyFailed](../../guides/multi-cluster/publish-a-global-endpoint.md#published-is-false-with-reason-applyfailed). |
| `False` | `DrainFailed` | After the route moved, for example because `routeNamespace` changed, setting the old route's weights to 0 failed. |
| `False` | `InvalidOwner` | The map doesn't match its InferenceService, for example until OME rewrites it after a spec change. See [When the map stops updating](#where-it-comes-from). |
| `False` | `LegacyLifecycleActive` | The publisher waits for the legacy endpoint publisher to finish its cleanup. See [LegacyLifecycleActive](../../guides/multi-cluster/publish-a-global-endpoint.md#published-is-false-with-reason-legacylifecycleactive). |
| `False` | `PublisherChanged` | `status.publisher` lists targets that another publisher claimed, for example after `ome.multicluster.config.routing.publisher.name` changed. |
| `False` | `Finalizing` | The map is being deleted, and the publisher is removing its route. |
| `False` | `Unpublished` | The InferenceService stopped being routed or is being deleted. If the map is being deleted, the publisher has removed its route. |
| `False` | `UnpublishFailed` | OME doesn't set this reason. When removing the route fails, `Published` stays `Finalizing`, and the manager logs `TrafficMap publisher finalization failed` and retries. |

When publishing fails, the route from the last success stays, and `status.gatewayRef` still names it. The condition's message is generic. The error is in the manager's log, on a line that reads `TrafficMap publisher reconciliation failed`. The publisher retries on its own.

## Staleness checks

Before you rely on a map, check that it's current:

| Compare | With | A mismatch means |
| --- | --- | --- |
| `spec.observedISVCGeneration` | The InferenceService's `metadata.generation` | OME hasn't built a table from the latest spec yet. If the mismatch stays, see [When the map stops updating](#where-it-comes-from). |
| `status.observedTrafficMapGeneration` | The map's `metadata.generation` | The publisher hasn't published the latest table. After a failure, it still holds the generation of the last success. |
| Each condition's `observedGeneration` | The map's `metadata.generation` | The condition describes an older table. |
| `status.sourceUID` | The InferenceService's `metadata.uid` | The map is left from an earlier InferenceService with the same name. |
| `probe.lastProbeTime` | The current time | A time much older than the probe's `period` means that the prober has stopped. |

To compare the map's generation with the one that the publisher last published:

```bash
kubectl get trafficmap llama-chat -n llama-demo \
  -o jsonpath='{.metadata.generation} {.status.observedTrafficMapGeneration}{"\n"}'
```

```output
4 4
```

The numbers match, so the publisher has published the latest table.

## When the map stops updating {#where-it-comes-from}

Some errors stop OME from updating a map. The map keeps its last table and conditions, and the route keeps its last published weights. When the error follows a spec change, `Published` turns `False` with reason `InvalidOwner`. The error shows only in the manager's log. Read every replica's log with `kubectl logs -n ome -l control-plane=ome-controller-manager --tail=-1`.

| Log error | Cause |
| --- | --- |
| `validate placement intent:` | The InferenceService carries the obsolete `ome.io/accelerator-requirements` or `ome.io/cluster-selector` annotation. Remove the annotation, and put the restriction in `spec.placement.clusterAffinity`. |
| `validate routing policy:` | A factor in `spec.routing.capacityFactors` is 0 or negative. See [The weights don't change](../../guides/multi-cluster/weight-traffic-for-heterogeneous-clusters.md#the-weights-dont-change). |
| `parse ome.io/traffic-drain:` | The drain annotation is malformed. See [The TrafficMap stops changing after a hand edit](../../guides/multi-cluster/drain-a-workload-cluster.md#the-trafficmap-stops-changing-after-a-hand-edit). |
| `TrafficMap llama-demo/llama-chat exists but is not controlled by InferenceService UID` | A TrafficMap with that name belongs to something else, for example an earlier InferenceService with the same name. |

## Next steps

- [Publish a global endpoint](../../guides/multi-cluster/publish-a-global-endpoint.md): write the map's weights into your global gateway.
- [Weight traffic for heterogeneous clusters](../../guides/multi-cluster/weight-traffic-for-heterogeneous-clusters.md): count one cluster's replicas for more with a capacity factor.
- [Tune routing capacity polling](../../guides/multi-cluster/tune-routing-capacity-polling.md): cap a cluster's weight at the capacity it reports, or make the report the weight.
- [Configure routing health probes](../../guides/multi-cluster/routing-health-probes.md): gate a cluster's weight on an end-to-end probe.
- [Drain a workload cluster](../../guides/multi-cluster/drain-a-workload-cluster.md): hold a cluster at weight 0 with kubectl ome traffic drain.
