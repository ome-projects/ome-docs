---
title: Canary progression
description: Canary steps move a share of each component's Instances to a new revision, then advance at once, on a timer, on a promote or on Prometheus analysis.
since: v1.3
---

A canary moves a new revision onto a share of a component's [Instances](../../concepts/omenative/instances.md) at a time, and waits at a gate after each step. The gate opens at once, after a timed pause, when you promote the step, or when Prometheus analysis passes. A rollback, from you or from a failed analysis, returns every Instance to the stable revision. You declare a canary in the `canary` block of a [rollout group](../../concepts/rollouts-and-traffic/rollout-groups.md) in an [InferenceService](../../concepts/serving/inference-services.md). To act on a running canary, see [Promote or roll back a canary](../../guides/roll-out-changes/promote-or-roll-back-a-canary.md).

!!! warning "Capacity sets the canary's share of requests"
    Requests reach every ready Instance of the component, whatever its revision, so the new revision's share of requests follows its share of ready Instances. OME records each step's `traffic` weight in status, but doesn't route by it. To limit the requests a new revision gets, limit its `capacity`. [Traffic weight in status](#traffic-split) has the details.

A canary group's components must run on [OMENative](../../concepts/architecture/deployment-modes.md). Set `spec.deploymentMode: OMENative`, or `ome.io/deploymentMode: OMENative` in the component's `annotations`. A multi-node component needs this too, even though it runs on OMENative by default.

## Example

The InferenceService `chat` rolls its engine out in three steps:

```yaml title="chat.yaml"
apiVersion: ome.io/v1beta1
kind: InferenceService
metadata:
  name: chat
  namespace: prod
spec:
  deploymentMode: OMENative
  model:
    name: llama-3-2-1b-instruct
  engine:
    minReplicas: 4
    maxReplicas: 4
  rollout:
    groups:
      - components: [engine]
        canary:
          scaleDownDelaySeconds: 60
          readyTimeout: 20m
          steps:
            - capacity: "25%"
              traffic: 10
              pause:
                duration: 10m
            - capacity: "50%"
              traffic: 50
              pause: {}
            - capacity: "100%"
              traffic: 100
```

| Step | `capacity` | Instances on the new revision | `traffic` | Gate |
| --- | --- | --- | --- | --- |
| 1 | `"25%"` | 1 of 4 | 10 | Timed: the step advances 10 minutes after its new Instance is ready. |
| 2 | `"50%"` | 2 of 4 | 50 | Manual: the step waits for the alpha `kubectl ome rollout promote`. |
| 3 | `"100%"` | 4 of 4 | 100 | Immediate: the canary completes 60 seconds after all 4 new Instances are ready. |

Each step first waits for its new Instances to be ready, and the canary fails if a step waits longer than 20 minutes. [`kubectl ome rollout status`](../kubectl-ome/rollout.md#status) shows the canary's step, gate and phase.

A canary runs when a change, such as a new image, gives the engine a new revision while an earlier one serves. So the first revision of `chat` rolls out without a canary, and adding the group starts nothing until the next change. The canary keeps the plan it started with: an edit to the `canary` block waits for the next run. The alpha `kubectl ome rollout repin` applies it to the running canary, as [Repin a drifted rollout plan](../../guides/roll-out-changes/repin-a-drifted-rollout-plan.md) shows.

### Canary prefill and decode together

When an InferenceService declares both an engine and a decoder, a canary group that names one must name both, and the two roll out as one canary. The InferenceService `chat-pd` uses the model and the prefill-decode runtime that [Serve a prefill-decode model](../../guides/omenative/serve-a-prefill-decode-model.md) creates, on GPU nodes with RDMA NICs:

```yaml title="chat-pd.yaml"
apiVersion: ome.io/v1beta1
kind: InferenceService
metadata:
  name: chat-pd
  namespace: prod
spec:
  deploymentMode: OMENative
  model:
    name: llama-3-2-1b-instruct
  runtime:
    name: srt-llama-3-2-1b-instruct-pd
  engine:
    minReplicas: 4
    maxReplicas: 4
  decoder:
    minReplicas: 2
    maxReplicas: 2
  router:
    minReplicas: 1
    maxReplicas: 1
  rollout:
    groups:
      - components: [engine, decoder]
        canary:
          steps:
            - capacity: "50%"
              traffic: 50
              pause: {}
            - capacity: "100%"
              traffic: 100
```

Each step's `capacity` applies to each component. Step 1 moves 2 of the 4 engine Instances and 1 of the 2 decoder Instances to the new revision. It waits for all three to be ready, then for a promote. The router is in no group, so it rolls out on its own and balances requests across both revisions.

## Fields

| Field | Default | What it sets |
| --- | --- | --- |
| `steps` | Required | The steps, in order. |
| `steps[].capacity` | Required | How many of each component's Instances run the new revision: a percentage such as `"25%"`, or an integer. The last step's percentage must be `"100%"`. See [New-revision capacity](#new-revision-capacity). |
| `steps[].traffic` | Required | The new revision's weight in status, from 0 to 100. It can't fall from one step to the next, must be 100 at the last step, and above 0 needs a `capacity` above zero. |
| `steps[].pause` | None | A `duration` makes a timed gate, and `pause: {}` a manual one. See [How a step advances](#how-a-step-advances). |
| `steps[].analysis` | None | Prometheus checks that gate the step. See [Canary metric analysis](canary-analysis.md). |
| `readyTimeout` | The operator's, `15m` with the chart | How long a step can wait for its new Instances. Above zero. See [`readyTimeout`](#readytimeout). |
| `scaleDownDelaySeconds` | None | The canary completes at least this many seconds after the last step's new Instances are ready. Until then, a rollback still works and a last-step analysis keeps sampling. It isn't a drain window: a `"100%"` last step has already replaced the old Instances, and a rollback doesn't wait for it. |
| `prometheus` | The operator's default source | Where analysis queries go: `providerRef` or `serverAddress`. See [Canary metric analysis](canary-analysis.md#where-the-metrics-come-from). |

## New-revision capacity

`capacity` takes one of these forms:

| Form | Example | Instances on the new revision |
| --- | --- | --- |
| A quoted whole-number percentage, from `"0%"` to `"100%"` | `"25%"` | That share of the desired replicas, rounded up: `"25%"` of 3 is 1. |
| An unquoted integer | `2` | That many, up to the desired replicas. |
| A quoted number without `%` | `"2"` | None: admission rejects it. |

A component's desired replicas are the Instance count it is scaled to: the `spec.replicas` of its [InferenceReplica](../api/ome.v1beta1.md#ome-io-v1beta1-InferenceReplica), which OME fills from the component's resolved `minReplicas` — from the InferenceService, its runtime or the [replica defaults](../../guides/operate-ome/set-replica-defaults.md) — and which its [autoscaler](../../concepts/serving/component-autoscaling.md) moves as it scales. A step sizes against that live count, not the `minReplicas` floor: a `"50%"` step on an engine that an autoscaler has at 8 Instances stages 4 new Instances, even with `minReplicas: 4`. Only when the replica carries no count does OME fall back to the component's `minReplicas` in the InferenceService itself, then to its `maxReplicas`.

A step replaces Instances; it doesn't add any. The rest of the desired replicas stay on the previous revision. When an autoscaler changes the count during a step, the step re-sizes against it: a percentage keeps its share, and an integer keeps its count. Each component works out its own count, as the [prefill-decode example](#canary-prefill-and-decode-together) shows. For a multi-node component, capacity counts whole Instances, each a leader and its workers.

## How a step advances

A step starts by moving its capacity to the new revision, and waits in `Pending` until every component in the group has that many new Instances ready. At that moment, OME records the step's traffic, and the step's gate decides when the next step starts:

| Gate | The step sets | The step advances when |
| --- | --- | --- |
| Immediate | Only `capacity` and `traffic` | Its new Instances are ready. |
| Timed | `pause` with a `duration` | The duration has passed since its new Instances were ready. |
| Manual | `pause: {}` | You promote it with `kubectl ome rollout promote`. |
| Analysis | `analysis` | Its metric checks pass, after the optional `initialDelay` and no sooner than the step's `pause.duration`. See [Canary metric analysis](canary-analysis.md). |

GATE in `kubectl ome rollout status` shows these names. If a step's ready Instances drop below its count, the step goes back to `Pending`, and its clock starts again once its new Instances are ready. The one capacity loss a step doesn't wait out is a [crash-looping canary pod](#crash-looping-canary-pods): it parks the canary at `Failed` before OME checks any gate. To end a step's wait yourself, [force the step](#force-a-step-past-its-capacity-wait). At the last step, the gate holds the canary in `Promoting` until it passes.

## Force a step past its capacity wait

A step whose new Instances can't all become ready waits in `Pending`, and a [ready timeout](#readytimeout) ends that wait at `Failed`. The `ome.io/rollout-promote-force` annotation ends it with an advance instead. Set it to the canary's `canaryRevisionHash`, from the canary status of the group's [primary component](#phases); no `kubectl ome` command sets it:

```bash
kubectl get inferenceservice chat -n prod \
  -o jsonpath='{.status.components.engine.canary.canaryRevisionHash}'
```

```output
bbbbbbbb
```

```bash
kubectl annotate inferenceservice chat -n prod ome.io/rollout-promote-force=bbbbbbbb
```

```output
inferenceservice.ome.io/chat annotated
```

One force moves the canary one step, at any step:

- The step stops waiting for its new Instances. Its capacity wait keeps no clock, so it can't park at `Failed`, and OME still asks for the step's count, so capacity keeps converging behind the advance.
- The step's gate — manual, timed or analysis — opens without a separate promote, and the canary advances. The next step's capacity wait and gate apply as usual.
- Status records the step's `traffic`, but the written [weights](#traffic-split) follow the pods that serve: while no new Instance serves, the stable revision keeps every request, so a forced step never routes traffic onto nothing.

OME emits the Warning event `CanaryStepForced` on the InferenceService, naming the step, counted from 0, the revision, and how many of the step's Instances were ready, and removes the annotation once the advance is saved. On the last step, the force opens the gate into `Promoting` and stays until the canary completes: the completion waits, without parking, for every Instance to serve the new revision and for [`scaleDownDelaySeconds`](#fields), however long that takes. To force another step, wait for OME to remove the annotation, then set it again: one force is one step's. A value that isn't the current canary's revision hash does nothing, and admission rejects one that isn't lowercase letters and digits, at least 6 of them.

The force never overrides:

- a [paused or frozen rollout](../../guides/roll-out-changes/pause-and-resume-a-rollout.md): the canary holds its step, and the force doesn't survive the pause to act on resume;
- a `Failed` canary, parked by a ready timeout or a [crash-looping canary pod](#crash-looping-canary-pods);
- a rollback, rolling or held.

In each case OME removes the annotation without advancing anything, and records a `CanaryForceIgnored` warning event that names the reason. The plain [`ome.io/rollout-promote`](../api/labels-and-annotations.md#rollout-actions) is the opposite trade: it opens the step's gate, but waits for the step's new Instances.

## Phases

The canary's phase is in `status.components.<component>.rolloutPhase` of the group's primary component: the router when the group includes it, otherwise the engine. PHASE in `kubectl ome rollout status` shows it:

| Phase | Meaning |
| --- | --- |
| `Pending` | The step waits for its new Instances to be ready. |
| `Canarying` | The step's new Instances are ready, and OME checks its gate. |
| `Paused` | The canary waits at a gate before the last step, or holds after a [repin](../kubectl-ome/rollout.md#repin). |
| `Promoting` | The last step's new Instances are ready, and the canary waits for its gate and for [`scaleDownDelaySeconds`](#fields). |
| `Stable` | The canary completed. |
| `Failed` | A step ran past its [ready timeout](#readytimeout), a canary pod kept dying after it served (see [Crash-looping canary pods](#crash-looping-canary-pods)), or an analysis rollback found a [stable revision with no live Instance](canary-analysis.md#when-the-stable-revision-runs-no-instance). Whatever the reason, the park holds every member's roll where it stands: see [What a `Failed` canary holds](#what-a-failed-canary-holds). |
| `RollingBack` | A rollback is moving Instances back to the stable revision. |
| `RolledBack` | Every Instance is back on the stable revision. |

## While a canary runs

| When | The canary |
| --- | --- |
| A change gives the group a newer revision | Starts again at step 1 with the newest revision, and keeps the same stable revision. A `Failed` canary starts again too, when the newest revision is neither its own nor the stable one: see [`readyTimeout`](#readytimeout). |
| You pause the rollout | Holds its step, but its clocks keep running, so it can advance or fail as soon as you resume. See [Pause and resume a rollout](../../guides/roll-out-changes/pause-and-resume-a-rollout.md). |
| You set [`ome.io/rollout-promote-force`](#force-a-step-past-its-capacity-wait) to its revision hash | Stops waiting for the step's new Instances, opens the step's gate and moves one step, while capacity converges behind the advance. Paused, `Failed`, rolling back or rolled back, it doesn't move: OME removes the annotation and records a `CanaryForceIgnored` warning event instead. |
| You run the alpha `kubectl ome rollout rollback`, or [its analysis](canary-analysis.md#how-a-sample-is-judged) rolls it back | Records 0% for the new revision at once, and moves every Instance back to the stable revision. The component stays on the stable revision until a change produces a revision other than those two. See [Roll back the canary](../../guides/roll-out-changes/promote-or-roll-back-a-canary.md#roll-back-the-canary). |
| A rollback finds a component's stable revision with no live Instance | From the analysis, the rollback doesn't start: the canary parks at `Failed` where it stands, records the reason `StableRevisionMissing`, and emits the Warning event `CanaryStableRevisionEmpty`. From you, it proceeds, with the same Warning event. See [When the stable revision runs no Instance](canary-analysis.md#when-the-stable-revision-runs-no-instance). |
| A step runs past its [ready timeout](#readytimeout) | Goes to `Failed`, and [the roll holds where it stands](#what-a-failed-canary-holds). A stalled analysis with `onInconclusive: RollbackOnStall` rolls it back instead. |
| A canary pod keeps dying after it served | Parks at `Failed` where it stands, returns the step's recorded traffic to the stable revision, and emits the Warning event `CanaryPodCrashed`. See [Crash-looping canary pods](#crash-looping-canary-pods). |

## Traffic weight in status {#traffic-split}

When a step's new Instances are ready, OME records the step's `traffic` as the new revision's weight and the rest as the stable revision's. It writes both in `status.components.<component>.traffic` of the primary component:

| Field | Value |
| --- | --- |
| `revisionName` | The name of the revision's own Service, `<isvc>-<component>-rev-<hash>`. |
| `percent` | The revision's weight. A revision with a weight of 0 is left out. |
| `tag` | `canary` for the new revision, `stable` for the previous one. |
| `latestRevision` | `true` for the new revision. |
| `pairingProtocol` | The revision's `spec.rollout.pairingProtocol`, when it has one. |

`kubectl ome rollout status` shows the weight in TRAFFIC, as `target% -> observed%`, and `explain` shows the canary's revisions: see [`kubectl ome rollout`](../kubectl-ome/rollout.md#status-output-fields). At step 1 of the [example](#example), status records 10% for the new revision, but it has a quarter of the ready Instances and gets about a quarter of the requests.

## `readyTimeout`

The ready timeout bounds how long a step waits in `Pending` for its new Instances, and how long an analysis step goes without a conclusive result. The first of these that's set applies:

| Order | Setting | Notes |
| --- | --- | --- |
| 1 | The `ome.io/rollout-ready-timeout` annotation on the InferenceService | A duration, such as `30m`. OME ignores zero or less, and admission warns (`RolloutTimeoutTooShort`) about a value under a minute. |
| 2 | `readyTimeout` in the group's `canary` | Above zero. |
| 3 | `defaultReadyTimeout` in the `rollout` block of the `inferenceservice-config` ConfigMap | `15m` with the ome-resources chart (`ome.controller.rollout.defaultReadyTimeout`). The kustomize manifests don't set it. |

With none set, a step never times out: it waits in `Pending` until its Instances are ready, or until you [force the step](#force-a-step-past-its-capacity-wait). A [crash-looping canary pod](#crash-looping-canary-pods) still parks the canary at `Failed`, timeout or none. The timeout counts from when the step last entered `Pending`. For an analysis, it counts from the last conclusive sample, or from when the step's new Instances were ready if there's none yet.

When it expires, the phase is `Failed`. The canary keeps its Instances and its recorded traffic, the stable Instances keep serving, and `promote` refuses. No further Instance of any member moves to the canary revision, and OME announces the hold once with the Warning event `CanaryRollHeld`: see [What a `Failed` canary holds](#what-a-failed-canary-holds). A [force](#force-a-step-past-its-capacity-wait) doesn't lift the park either: it only ends a live step's wait before the timeout expires. Apply a fix that gives the group a revision other than the failed canary's and the stable one, and a fresh canary starts at step 1 toward it, with the same stable revision. A [rollback](../../guides/roll-out-changes/promote-or-roll-back-a-canary.md#roll-back-the-canary) also works on a failed canary: every Instance returns to the stable revision, as [When a step never becomes ready](../../guides/roll-out-changes/promote-or-roll-back-a-canary.md#when-a-step-never-becomes-ready) shows. A [crash-looping canary pod](#crash-looping-canary-pods) parks the canary the same way, except that the step's recorded traffic returns to the stable revision at once.

## Crash-looping canary pods

A canary pod that keeps dying after its Instance served parks the canary where it stands. OME reads the crash before the step's capacity, its ready timeout and its gate, so the park happens at any step, with or without a [ready timeout](#readytimeout) set:

- The phase goes to `Failed` at the current step, and `status.components.<component>.canary.failed.reason` records `CapacityTimeout`, the same reason as a ready-timeout park. The event tells the two apart.
- The step's recorded traffic returns to the stable revision: status records 0% for the new revision, where a ready-timeout park keeps the step's [weight](#traffic-split).
- OME emits the Warning event `CanaryPodCrashed` on the InferenceService, naming the component, the pod, its termination detail and the step, counted from 0. In a group of several components, a crash in any member's canary pod parks the group's canary.

The canary keeps its Instances, and the stable Instances keep serving. Recovery is the same as for any `Failed` canary: a [rollback](../../guides/roll-out-changes/promote-or-roll-back-a-canary.md#roll-back-the-canary) returns every Instance to the stable revision, and a fix that gives the group a revision other than the parked canary's and the stable one starts a fresh canary at step 1. The hold is the same too: [What a `Failed` canary holds](#what-a-failed-canary-holds) says what stops moving, and shows the `CanaryRollHeld` event that follows `CanaryPodCrashed`.

OME reads a crash loop only from the pods of the new revision, and only from their runner container, `ome-container`: a stable pod's crash, or a sidecar or init container that restarts, parks nothing. The pod's Instance must have served first:

| The runner | The canary |
| --- | --- |
| Crashes before its Instance is first ready | Waits in `Pending`, under the [ready timeout](#readytimeout). The crash park never fires. |
| Dies once after its Instance served | Waits out the dip in capacity: the kubelet restarts the container in place, or the [Instance restart policy](../../concepts/omenative/instance-restart-policy.md) rebuilds the Instance. |
| Dies a second time after its Instance served | Parks at `Failed`, whether the kubelet has restarted the runner again or is backing off. Deaths count from when the canary started, across its steps. |
| Dies in the rebuilt pod, after a rebuild for a death during the canary | Parks at `Failed`: the Instance's second death under one repair. |

[When a canary pod keeps crashing](../../guides/roll-out-changes/promote-or-roll-back-a-canary.md#when-a-canary-pod-keeps-crashing) shows the event and the recovery.

## What a `Failed` canary holds

Whatever parks the canary at `Failed` — a [ready timeout](#readytimeout), a [crash-looping canary pod](#crash-looping-canary-pods) or a rollback that finds [no stable Instance to return to](canary-analysis.md#when-the-stable-revision-runs-no-instance) — the park stops the whole group's roll where it stands:

- From the pass the canary parks, OME holds every Instance of every member on the revision it runs: the step's new Instances keep the canary revision, the rest keep the stable one, and no further Instance of any member moves to the parked canary's revision.
- One kind of move finishes: an Instance whose rebuild onto the canary revision is already in flight completes it, since holding it would strand its replacement pods. OME starts no new move, so a member that was still staging its step's capacity stops short of it.
- A held Instance that loses its pods is rebuilt on the revision it runs, canary or stable: the [Instance restart policy](../../concepts/omenative/instance-restart-policy.md) repairs it where it stands, it doesn't move it.
- The traffic split stays as the park left it: a ready-timeout park keeps the step's recorded [weight](#traffic-split), where a crash park has already returned it to the stable revision.

OME announces the hold once, on the pass that records the park, with the Warning event `CanaryRollHeld` on the InferenceService — after the event that names the cause's own detail where there is one, such as `CanaryPodCrashed` or `CanaryStableRevisionEmpty`. The message names the group's primary component, the step, counted from 0, the reason that `status.components.<component>.canary.failed.reason` records — `CapacityTimeout`, `AnalysisStalled` or `StableRevisionMissing` — and how far each member's roll had come: its ready Instances on the canary revision against the Instance count it is scaled to, the primary first and the other members by name. For the [prefill-decode example](#canary-prefill-and-decode-together), parked by a ready timeout at its first step with one engine Instance and one decoder Instance ready:

```bash
kubectl get events -n prod --field-selector reason=CanaryRollHeld -o custom-columns=TYPE:.type,OBJECT:.involvedObject.name,MESSAGE:.message
```

```output
TYPE      OBJECT    MESSAGE
Warning   chat-pd   engine canary stopped at step 0 (CapacityTimeout): the roll is held where it stands, engine 1 of 4 Instances on revision bbbbbbbb, decoder 1 of 2 Instances on revision dddddddd, and the traffic split stays as written; a rollback (ome.io/rollout-rollback), a resume (ome.io/rollout-resume) or a new revision ends the hold
```

The hold ends the three ways the message names:

- A [rollback](../../guides/roll-out-changes/promote-or-roll-back-a-canary.md#roll-back-the-canary) — `ome.io/rollout-rollback`, or the alpha `kubectl ome rollout rollback` — proceeds from the park and returns every Instance to the stable revision. The one park it can't lift is a canary parked because no ControllerRevision is retained for its stable revision: there OME removes the request and records a `CanaryRollbackIgnored` warning event, as [Rollout actions](../api/labels-and-annotations.md#rollout-actions) describes.
- A resume — the `ome.io/rollout-resume` annotation set to the parked canary's revision hash; no `kubectl ome` command sets it — re-enters the canary at step 1 toward the same revision, with every gate running again. [The service has no recognized pause](../../guides/roll-out-changes/pause-and-resume-a-rollout.md#the-service-has-no-recognized-pause) shows it.
- A change that gives the group a revision other than the parked canary's and the stable one starts a fresh canary at step 1 toward it.

A pause doesn't end the hold — a `Failed` canary stays parked through a pause and resume — `promote` refuses while it stands, and a [force](#force-a-step-past-its-capacity-wait) is removed without effect, with a `CanaryForceIgnored` warning event.

## What admission rejects

The webhook rejects a canary with a message that names the field and gives one of these reasons in parentheses:

| Reason | Cause |
| --- | --- |
| `CanaryRequiresOMENative` | A component in the group doesn't declare OMENative. `deploymentMode=""` means it sets neither the field nor the annotation. |
| `CanaryInvalid` | A step or `readyTimeout` breaks a rule in [Fields](#fields), or a `capacity` isn't one of the forms in [New-revision capacity](#new-revision-capacity). |
| `CanaryInvalid` | The group names a component other than `router`, `engine` or `decoder`, or one the InferenceService doesn't declare, or sets `maintainRatio`. |
| `CanaryInvalid` | The InferenceService has an engine and a decoder, and the group names one: `a canary group naming either must name both`, or for the decoder, `must include its entrypoint Component "engine"`. |
| `MultipleCanaryGroups` | Two canary groups cover the same unit: the router, or the engine and decoder. |
| `GroupOrderingNotHonored` | A canary group sits beside other groups without `spec.rollout.groupOrdering: Concurrent`. |
| `SoakNotHonored` | The canary group sets `soak`. |
| `InvalidDuration` | The `ome.io/rollout-ready-timeout` annotation isn't a duration. |
| `AnalysisInvalid` | An analysis breaks a rule in [Canary metric analysis](canary-analysis.md#what-admission-checks). |

The API server itself rejects more than 20 steps, or a `traffic` outside 0 to 100. [Rollout groups](../../concepts/rollouts-and-traffic/rollout-groups.md#what-admission-rejects) lists the rules for every group.

## Related pages

- [Canary metric analysis](canary-analysis.md): the metric checks an analysis step runs.
- [Promote or roll back a canary](../../guides/roll-out-changes/promote-or-roll-back-a-canary.md): act on a canary at its gate.
- [Pause and resume a rollout](../../guides/roll-out-changes/pause-and-resume-a-rollout.md): hold a rollout, canary included.
- [Serve a prefill-decode model](../../guides/omenative/serve-a-prefill-decode-model.md): run the engine and decoder that the prefill-decode example rolls out.
- [Rollout groups](../../concepts/rollouts-and-traffic/rollout-groups.md): how groups divide a rollout between components.
- [Deployment modes](../../concepts/architecture/deployment-modes.md): how to opt in to OMENative.
- [`kubectl ome rollout`](../kubectl-ome/rollout.md): the commands that show and act on a canary.
- [GroupCanary](../api/ome.v1beta1.md#ome-io-v1beta1-GroupCanary) in the API reference.
