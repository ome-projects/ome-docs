---
title: Rollout groups
description: Rollout groups roll an InferenceService's OMENative components out as one, so prefill and decode change together, by canary, blue-green or rolling update, with metric-gated canary steps.
since: v1.3
---

A rollout group rolls a change out to components of an [InferenceService](../serving/inference-services.md) as a unit. Put the engine and the decoder in one group, and prefill and decode move to each new revision together, by blue-green, rolling update or canary. A canary moves capacity in steps, and a step can wait for a timer, for you, or for Prometheus analysis that advances the canary or rolls it back.

You declare up to three groups in `spec.rollout.groups`. A component can be in only one group, and a component in no group rolls out on its own.

Groups need [OMENative](../omenative/overview.md) declared on the InferenceService, with `spec.deploymentMode: OMENative` or the component's `ome.io/deploymentMode` annotation. A component that's OMENative only because of its `leader` and `worker`, or its runtime, doesn't count, as [Opt in to OMENative](../architecture/deployment-modes.md#opt-in-to-omenative) explains. A single-node component runs as RawDeployment by default, where a change is a plain Deployment update, with no group.

## Roll prefill and decode together

This InferenceService serves the pre-configured model `mistral-7b-instruct-v0-2`. Its prefill-decode runtime, `srt-mistral-7b-instruct-pd`, runs a router, an engine and a decoder, and needs GPU nodes with RDMA NICs, as [Serve a prefill-decode model](../../guides/omenative/serve-a-prefill-decode-model.md) shows. One group holds the engine and the decoder:

```yaml title="mistral-pd.yaml"
apiVersion: ome.io/v1beta1
kind: InferenceService
metadata:
  name: mistral-pd
  namespace: mistral-demo
spec:
  deploymentMode: OMENative
  model:
    name: mistral-7b-instruct-v0-2
  runtime:
    name: srt-mistral-7b-instruct-pd
  engine:
    minReplicas: 1
    maxReplicas: 1
  decoder:
    minReplicas: 1
    maxReplicas: 1
  router:
    minReplicas: 1
    maxReplicas: 1
  rollout:
    groups:
      - components: [engine, decoder]
```

Apply the file:

```bash
kubectl apply -f mistral-pd.yaml
```

```output
inferenceservice.ome.io/mistral-pd created
```

- When a change gives both a new revision, they roll out together by [blue-green](#choose-a-progression), since the group sets no progression, paced in [batches](#batches-for-the-engine-and-the-decoder).
- The router is in no group, so it rolls out on its own.
- While a component's update waits, `status.components.<component>.lifecycle.rolloutHold` names the gate that holds it. [Lifecycle status](../architecture/deployment-modes.md#lifecycle-status) lists the gates.
- An edit to `spec.rollout` during a rollout [applies to the next one](rollout-policy.md#when-the-reference-is-resolved).

Each group can set:

| Field | Description |
| --- | --- |
| `components` | Required. One to three of `router`, `engine` and `decoder`. |
| `canary`, `blueGreen` or `rollingUpdate` | The progression. Set at most one. See [Choose a progression](#choose-a-progression). |
| `policyRef` | Alpha, and off by default. A [RolloutPolicy](rollout-policy.md) that supplies the progression when the group sets none. |
| `soak` | In a [sequence](#roll-components-one-at-a-time), how long to wait between components. |
| `maintainRatio` | For a blue-green or rolling-update group of two or more components. Keeps them in step, and their replica ratio within `tolerance` percent. See [Rolling update](#rolling-update) and [Batches for the engine and the decoder](#batches-for-the-engine-and-the-decoder). |

## Choose a progression

A group rolls out blue-green unless it sets another progression.

| Progression | How the group rolls out | Set |
| --- | --- | --- |
| Blue-green | Its components start together. Each updates at most 25% of its replicas at a time, rounded up, while both revisions serve requests. | Nothing, or `blueGreen: {}` |
| Rolling update | Each component moves within its own `maxSurge` or `maxUnavailable` budget, and `maintainRatio` can keep them in step. | `rollingUpdate` |
| Canary | Steps move a share of capacity to the new revision, and a step can wait for you or for metric analysis. | `canary` |

In a blue-green or rolling-update group, a failed Instance in any component marks the whole group failed. OME records a `CoordinationGroupFailed` Warning event on the InferenceService, but doesn't stop the other components.

### Rolling update

`maxSurge` and `maxUnavailable` set each component's own budget, as a count or a percentage, 25% by default. With the default `SurgeThenDrain` [update strategy](../architecture/omenative-update-strategies.md), a component starts a new Instance before it stops an old one, within `maxSurge`. With any other strategy, it moves within `maxUnavailable`.

An edit that changes some of the group's components and not others is admitted: the changed components roll out, and the rest keep their revision and roll nothing. The webhook warns, once per group, naming who moves and who stays: `spec.rollout.groups[0]: only the engine changes; the decoder keeps its revision and rolls nothing (RollingUpdatePartialChange)`. A component counts as changed when the edit re-renders its pod template — its image, but also an env, a command, a resource, a volume, a label or an annotation — while a change that re-renders no pods, such as its replica counts or its `lifecycle` block, draws no warning. The group rolls one member alone because any mix of its members' revisions is expected to serve together; when the engine-decoder wire protocol makes a mix unsafe, change it through [`pairingProtocol`](#change-the-engine-decoder-wire-protocol), which needs a blue-green or canary group.

This group moves four engines and two decoders one Instance at a time, and keeps their 2:1 ratio:

```yaml
spec:
  engine:
    minReplicas: 4
    maxReplicas: 4
  decoder:
    minReplicas: 2
    maxReplicas: 2
  rollout:
    groups:
      - components: [engine, decoder]
        rollingUpdate:
          maxSurge: 1
          maxUnavailable: 0
        maintainRatio:
          tolerance: 25
```

- `maxSurge: 1` lets each component start one new Instance at a time. Under another strategy, `maxUnavailable: 0` would stall it.
- `maintainRatio` keeps the two in step. OME holds a component while it's more than 25% further through its rollout than the other. It also holds an update that would move their serving ratio more than 25% from 2:1 — though never for good: when no single step of a component could fit that band, OME admits such a step rather than hold the roll for good, as described below. The held component's `rolloutHold` gate is `Ratio`.

`tolerance` runs from 0 to 100, and 0 allows no drift. When you leave it out, it's 5, the `ome-resources` value `ome.controller.coordination.defaultRatioTolerancePercent`. Above 50, the webhook warns that the check is effectively off (`RatioToleranceTooHigh`).

An update that `InPlaceIfPossible` or `InPlaceOnly` [applies in place](../architecture/omenative-update-strategies.md#in-place-updates) skips only the serving-ratio half of the check, since the patched pod returns with its capacity. The update still waits, at the same `Ratio` gate, while its component is more than `tolerance` percent further through its rollout than the other. When it goes ahead, OME records a Normal `RatioGateBypassed` event on the InferenceService: the serving-capacity band is waived for an in-place start, and the new-revision lockstep still applies.

Neither hold wedges the group below the replica counts it asks for. A component that serves fewer pods than its desired count — an Instance failed mid-roll, or a node outage took its pods — recovers capacity through the same `Ratio` gate: OME starts one new Instance at a time for it, even while the serving-ratio band refuses its ordinary steps. A recovery step never takes the component past its desired count, and it waits while the component's serving capacity, as a share of that count, is ahead of a peer's that is also short, so recovery can't buy a lead over a still-rolling peer. A held component that starts a new Instance is refilling lost capacity, not advancing its rollout past the gate.

The further-through hold yields to recovery only for a component that has lost capacity: one serving fewer pods than the Instances it has, not counting Instances a scale-up is still creating. Raising a component's replicas mid-rollout is not lost capacity. OME creates the extra Instances right away, outside these holds, but a component serving every Instance it had keeps the hold: when it's more than `tolerance` percent further through its rollout than the other, it waits at the `Ratio` gate until the other catches up, even though it now desires more. The serving-ratio band measures against the counts you ask for now, so the other component's updates can also wait at the same gate while the extra Instances come up; they go ahead once the new Instances serve.

Nor does the band hold a roll that no single step could ever satisfy. At some shapes the band is narrower than one Instance: two components of four Instances each under a tolerance of 25 read one pod out as 4:3, 33 points off 1:1, whichever side drains. When no single step of a component fits the band, OME admits the step rather than park the roll at the `Ratio` gate for good. The step starts from a component serving every Instance it asks for, in a group whose serving ratio sits inside the band — a component short of a pod recovers it first, as above — and the further-through hold still paces the component against its peer, so the allowance never buys a lead of more than the tolerance and one step. What the admitted step may do depends on its direction:

- A surge only adds serving capacity: it never lowers the other component's count, and the extra pod is transient — once the new Instance serves, the old one drains. So the surge the band can't fit goes ahead, however far it reads: one replica surging beside one reads 2:1, 100 points off 1:1, and a 4:4 pair under a tolerance of 10 surges to 5:4, 25 points off. It draws no event, and the group's `maxSurge` still caps how many surges run at once.
- A drain takes serving capacity out, so OME admits one pod of the component out of rotation at a time: until the replacement serves and the group is back at its reference, the component's next drain waits at the `Ratio` gate, and the group's off-band ratio refuses the other components' drains too. A drain that moves the serving ratio no more than twice the `tolerance`, with the component keeping a pod serving, is the ordinary overshoot of a drain-first roll under a narrow band, and draws no event. Past that — or when a single-replica component's only drain leaves it serving nothing, the drain-first step you chose for it — the serving ratio is outside the tolerance for as long as the step runs, and OME records a Warning `RatioStepOutsideTolerance` event on the InferenceService with the counts and the tolerance.

A `RatioStepOutsideTolerance` event therefore names a shape the webhook below would refuse — reached without asking it. Take eight engines patched in place beside a decoder on `RecreatePod` with no `minReplicas` and a `maxReplicas` of 4, under a `tolerance` of 20 — a pair these holds pace when the operator's [`keepRatio`](../../guides/operate-ome/configure-the-controller.md#pace-a-prefill-decode-roll) rule is off, since a single-pod in-place engine counts no moves. The webhook admits the spec, sizing the decoder at its `maxReplicas` of 4: one decoder pod out reads 8:3, 33 points off 8:4, within the 40 it grants a single drain. Then the decoder's [autoscaler](../serving/component-autoscaling.md#what-the-autoscaler-scales) scales it to 2 — a scale lands on the component's InferenceReplica, not on the spec the webhook judges — and at 8:2 no decoder step fits: one pod out reads 8:1, 100 points off. OME admits the decoder's first drain all the same, and records the Warning: `RatioStepOutsideTolerance: decoder 1 of 2 serving after the drain; no single step fits tolerance 20 at 8:2; admitting one. The serving ratio is outside the tolerance while this step runs; the band admits one such step at a time rather than hold the roll for good. Widen the tolerance or resize the group to a shape one step keeps inside it.` The ratio reads 8:1 until the replacement decoder serves, and the second drain repeats the event. To stop them, do what the message says: widen `maintainRatio.tolerance` to 50, the smallest whose doubled overshoot covers the 100 points, or return the group to a shape one step keeps in band, such as more decoder replicas.

The webhook rejects a `maintainRatio` group whose drain-first member has no first step the band admits silently (`RatioDrainInfeasible`): rather than let a group roll only through `RatioStepOutsideTolerance` steps, it refuses the spec. A member drains first when it updates by `RecreatePod`, or by an in-place strategy on a multi-pod [Instance](../omenative/instances.md), which recreates the whole gang. The webhook projects its first drain — one Instance out of rotation — against each other member of the group, the router included, and rejects the spec when the drain would leave the member with nothing serving, or would move a pair's serving ratio more than twice the `tolerance` from the ratio the rollout starts at — the overshoot the serving-ratio band grants a single drain from a fully serving group. It sizes each member the way the rollout does — `minReplicas` when it's positive, else `maxReplicas`, else the [`defaultMinReplicas`](../../guides/operate-ome/set-replica-defaults.md) fill — and judges at the group's `tolerance`, or the default above when the group sets none.

In this group, no decoder step fits the band or its silent overshoot: one of its two pods out reads the serving ratio as 6:1, 100 points off the starting 6:2, and twice the tolerance of 20 caps a single silent drain at 40 points:

```yaml
spec:
  engine:
    minReplicas: 6
    maxReplicas: 6
    lifecycle:
      updateStrategy:
        type: InPlaceIfPossible
  decoder:
    minReplicas: 2
    maxReplicas: 2
    lifecycle:
      updateStrategy:
        type: RecreatePod
  rollout:
    groups:
      - components: [engine, decoder]
        rollingUpdate:
          maxUnavailable: 1
        maintainRatio:
          tolerance: 20
```

The rejection names the ways out: `spec.rollout.groups[0]: the decoder rolls drain-first (RecreatePod) in a maintainRatio group, and its first step could never be admitted: with decoder 2 and engine 6, one decoder pod out reads decoder 1 : engine 6, 100 points off the starting ratio, and the ratio check lets a single drain go at most 40 points (twice the tolerance of 20), so the rollout would park for good; widen maintainRatio.tolerance to at least 50, give the decoder the SurgeThenDrain update strategy, add decoder replicas, or give the engine a SurgeThenDrain or RecreatePod update strategy, which hands the pacing to the batch step (RatioDrainInfeasible)`. The tolerance it suggests is the smallest that admits the drain. A drain-first member with a single replica is rejected at any tolerance — its only drain leaves it with nothing serving — with the same ways out except the tolerance.

The webhook skips a group the [batches below](#batches-for-the-engine-and-the-decoder) pace: one that lists both the engine and the decoder, each with a declared strategy that counts its moves — `SurgeThenDrain`, `RecreatePod`, or an in-place strategy on multi-pod Instances. It reads only what the spec declares: a component with no `lifecycle.updateStrategy` surges by default at runtime but doesn't count here, and the webhook doesn't read the operator's `keepRatio` rule below either. So the last way out above is literal — declare `SurgeThenDrain` or `RecreatePod` on the engine or the decoder, and the batches pace the group instead. The rule runs on updates too: a scale-down or a strategy change that gives a group one of these shapes is refused the same way.

### Batches for the engine and the decoder

OME paces a blue-green or rolling-update group that lists both the engine and the decoder in batches instead, whether or not it sets `maintainRatio`, when each of the two counts its moves in a step of its own:

- A component that drains first — it updates by `RecreatePod`, or by an in-place strategy on a multi-pod [Instance](../omenative/instances.md), which recreates the whole gang — counts its drains. Its step is the stricter of the group's `maxUnavailable` resolved against its replicas, rounded up for a percentage, and its own [`lifecycle.updateStrategy.rollingUpdate.maxUnavailable`](../architecture/omenative-update-strategies.md#pacing-with-rollingupdate).
- A component on the default `SurgeThenDrain` [update strategy](../architecture/omenative-update-strategies.md) counts its surges, the new Instances it starts. Its step is the stricter of the group's `maxSurge` — 25% when the group sets none — and its own [`lifecycle.updateStrategy.rollingUpdate.maxSurge`](../architecture/omenative-update-strategies.md#pacing-with-rollingupdate), resolved the same way.

No component starts its next batch until every component's current batch serves on the new revision, and the batches are what keep the components in step: for the batched components, OME consults neither the serving-ratio band nor the further-through hold above. A router in the group joins the batches when it counts its moves the way the engine or the decoder does, and stays under the holds above otherwise. A component that updates single-pod Instances in place counts no drains or surges, so it puts the group back under the holds above — unless the operator's `keepRatio` rule below makes it count its in-place updates against its `maxUnavailable` step, like a drain-first component.

With four engines and two decoders on `RecreatePod` and `maxUnavailable: "50%"`, the first batch drains two engines and one decoder together. The decoder's second drain then waits, at the same `Ratio` gate, with a reason that names the two batches: `decoder batch 2 waits for engine batch 1 to serve (0 of 2 serving)`. Both new engines serving releases it. An update attempt that ends without serving counts toward its batch until OME retries it, so a failed Instance doesn't hold the other components, and a batch still short counts its failures, as in `1 of 3 serving, 1 failed`.

The `ome-resources` chart adds two rules to these batches, on by default in its [`lifecycle.prefillDecodeRoll`](../../guides/operate-ome/configure-the-controller.md#pace-a-prefill-decode-roll) block — so on a default install, an engine-decoder group rolls in paired, decoder-first batches, even one that surges and sets no `maintainRatio`:

- `keepRatio: true` plans the two components' batches together, sized so that the share of each side already moving to the new revision stays within the group's `tolerance` of the other side's share — the operator default, the chart's 5, when the group names none. A move counts from the moment it starts, on the drain-first and the surge path alike. A batch that can't fit within the tolerance shrinks, and at least one Instance moves per batch, so a tight tolerance slows the roll and never stops it. A group opts out of the sizing with a `tolerance` of `100`.
- `firstSide: decoder` makes the decoder move first inside every batch: the engine's moves of a batch are admitted once the decoder's new Instances of that batch serve. Until then the engine waits, at the same `Ratio` gate, with a reason such as `engine batch 1 waits for decoder batch 1 to serve first (0 of 1 serving)`.

The OME binary applies neither rule without the operator's block. [Pace a prefill-decode roll](../../guides/operate-ome/configure-the-controller.md#pace-a-prefill-decode-roll) shows what each rule does, with a worked example, and how to turn one or both off.

### Canary

A canary group moves all its components through one canary's steps. Each step's `capacity` applies to each component: at `"25%"`, a quarter of the engine's replicas and a quarter of the decoder's, rounded up, run the new revision. The step waits until both are ready. When the InferenceService declares both, the engine and the decoder are one canary unit, so a canary group that names one must name both. The router is a unit of its own, and one canary group can hold both units.

```yaml
spec:
  rollout:
    groups:
      - components: [engine, decoder]
        canary:
          steps:
            - capacity: "25%"
              traffic: 10
              pause: {}
            - capacity: "100%"
              traffic: 100
```

The first step waits until you promote it. To promote a step, or to roll the canary back to its stable revision, see [Promote or roll back a canary](../../guides/roll-out-changes/promote-or-roll-back-a-canary.md). The router can run a canary of its own at the same time, under `groupOrdering: Concurrent`. The alpha `kubectl ome rollout promote` and `rollback` commands then refuse, since they act only on a rollout with one canary group. [Canary progression](../../reference/rollouts/canary-progression.md) describes the steps, and [Canary metric analysis](../../reference/rollouts/canary-analysis.md) describes their Prometheus checks.

!!! warning "Requests follow capacity, not the traffic weight"
    OME records each step's `traffic` weight in status, but its routes don't apply it. The new revision gets requests in proportion to its share of ready pods, which `capacity` sets.

## Change the engine-decoder wire protocol

`spec.rollout.pairingProtocol` is a token you choose, such as `kv-v1`, for the wire protocol between the engine and the decoder. Change it only when that protocol changes. A new token gives both components a new revision, and OME labels their pods with `ome.io/pairing-protocol`.

To move from one token to another, roll the engine and the decoder out in one blue-green group. If `mistral-pd` runs with `pairingProtocol: kv-v1`, this spec moves it to `kv-v2`:

```yaml
spec:
  rollout:
    pairingProtocol: kv-v2
    groups:
      - components: [engine, decoder]
        blueGreen: {}
```

While the change rolls out, OME keeps at least one engine and decoder pair with the same token serving, and keeps each component at its pairable floor: at least its desired replicas minus `maxUnavailable` [Instances](../omenative/instances.md) with a serving pod that still has a partner. A partner is a serving pod of the other component whose token is the same, or empty on either side, and a surge replacement counts from the moment it serves. `maxUnavailable` is the stricter of 25% of the component's desired replicas, rounded up, and its own [`lifecycle.updateStrategy.rollingUpdate.maxUnavailable`](../architecture/omenative-update-strategies.md#pacing-with-rollingupdate). The floor applies only when it comes to 2 or more: four engines with the default budgets keep a floor of 3, and with one or two replicas only the one-pair rule applies.

Under the default `SurgeThenDrain` update strategy, each component starts and serves its new-token pods before its old pods drain, and an old pod's drain waits, with the `rolloutHold` gate `Pairing`, in four cases:

- The component's own new pods can't pair yet, because no serving pod of the other component runs the new token, and the other component also updates by `SurgeThenDrain`. Retiring an old pod that still pairs would trade serving capacity for pods that can't pair, so the drain waits, with a reason such as `pairing protocol transition to "kv-v2": the replacement engine cannot pair because no serving decoder runs cohort "kv-v2" yet; holding the drain of the serving engine of cohort "kv-v1" until one does`. The other component's first pod to serve the new token releases the hold.
- The drain would push the other component below its floor, because the pods leaving are the last partners of its Instances still on the old token. The reason counts the shortfall, such as `pairing protocol transition to "kv-v2": draining the serving engine pods of cohort "kv-v1" would leave the decoder with 2 pairable serving Instance(s), below its floor of 3 (desired 4 minus maxUnavailable 1); holding the drain until the decoder Instances still on cohort "kv-v1" have another partner or have moved`. The hold releases once those Instances have another partner, through new-token replacements of their own, or have moved.
- The drain would push the component itself below its own floor: its replacement can't pair yet, and the other component's new token is already coming up without this drain, by surge or by an Instance mid-update toward it. The reason combines the two, ending in `retiring the serving engine of cohort "kv-v1" would leave the engine with 2 pairable serving Instance(s), below its floor of 3 (desired 4 minus maxUnavailable 1); holding the drain until one does`, and the other component's first pod to serve the new token releases it.
- The drain would take the last such pair out of service. It waits until the new token serves on both components.

A floor holds a drain only when the drain itself lowers the pairable count, so a component already below its floor doesn't block a drain that leaves the count unchanged. The holds can't deadlock, because a `SurgeThenDrain` component's new pods come up whether or not any drain waits. With that strategy on both components, even one engine and one decoder keep a pair serving through the change.

Under `RecreatePod` or an in-place strategy, a component runs the new token only once its old pods leave rotation, so OME checks both floors before a step takes its pod out of rotation, at the same `Pairing` gate. The step waits with a reason such as `pairing protocol transition to "kv-v2": updating a serving engine of cohort "kv-v1" would leave the engine with 2 pairable serving Instance(s), below its floor of 3 (desired 4 minus maxUnavailable 1); holding until cohort "kv-v2" serves on both Components`, until the new token serves on both components. With one engine and one decoder the change can briefly leave no pair.

The webhook rejects a change from one token to another when the engine and the decoder roll out separately or by rolling update (`PairingProtocolChangeUncoordinated`). It always accepts setting a token for the first time, or clearing it.

## Roll components one at a time

When the groups that aren't canaries are two or more blue-green groups of one component each, they form a sequence. OME rolls a sequence out one group at a time, in list order. With this `spec.rollout`, `mistral-pd` rolls out the decoder first, then the engine:

```yaml
spec:
  rollout:
    groups:
      - components: [decoder]
        soak: 10m
      - components: [engine]
```

When a change gives both a new revision, OME:

1. Rolls out the decoder, while the engine waits.
2. Waits out the 10-minute soak once the decoder finishes.
3. Rolls out the engine.

OME skips a component that the change leaves alone. Each wait lasts the longest `soak` of any group but the last, counted from when the component before it last finished a rollout.

While the decoder rolls out, the engine's `rolloutHold` shows why it waits:

```bash
kubectl get inferenceservice mistral-pd -n mistral-demo -o custom-columns='GATE:.status.components.engine.lifecycle.rolloutHold.gate,REASON:.status.components.engine.lifecycle.rolloutHold.reason'
```

```output
GATE         REASON
Sequential   Sequential waiting on decoder
```

During the soak, the reason is `Sequential.Soak: engine waiting out the 10m0s soak after decoder`.

If a component's rollout fails, the components after it wait, and OME records a `CoordinationGroupFailed` Warning event. The sequence goes on once another change, such as a fixed image or the previous one, makes the failed component healthy.

## How groups run together {#what-groupordering-promises}

Each canary group rolls out on its own, alongside the other groups. The other groups roll out at the same time, unless they form a [sequence](#roll-components-one-at-a-time).

`spec.rollout.groupOrdering` changes only what the webhook accepts, not how the groups roll out. `Sequential`, the default, accepts one group or a sequence alone. Any other list of two or more groups, such as a canary group beside another group, needs `groupOrdering: Concurrent`, or the webhook rejects it with `GroupOrderingNotHonored`.

## What admission rejects

When the webhook rejects a rollout, `kubectl` prints `admission webhook "inferenceservice.ome-webhook-server.validator" denied the request:`, then a message that ends in a reason in parentheses:

| Reason | Cause |
| --- | --- |
| `CoordinationRequiresOMENative` | A grouped component doesn't declare OMENative on the InferenceService. A canary group gets `CanaryRequiresOMENative`. |
| `InvalidComponentInCoordinationGroup`, `OrphanCoordinationGroup` | A group names a component other than `router`, `engine` or `decoder`, or one that the InferenceService doesn't declare. |
| `DuplicateComponentInCoordinationGroups` | A component is in two groups. |
| `GroupOrderingNotHonored` | Under `Sequential`, two or more groups other than a sequence alone. |
| `SoakNotHonored` | A `soak` outside a sequence. |
| `OrderNotHonored` | A group sets `order`, which no progression applies. |
| `InvalidRollingUpdateInteger`, `InvalidRollingUpdatePercent`, `ZeroBudgetPacingUnstartable` | A negative count, a string that isn't a percentage from `"0%"` to `"100%"`, or two budgets that resolve to zero. |
| `RatioDrainInfeasible` | A `maintainRatio` group's drain-first member's first drain would leave it with nothing serving, or move a pair's serving ratio more than twice the `tolerance`. See [Rolling update](#rolling-update). |
| `PairingProtocolChangeUncoordinated` | `pairingProtocol` changes tokens while the engine and the decoder roll out separately or by rolling update. |
| `RolloutPolicyRefUnsupported` | A group sets `policyRef` while RolloutPolicy is off. |

Not every reason in parentheses is a rejection: an edit that changes only some of a rolling-update group's components is admitted, with a `RollingUpdatePartialChange` warning. See [Rolling update](#rolling-update).

[Canary progression](../../reference/rollouts/canary-progression.md#what-admission-rejects) lists the rules for canary groups.

## Next steps

- [Promote or roll back a canary](../../guides/roll-out-changes/promote-or-roll-back-a-canary.md): move a canary past a step that waits for you, or return it to its stable revision.
- [`kubectl ome rollout`](../../reference/kubectl-ome/rollout.md): see where a rollout is, and the gate a canary step waits at.
- [Canary progression](../../reference/rollouts/canary-progression.md): every field of a canary step, and how a step advances.
- [Canary metric analysis](../../reference/rollouts/canary-analysis.md): gate a step on Prometheus metrics.
- [Pause and resume a rollout](../../guides/roll-out-changes/pause-and-resume-a-rollout.md): hold a rollout in progress, and let it go on.
- [Rollout policy](rollout-policy.md): define a progression once, for groups to share. It's alpha, and off by default.
- [Serve a prefill-decode model](../../guides/omenative/serve-a-prefill-decode-model.md): run the engine and the decoder on OMENative.
