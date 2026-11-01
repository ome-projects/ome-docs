---
title: Migration and transient scale
description: OMENative moves an Instance to another node by starting its replacement first, and can run more or fewer Instances for a while without an edit to the InferenceService.
since: v1.3
---

[OMENative](overview.md) can move an [Instance](instances.md) to another node with no dip in serving capacity. It starts a replacement elsewhere and drains the old Instance once the replacement serves. Use a migration to empty a node before maintenance. A plain `kubectl drain` evicts the pods first, so the Instance stops serving until its pods run again elsewhere. A transient scale changes a component's Instance count without an edit to the [InferenceService](../serving/inference-services.md), until OME or the autoscaler sets it again.

Both act on the component's [InferenceReplica](../architecture/deployment-modes.md#what-ome-creates), which OME creates to hold its Instances, so both need a component on OMENative. [Request an Instance migration](../../guides/scale-and-migrate/request-an-instance-migration.md) and [Request a transient scale](../../guides/scale-and-migrate/request-a-transient-scale.md) give the steps.

The examples use the InferenceService `qwen3-0-6b` in the namespace `qwen3-0-6b`, from [Serve your first model](../../getting-started/serve-your-first-model.md), with its engine [switched to OMENative](../architecture/deployment-modes.md#opt-in-to-omenative) and running two Instances, 0 and 1. The engine's InferenceReplica is `qwen3-0-6b-engine`.

## How a migration works {#surge-migration}

OMENative moves an Instance by surge: it starts a replacement Instance, the surge, before it removes the old one, the source. Moving Instance 0 of `qwen3-0-6b-engine` off `gpu-node-a`, with `gpu-node-b` as a hint, goes like this:

1. OME gives the surge the lowest unused index, 2. The migration record moves to `SurgePending`, and OME records a `MigrationRequestAccepted` event. [`kubectl ome instance list`](../../reference/kubectl-ome/instance.md) shows Instance 0 as `Migrating` and Instance 2 as `Creating`.
2. OME creates the surge's pods from Instance 0's revision, with the same pod layout. They can't schedule on `gpu-node-a`, and the scheduler prefers `gpu-node-b`.
3. When the surge is ready and serving, and has stayed ready for the component's [`lifecycle.minReadySeconds`](../../guides/roll-out-changes/pace-rollouts-with-min-ready-seconds.md#ready-versus-available), the record moves to `SurgeReady`.
4. OME drains Instance 0, with the record in `Draining`, and then deletes it. When the deleted pods stay `Terminating`, the record [parks](#parked-migrations) until they're gone.
5. The record ends in `Completed`, with the message `migrated to instance=2`, and OME records a `MigrationCompleted` event.

While the move runs, the InferenceReplica's `CURRENT` column counts the surge, one Instance over `DESIRED`:

```bash
kubectl get inferencereplica qwen3-0-6b-engine -n qwen3-0-6b
```

```output
NAME                COMPONENT   DESIRED   CURRENT   READY   AVAILABLE   AGE
qwen3-0-6b-engine   engine      2         3         2       2           3d
```

So a migration needs room for one more whole Instance, even when only one pod of a multi-pod Instance is on the node you empty. If no node has room for the surge, the request fails at its [deadline](#deadline).

The surge keeps its [index](../architecture/deployment-modes.md#pod-labels-and-environment), so after the move the engine's Instances are 1 and 2, and a later scale-up takes index 0.

## Migration requests

A migration request asks OMENative to move one Instance off its node. It's an annotation on the InferenceService, not on the InferenceReplica, and three kinds of sender write it:

- [`kubectl ome migration start`](../../reference/kubectl-ome/migration.md), which is alpha, writes the request and fills in `from_node`. For an Instance whose pods span nodes, `--from-node` names the node to leave.
- [Alfred](../scheduling/alfred.md), OME's alpha GPU cluster caretaker, recommends Instance moves when free GPUs fragment or a node turns unhealthy and, when you [let it](../../guides/scheduling/let-alfred-migrate-instances.md), asks OMENative to carry them out.
- You, or your own tooling. OME treats a hand-written annotation like any other.

The key is `ome.io/migration-request-v1-` followed by a request UUID, and the value is a JSON object:

```json
{
  "schemaVersion": "v1",
  "component": "engine",
  "instance": 0,
  "from_node": "gpu-node-a",
  "hint_target_nodes": ["gpu-node-b"],
  "reason": "drain gpu-node-a for maintenance",
  "requested_at": "2026-09-28T08:00:00Z",
  "requested_by": "kubectl-ome"
}
```

| Field | Meaning |
| --- | --- |
| `schemaVersion` | Always `v1`. |
| `component` | `engine`, `decoder` or `router`. |
| `instance` | The index of the Instance to move. |
| `from_node` | The node to leave. When the Instance no longer runs there, OME moves it off the node it does occupy: see [When a request waits or fails](#what-refuses-a-migration-request). |
| `hint_target_nodes` | Optional. Nodes the scheduler should prefer for the surge. |
| `reason` | Optional. Free text that OME keeps in the migration record. |
| `requested_at` | Optional. When the request was made. |
| `requested_by` | Optional. Who asked, such as `kubectl-ome` or `alfred`. |

OME's webhook rejects a malformed request, or one for a component the InferenceService doesn't have, with a message that starts with `migration request`.

OME then records the request in the InferenceReplica's `status.migrations` as `Accepted`, adds a row to the audit ConfigMap `qwen3-0-6b-ome-migration-audit`, and removes the annotation. So a missing annotation means OME has taken the request, not that the Instance has moved. The `MigrationRequestAccepted` event comes later, when the move starts. A request OME can't act on, such as one for a removed component, gets a `MigrationRequestRejected` event instead of a record.

## Migration policy

`spec.<component>.lifecycle.migrationPolicy.mode` sets whether a component's Instances can move:

| Mode | Effect |
| --- | --- |
| `Auto` | The default. OME carries out migration requests, and [relocates](#automatic-relocation) an Instance that stalls on its node. |
| `Surge` | Same as `Auto` in this release. |
| `Never` | OME fails each migration request at once, with `migrations disabled by MigrationPolicy Mode=Never`, and turns relocation off. |

The runtime's `engineConfig`, `decoderConfig` or `routerConfig` can set the mode too, and the InferenceService's setting wins (see [The engine, decoder and router](../runtimes/serving-runtimes.md#the-engine-decoder-and-router) and [MigrationPolicy](../../reference/api/ome.v1beta1.md#ome-io-v1beta1-MigrationPolicy)). This is the examples' InferenceService, with migration turned off for its engine:

```yaml title="isvc.yaml"
apiVersion: ome.io/v1beta1
kind: InferenceService
metadata:
  name: qwen3-0-6b
  namespace: qwen3-0-6b
spec:
  deploymentMode: OMENative
  model:
    name: qwen3-0-6b
  runtime:
    name: srt-qwen3-0-6b
  engine:
    minReplicas: 2
    maxReplicas: 4
    lifecycle:
      migrationPolicy:
        mode: Never
```

## Deadline and caps {#deadline}

Each request has a deadline: the time OME accepts it, plus the component's `lifecycle.instanceReadyTimeout`, or OME's cluster-wide `instanceReadyTimeout` when the component sets none. The clock runs while a request waits. A [paused rollout](../../guides/roll-out-changes/pause-and-resume-a-rollout.md) holds every migration until you resume it, and stops the clock: OME clears the deadline of every open request while the component is paused, so none passes during the pause, and when you resume, each request gets a fresh deadline, a full timeout from that point.

A request that misses its deadline fails with a `MigrationExpired` event. OME removes the surge, if there is one, and the source goes back to `Ready`, or to `Failed` if its pods aren't ready. A [parked](#parked-migrations) migration is the exception: with no `forceDelete` policy, it outlives its deadline.

A known bug: if neither timeout is set, or the controller's `lifecycle` settings are invalid, every request fails at once with `deadline exceeded in phase Accepted: surge never allocated`. Set a timeout first, as [Set Instance readiness deadlines](../../guides/omenative/set-instance-readiness-deadlines.md) shows.

Migrations also use these controller settings, in the `lifecycle` entry of the `inferenceservice-config` ConfigMap or the chart's `ome.controller.lifecycle`:

| Setting | Chart | Kustomize manifests | What it sets |
| --- | --- | --- | --- |
| `instanceReadyTimeout` | `30m` | `30m` | The deadline, when the component sets none. |
| `stuckPodGracePeriod` | `60s` | Not set | How old a surge pod can get while stuck in a state such as `CrashLoopBackOff` before OME fails the request. Unset leaves it to the deadline. |
| `audit.maxMigrationsPerWindow` | `10` | Not set | How many of a component's migrations can start within `window`. |
| `audit.window` | `1h` | Not set | The window for that cap, and how long finished records stay in `status.migrations`. |
| `audit.maxInFlightMigrations` | `3` | Not set | Migrations running at once, [parked](#parked-migrations) ones included. Running migrations stay serial, so only parked ones can fill the cap. |
| `autoMigrate.maxAttempts` | `3` | Not set | How many times OME [relocates](#automatic-relocation) an Instance. Unset turns relocation off. |

A request that would go over `maxMigrationsPerWindow` fails, with a `RateLimited` event and a message such as `migration rate cap reached (10/10 in the last 1h0m0s)`. One that would go over `maxInFlightMigrations`, which only [parked migrations](#parked-migrations) can fill, fails the same way with `in-flight migration cap reached (3/3)`. Without all three `audit` settings, as on a kustomize install, a request waits in `Accepted` until you add them or its deadline passes. OME records a `MigrationPolicyUnconfigured` event, and sets a [condition](../architecture/deployment-modes.md#conditions) of the same name on the InferenceReplica. [The request stays Accepted](../../guides/scale-and-migrate/request-an-instance-migration.md#the-request-stays-accepted) shows how to add them.

## Automatic relocation

OMENative also moves Instances on its own. When a new Instance, or an update of a single-pod Instance, runs out of time or gets stuck, OME can mark it `Failed` and rebuild it on another node, if:

- all the attempt's pods ran on one node, and none failed for a cause in the workload itself, such as an image that can't be pulled;
- the mode is `Auto` or `Surge`;
- the Instance has relocations left in `autoMigrate.maxAttempts`.

The rebuilt Instance also avoids nodes that earlier relocations left. Each relocation adds a `Relocated` record to `status.migrations`, records an `AutoMigrationTriggered` event and adds one to the counter [`ome_omenative_auto_migrations_total`](#count-automatic-relocations). After the last one allowed, which also records `AutoMigrationCapReached`, OME stops relocating the Instance until it's next `Ready`. [What OME does with a failed Instance](instances.md#what-ome-does-with-a-failed-instance) has the full rules.

The avoidance lasts only while another node has room. When the scheduler can't place a rebuild anywhere for longer than [`unschedulableGracePeriod`](instances.md#when-an-instance-fails), OME prefers a rebuild that may land on a blamed node to one that can't run at all: it releases the nodes the Instance avoids for the revision it's building, and records one Normal `NodeExclusionReleased` event naming them, ending with `no other node had room for the rebuild; it may land on gpu-node-a again`. The rebuild that found no room fails with the scheduler's verdict, as the No room row of [When an Instance fails](instances.md#when-an-instance-fails) describes, and the next one can land on a node an earlier relocation left. The release returns no relocation attempts: the ones spent stay counted against `autoMigrate.maxAttempts` until the Instance is next `Ready`, which resets the count and the avoided nodes together. An Instance that avoids no node gets no release and no event.

## When a request waits or fails {#what-refuses-a-migration-request}

Once OME has taken a request, the request waits in `Accepted` while:

- another migration of the same component runs: OME moves one Instance of a component at a time, oldest request first, skipping [parked](#parked-migrations) migrations;
- an update's surge Instance is in flight on the component;
- the source Instance has another operation in progress, has an unscheduled pod, or isn't `Ready`;
- the `audit` settings aren't set (see [Deadline and caps](#deadline)).

A source Instance that's `Failed` on the revision it runs keeps the request waiting too, but not mute, since no operation in flight ends that wait by itself. OME writes the wait onto the record's message: `waiting for source component=engine instance=0, Failed on its running revision qwen3-0-6b-engine-7b4c2e19 (pod qwen3-0-6b-engine-0-default-0 container ome-container stuck (CrashLoopBackOff)), to be Ready again:` and then what holds the Instance's repair — the revision's retry block its rebuild waits on, that no repair is open on it, that the kubelet retries the configuration or image the pod needs in place, or where the retry ladder that re-arms the repair stands. When only you can release the hold, the message ends with `reset the Instance or publish a corrected revision`. A Warning `MigrationDeferred` event carries the same message, once per cause, and the move goes on by itself once the Instance is `Ready` again. [What OME does with a failed Instance](instances.md#what-ome-does-with-a-failed-instance) explains the repairs, and [The request stays Accepted](../../guides/scale-and-migrate/request-an-instance-migration.md#the-request-stays-accepted) says what each hold needs. The deadline keeps running while the request waits.

A `from_node` that no longer matches doesn't fail a request. When the source Instance was rebuilt on another node after the request, OME records a Normal `MigrationFromNodeMismatch` event naming both nodes, and moves the Instance off the node it occupies; the record's `from_node` follows. For an Instance whose pods span nodes, with none of them on `from_node`, the move leaves the leader's node.

OME fails a request when the source names no node to leave, with no live pods at all or a single-pod Instance's leftover pods on two nodes, when `maxMigrationsPerWindow` is reached or when the deadline passes. A source that waits on you rather than on a repair fails the request at once, too: one that's `Failed` on a revision it never started, and one whose running revision is [held](../../guides/roll-out-changes/release-a-held-revision.md#how-holds-and-releases-work) after its failed attempts — even when its pods read `Ready` between crashes — since the surge would only rebuild the held revision on another node. Both messages name the revision and where its retries stand, and end with `correct or release the revision, then request the move again`. The record ends in `Failed`, with a Warning event that says why: see [The migration fails](../../guides/scale-and-migrate/request-an-instance-migration.md#the-migration-fails).

When a move can't start now, `kubectl ome migration start` exits `3` with `migration precondition conflicts or is stale; inspect migration status and retry explicitly`. The usual causes are a paused rollout, an Instance that isn't `Ready` or already has a request, and the mode `Never`. An `ome.io/rollout-rollback` annotation left from a [rollback](../../guides/roll-out-changes/promote-or-roll-back-a-canary.md#roll-back-the-canary) blocks it too. [Refusals](../../reference/kubectl-ome/migration.md#start-refusals) lists the others.

A request for a component in RawDeployment or `MultiNode` (deprecated) mode stays on the InferenceService, since those modes have no InferenceReplica to act on it. Remove it yourself:

```bash
kubectl annotate inferenceservice qwen3-0-6b -n qwen3-0-6b ome.io/migration-request-v1-3f9c2a1e-7b4d-4c8e-9a6f-2d1b5e8c4a70-
```

```output
inferenceservice.ome.io/qwen3-0-6b annotated
```

## Parked migrations

In `Draining`, once the source Instance's pods have drained, OME deletes them. A deleted pod only goes away when its kubelet confirms that its containers stopped, so on a dead node it can stay `Terminating` for good, holding a [pod name](instances.md#what-an-instance-is) the component reuses. When every source pod is `Terminating` past its `deletionTimestamp`, which already includes its grace period, OME parks the migration: the record keeps its `Draining` phase, the surge keeps serving, and the record's message is `parked on source teardown: pod qwen3-0-6b-engine-0-default-0 is Terminating past its own deletion deadline`, naming the first of the stuck pods by name. When no `forceDelete` policy is set, the message adds `; no forceDelete policy is configured, so only its kubelet or an operator can remove it`. A Warning `MigrationParked` event carries the message, once per park.

A parked migration doesn't hold up the component's other migrations: OME skips it and runs the oldest request that isn't parked, as the event's ending says — `later migration requests for component=engine instance=0 dispatch ahead of it`. The park isn't free, though: the surge keeps `CURRENT` one over `DESIRED`, as in [How a migration works](#surge-migration), and the record keeps a slot under `audit.maxInFlightMigrations`.

OME has deleted everything it owns, so only the pod's kubelet, the owner of a [finalizer on it](../../guides/omenative/recover-stuck-deletions.md#a-poddeleteblockedbyfinalizer-event), or the [force-delete escalation](../../guides/omenative/recover-stuck-deletions.md) can remove the pods. OME keeps tending the record and completes the migration on its own once they're gone. With the escalation on, OME force-deletes a source pod once its node counts as dead, so a park on a dead node ends by itself.

The [deadline](#deadline) treats a park by the `forceDelete` policy:

- With no policy, a parked migration outlives its deadline: failing it would tear down the serving surge while the stuck pod still holds the source's pod name, with nothing configured to ever clear it. It stays parked until the pods are removed. A park whose surge stops serving loses that protection and fails at its deadline.
- With a policy, a source pod that stays `Terminating` past its `deletionTimestamp` plus `overdueSlack` — one the escalation doesn't act on, such as a pod with finalizers or on a node that isn't provably dead — fails the migration at its deadline, with a `MigrationExpired` event and the message `deadline exceeded in phase Draining: source drain incomplete`. OME removes the surge, and the source ends `Failed` while the pod holds its name.

## Watch migrations

`kubectl ome migration status` shows each migration record and its phase. For older moves, `kubectl ome migration history` also reads the audit ConfigMap, which keeps the last 200 finished migrations until you delete the InferenceService. [Step 3 of Request an Instance migration](../../guides/scale-and-migrate/request-an-instance-migration.md#step-3-follow-the-request) shows both commands, [`kubectl ome migration`](../../reference/kubectl-ome/migration.md) explains their columns and [MigrationStatus](../../reference/api/ome.v1beta1.md#ome-io-v1beta1-MigrationStatus) lists a record's fields.

OME records migration events on the InferenceService. This command lists completed migrations:

```bash
kubectl get events -n qwen3-0-6b --field-selector reason=MigrationCompleted -o custom-columns=KIND:.involvedObject.kind,NAME:.involvedObject.name,MESSAGE:.message
```

```output
KIND               NAME         MESSAGE
InferenceService   qwen3-0-6b   OMENative migration uuid=3f9c2a1e-7b4d-4c8e-9a6f-2d1b5e8c4a70 complete: component=engine instance=0 -> instance=2 (revision=qwen3-0-6b-engine-7b4c2e19)
```

| Reason | Type | When |
| --- | --- | --- |
| `MigrationRequestAccepted` | Normal | A move starts. |
| `MigrationCompleted` | Normal | The surge has replaced the source. |
| `MigrationRequestRejected` | Warning | OME rejected or failed a request, for any reason but its deadline or a stuck surge pod. |
| `MigrationFromNodeMismatch` | Normal | `from_node` doesn't host the source, so the move leaves the node the Instance occupies instead. |
| `MigrationFromNodeMismatch` | Warning | The source names no node to leave, so the request fails. |
| `MigrationNodeAffinityConflict` | Warning | The source's node affinity requires `from_node`. |
| `RateLimited` | Warning | A cap is reached. |
| `MigrationPolicyUnconfigured` | Warning | The `audit` settings aren't set, so the request waits. |
| `MigrationDeferred` | Warning | The source Instance is `Failed` on the revision it runs, so the request waits; the message names what holds its repair. |
| `MigrationParked` | Warning | Every source pod of a `Draining` migration is stuck `Terminating`, so the record [parks](#parked-migrations) and later requests dispatch ahead of it. |
| `MigrationSurgeCreateBlocked` | Warning | The API server refused a surge pod. OME retries until the deadline. |
| `MigrationSurgeWedged` | Warning | A surge pod is stuck, so OME failed the request. |
| `MigrationExpired` | Warning | A request passed its deadline. |
| `AutoMigrationTriggered` | Normal | OME relocated an Instance. |
| `AutoMigrationCapReached` | Warning | An Instance used its last relocation. |
| `NodeExclusionReleased` | Normal | No node had room for a rebuild that avoided the nodes earlier [relocations](#automatic-relocation) left, so the next rebuild may land on them again. |

### Count automatic relocations

The `AutoMigrationTriggered` events can undercount relocations: the manager's [per-object event budget](../../guides/operate-ome/configure-the-controller.md#tune-the-per-object-event-budget) silently drops events about a busy InferenceService, and a run of relocations is just such a period. OME also counts each [relocation](#automatic-relocation) it records on the counter `ome_omenative_auto_migrations_total` — one increment per relocation, the occurrence the `Relocated` record and the event mark. The controller manager serves it on its metrics endpoint, which the bundled Prometheus scrapes by default: see [OME's components](../../guides/operate-ome/metrics.md#omes-components). A migration you request doesn't touch the counter; follow those through the records and events above.

| Label | Value |
| --- | --- |
| `namespace` | The InferenceService's namespace. |
| `isvc` | The InferenceService's name. |
| `component` | The relocated Instance's component, such as `engine`. |
| `reason` | Always `AutoRecover`, the marker OME puts on a relocation's row in the [audit ConfigMap](../../reference/kubectl-ome/migration.md#history). |

This query sums each InferenceService's relocations over the last day:

```text
sum by (exported_namespace, isvc) (increase(ome_omenative_auto_migrations_total[24h]))
```

In queries, an InferenceService's namespace is in the label `exported_namespace`, because `namespace` holds the controller's. When you delete an InferenceService, OME drops its series from the counter, so sums don't keep counting a service that's gone.

## Transient scale

A transient scale sets `spec.replicas` on a component's InferenceReplica through the Kubernetes scale subresource. The InferenceService's `status.components.<component>.scaleTargetRef` names the InferenceReplica. Use `kubectl ome scale`, which checks first, or plain `kubectl scale`, which skips those checks.

The count lasts until whatever owns it writes it again. To change it for good, change `minReplicas`, `maxReplicas` or the autoscaler in the InferenceService.

### Who owns the count

The component's autoscaler class decides what writes the count next. With no autoscaling settings, the class is `HPA`, at 80% average CPU utilization. [Component autoscaling](../serving/component-autoscaling.md) explains the classes.

| Class | What replaces your count |
| --- | --- |
| `HPA` | The HorizontalPodAutoscaler that OME creates. |
| `KEDA` | KEDA, from the [ScaledObject](../serving/component-autoscaling.md#keda) that OME creates. |
| `External` | The autoscaler you run. |
| `None` | OME, which sets it back to `minReplicas` within moments. |

### `kubectl ome scale`

!!! note "Alpha"
    This command is alpha. Its flags and behavior can change between releases.

[`kubectl ome scale`](../../reference/kubectl-ome/scale.md) needs `--override-autoscaler --yes`, whatever the autoscaler class. It reports that the API server stored the count, not that the new Instances are up.

The command refuses while a rollout, a migration or other Instance work is in progress on the component, and when the count is outside the component's bounds. It reads the bounds from the InferenceService and its runtime, not from the controller's [replica defaults](../../guides/operate-ome/set-replica-defaults.md). So when neither sets `maxReplicas`, it accepts only `minReplicas`, or 1 when that's unset. [Refusals](../../reference/kubectl-ome/scale.md#refusals) lists every cause.

### When the count changes

- When `spec.replicas` goes up, OME creates Instances at the lowest unused indices.
- When it goes down, OME removes the Instances with a pod on a [cordoned node](instances.md#index) first, then keeps `Ready` Instances before the others, lower indices next, and drains and deletes the rest. It keeps an Instance that's migrating, and its surge.
- A count of 0 runs one Instance until OME sets the count back to `minReplicas`. `kubectl ome scale` refuses 0.
- A paused rollout holds scale-up, but OME still scales down.

`kubectl get inferencereplica` shows the new count under `DESIRED`.

## Next steps

- [Request an Instance migration](../../guides/scale-and-migrate/request-an-instance-migration.md): move an Instance off a node, step by step.
- [Request a transient scale](../../guides/scale-and-migrate/request-a-transient-scale.md): change a component's Instance count for a while.
- [Alfred](../scheduling/alfred.md): how Alfred finds Instances to move.
- [Configure pod disruption budgets](../../guides/deploy-models/configure-pod-disruption-budgets.md): limit how many Instances a node drain takes down at once.
