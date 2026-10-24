---
title: Autoscaler policy
description: An AutoscalerPolicy is an alpha, reusable set of KEDA or HPA autoscaling settings that InferenceService components share by name.
since: v1.3
---

An AutoscalerPolicy lets the InferenceServices in a namespace share one set of autoscaling settings: [KEDA](https://keda.sh) triggers, or HorizontalPodAutoscaler metrics and behavior. An [InferenceService](inference-services.md) component names the policy in `autoscalerPolicyRef`, and OME builds its autoscaler from the policy, filling in the component's names and replica bounds. Edit the policy, and OME updates every component that uses it. AutoscalerPolicy is alpha and off by default: [Turn on the feature](#turn-on-the-feature) shows how to turn it on.

Use a policy when several InferenceServices should scale the same way. A runtime can also carry autoscaling settings for the InferenceServices that use it, and a policy outranks them.

The examples use the `llama-demo` namespace and the `llama-3-2-1b-instruct` InferenceService from [A minimal InferenceService](inference-services.md#a-minimal-inferenceservice), and assume OME runs in the `ome` namespace.

## Turn on the feature

Set `ome.autoscalerPolicy.enabled: true` in both the `ome-crd` and `ome-resources` charts, version 1.3.0 or later. A `prometheus` trigger also needs a [metric provider](#metric-providers), and the chart binds none by default. This values file turns the feature on, and binds the examples' provider, `cluster-prometheus`, to the bundled Prometheus:

```yaml title="values.yaml"
ome:
  autoscalerPolicy:
    enabled: true
  metricProviders:
    cluster-prometheus:
      serverAddress: "http://ome-prometheus.ome.svc:9090"
```

Add these values to the values file you install both charts with. Upgrade `ome-crd` first, then `ome-resources`, at the chart version you run:

```bash
helm upgrade --install ome-crd oci://ghcr.io/moirai-internal/charts/ome-crd \
  --namespace ome --version 1.3.0 -f values.yaml
helm upgrade --install ome oci://ghcr.io/moirai-internal/charts/ome-resources \
  --namespace ome --version 1.3.0 -f values.yaml
```

Helm reports that each release has been upgraded.

!!! danger "Turning the feature off deletes every policy"
    Keep `enabled: true` in the values file: an `ome-crd` upgrade without it deletes the CRD and every AutoscalerPolicy with it, as [Configure the controller](../../guides/operate-ome/configure-the-controller.md#turn-on-rolloutpolicy-and-autoscalerpolicy) warns.

The `ome-resources` upgrade restarts the controller, which looks for the CRD and for KEDA only when it starts. A `KEDA` policy needs KEDA in the cluster: if you install KEDA later, or install the AutoscalerPolicy CRD without the chart, restart the controller yourself:

```bash
kubectl rollout restart deployment ome-controller-manager -n ome
```

```output
deployment.apps/ome-controller-manager restarted
```

Check that the CRD and the webhook are installed:

```bash
kubectl get crd autoscalerpolicies.ome.io -o name
kubectl get validatingwebhookconfiguration autoscalerpolicy.ome.io -o name
```

```output
customresourcedefinition.apiextensions.k8s.io/autoscalerpolicies.ome.io
validatingwebhookconfiguration.admissionregistration.k8s.io/autoscalerpolicy.ome.io
```

## Define a policy

This policy scales a component on the number of requests waiting in vLLM's queue, which the [bundled Prometheus](../../guides/operate-ome/metrics.md#what-it-scrapes) collects and labels with each pod's InferenceService and component:

```yaml title="queue-depth.yaml"
apiVersion: ome.io/v1beta1
kind: AutoscalerPolicy
metadata:
  name: queue-depth
  namespace: llama-demo
spec:
  class: KEDA
  keda:
    triggers:
      - type: prometheus
        providerRef:
          name: cluster-prometheus
        metricType: AverageValue
        metadata:
          query: 'sum(vllm:num_requests_waiting{namespace="{{ .Namespace }}",inferenceservice="{{ .ISVCName }}",component="{{ .Component }}"})'
          threshold: "5"
          ignoreNullValues: "false"
    fallback:
      failureThreshold: 3
      replicas:
        fromComponent: MaxReplicas
```

Apply the file:

```bash
kubectl apply -f queue-depth.yaml
```

```output
autoscalerpolicy.ome.io/queue-depth created
```

For each component that references it, OME renders this policy into a KEDA ScaledObject:

- The query sums the waiting requests across the component's pods. With `metricType: AverageValue`, KEDA divides the sum by the replica count, so it adds replicas to keep about 5 waiting requests per replica.
- `ignoreNullValues: "false"` makes an empty query result an error, instead of a zero.
- When the query fails more than 3 times in a row, KEDA scales the component to its `maxReplicas`.

The values in a trigger's `metadata` can use these variables, which OME fills in for each component:

| Variable | Value |
| --- | --- |
| `{{ .Namespace }}` | The InferenceService's namespace. |
| `{{ .ISVCName }}` | The InferenceService's name. |
| `{{ .Component }}` | The component: `engine`, `decoder` or `router`. |
| `{{ .MinReplicas }}` | The component's minimum replica count. |
| `{{ .MaxReplicas }}` | The component's maximum replica count. |
| `{{ .TargetName }}` | `<isvc>-<component>`, such as `llama-3-2-1b-instruct-engine`. |

A value holds literal text and these variables only: no template functions, pipelines or conditionals. The other fields:

| Field | What it sets |
| --- | --- |
| `class` | `KEDA`, which needs `keda.triggers`, or `HPA`. |
| `enforcement` | `Default`, which the API sets on every policy: components opt in with a reference, and an inline `autoscaler` outranks the policy, as [Inline settings win](#inline-settings-win) describes. `Required`, the schema's other value, is reserved: see the note below. |
| `providerRef` | The [metric provider](#metric-providers) of a `prometheus` trigger. Required, as is an explicit `ignoreNullValues`. OME sets the trigger's `serverAddress` and `authModes` from the provider, so a trigger can't set them. |
| `queryReturnsDesiredReplicas` | Declares that a trigger's query computes the replica count itself, which needs `metricType: AverageValue`: see [Queries that compute the replica count](#queries-that-compute-the-replica-count). |
| `keda.fallback.replicas` | A fixed `value`, or `fromComponent: MaxReplicas` or `MinReplicas`. |
| `keda.pollingInterval`, `cooldownPeriod`, `idleReplicaCount`, `advanced` | Copied to the ScaledObject as they are. |
| `hpa` | The HorizontalPodAutoscaler's `metrics` and `behavior`, copied as they are. Without it, the HorizontalPodAutoscaler keeps CPU use at 80%. |

OME rejects a policy that breaks these rules when you create or update it, including an invalid PromQL query and the one valid query shape that [Queries that compute the replica count](#queries-that-compute-the-replica-count) describes. [AutoscalerPolicySpec](../../reference/api/ome.v1beta1.md#ome-io-v1beta1-AutoscalerPolicySpec) in the API reference lists every field.

!!! note "Why the schema lists values OME rejects"
    OME rejects `enforcement: Required` with `EnforcementRequiredReserved: enforcement Required is a reserved shape and not implemented; use Default`. The value is reserved for a future enforcement tier, as the `ClusterAutoscalerPolicy` kind in [a component's reference](#attach-a-policy-to-an-inferenceservice) is for a future cluster-scoped policy. Rejecting a reserved value keeps it out of every stored object, so a controller too old to know the feature can never read a `Required` policy as `Default`, or a cluster-scoped reference as a namespaced policy. The `Pinned` mode of an InferenceService's [`scalingPolicy`](inference-services.md#scaling) is in the schema but rejected the same way.

This `HPA` policy keeps CPU use at 60%, and when it scales down, it keeps the highest replica count recommended in the last 5 minutes:

```yaml title="cpu-60.yaml"
apiVersion: ome.io/v1beta1
kind: AutoscalerPolicy
metadata:
  name: cpu-60
  namespace: llama-demo
spec:
  class: HPA
  hpa:
    metrics:
      - type: Resource
        resource:
          name: cpu
          target:
            type: Utilization
            averageUtilization: 60
    behavior:
      scaleDown:
        stabilizationWindowSeconds: 300
```

```bash
kubectl apply -f cpu-60.yaml
```

```output
autoscalerpolicy.ome.io/cpu-60 created
```

## Queries that compute the replica count

In the `queue-depth` policy, the query measures load, and KEDA works out the replica count from the measurement. A query can instead compute the count itself. This policy scales a component to its `maxReplicas` while it has requests running, and back to its `minReplicas` when it has none:

```yaml title="scale-to-max.yaml"
apiVersion: ome.io/v1beta1
kind: AutoscalerPolicy
metadata:
  name: scale-to-max
  namespace: llama-demo
spec:
  class: KEDA
  keda:
    triggers:
      - type: prometheus
        providerRef:
          name: cluster-prometheus
        metricType: AverageValue
        queryReturnsDesiredReplicas: true
        metadata:
          query: '((sum(vllm:num_requests_running{namespace="{{ .Namespace }}",inferenceservice="{{ .ISVCName }}",component="{{ .Component }}"}) > bool 0) * {{ .MaxReplicas }})'
          threshold: "1"
          ignoreNullValues: "false"
    fallback:
      failureThreshold: 3
      replicas:
        fromComponent: MaxReplicas
```

Apply the file:

```bash
kubectl apply -f scale-to-max.yaml
```

```output
autoscalerpolicy.ome.io/scale-to-max created
```

`sum(...) > bool 0` is 1 while any request is running, otherwise 0, and `* {{ .MaxReplicas }}` turns that into the component's `maxReplicas` or 0. With `metricType: AverageValue` and `threshold: "1"`, the HorizontalPodAutoscaler that KEDA creates divides the query's result by the threshold, rounds up, and clamps the count to the component's replica bounds. The query's result becomes the replica count, except that an idle component falls to its `minReplicas`, not to zero. As in the `queue-depth` policy, the fallback scales the component to its `maxReplicas` when the query keeps failing.

A query that computes the count needs `queryReturnsDesiredReplicas: true` and the parentheses around the comparison. The webhook rejects:

- `queryReturnsDesiredReplicas: true` on a trigger whose `metricType` isn't `AverageValue`, with the reason `DesiredCountRequiresAverageValue`. The field declares what the query returns, and changes nothing in the rendered trigger; the declaration matters because the other metric type breaks the count. With `Value`, the HorizontalPodAutoscaler also multiplies by the number of ready pods, which is right for a load metric, but turns a count of N into N times the pod count.
- The query without the parentheses around the comparison, with the reason `PromQLInvalid`. `sum(...) > bool 0 * {{ .MaxReplicas }}` is valid PromQL, but `*` binds tighter than `>`, so it means `sum(...) > bool (0 * {{ .MaxReplicas }})` and returns 0 or 1 instead of 0 or the count. OME checks every `prometheus` trigger's query for this shape, a `bool` comparison with arithmetic on plain numbers on its right side, with or without `queryReturnsDesiredReplicas`. The rejection message quotes the comparison, rendered with sample values in place of the variables, and shows the shape of the fix: `(sum(...) > bool N) * M`.

## Attach a policy to an InferenceService

Name the policy in a component's `autoscalerPolicyRef`. This version of `isvc.yaml` attaches `queue-depth` to the engine, and sets the engine's replica bounds:

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
    autoscalerPolicyRef:
      name: queue-depth
```

Apply the file:

```bash
kubectl apply -f isvc.yaml
```

```output
Warning: Runtime vllm-llama-3-2-1b-instruct will be auto-selected for model llama-3-2-1b-instruct
inferenceservice.ome.io/llama-3-2-1b-instruct configured
```

The engine still has OME's default HorizontalPodAutoscaler. OME creates the ScaledObject `scaledobject-llama-3-2-1b-instruct-engine` and hands that HorizontalPodAutoscaler over to KEDA, which keeps scaling the engine through it, as [Switch between HPA and KEDA](component-autoscaling.md#switch-between-hpa-and-keda) describes. Read the rendered server address, query and fallback replica count:

```bash
kubectl get scaledobject scaledobject-llama-3-2-1b-instruct-engine -n llama-demo \
  -o jsonpath='{.spec.triggers[0].metadata.serverAddress}{"\n"}{.spec.triggers[0].metadata.query}{"\n"}{.spec.fallback.replicas}{"\n"}'
```

```output
http://ome-prometheus.ome.svc:9090
sum(vllm:num_requests_waiting{namespace="llama-demo",inferenceservice="llama-3-2-1b-instruct",component="engine"})
4
```

The InferenceService reports where the engine's autoscaler came from:

```bash
kubectl get inferenceservice llama-3-2-1b-instruct -n llama-demo \
  -o custom-columns='SOURCE:.status.components.engine.autoscaler.specSource,POLICY:.status.components.engine.autoscaler.policy.name,RESOLVED:.status.components.engine.autoscaler.conditions[?(@.type=="AutoscalerResolved")].reason'
```

```output
SOURCE   POLICY        RESOLVED
policy   queue-depth   RenderedFromPolicy
```

The `engine`, `decoder` and `router` can each name a policy, the same one or different ones. The webhooks reject:

- any reference until the controller has found the CRD at startup, with a message that starts with `AutoscalerPolicyFeatureDisabled`;
- `kind: ClusterAutoscalerPolicy` in a reference, with `AutoscalerPolicyRefKindReserved: spec.engine.autoscalerPolicyRef.kind "ClusterAutoscalerPolicy" is not supported (ClusterAutoscalerPolicy is a reserved shape); use AutoscalerPolicy`. The schema lists the kind, but it's reserved until a cluster-scoped policy exists, so a reference needs only `name`: `kind: AutoscalerPolicy` is the default and the only kind there is;
- `autoscalerPolicyRef` in a runtime, because references go only in an InferenceService;
- a reference on a component that a `Proportional` [`scalingPolicy`](component-autoscaling.md#proportional-scaling) names as a follower, with `decoder follower cannot reference AutoscalerPolicy "queue-depth" when scaling is Proportional: remove the reference or remove decoder from ratios`, because a follower's count comes from its anchor;
- `minReplicas: 0` with only a reference: [scale to zero](../../guides/scale-and-migrate/scale-to-zero-with-keda.md) needs the component's own `autoscaler` with `class: KEDA`.

## Inline settings win

A component's inline `autoscaler` outranks its `autoscalerPolicyRef`, as [Which setting wins](component-autoscaling.md#which-setting-wins) lists. Set an inline `autoscaler` beside the reference to try other settings, to roll back, or to end a [hold](#fail-closed-behavior), without removing the reference. This engine keeps its reference but scales on CPU use:

```yaml
spec:
  engine:
    minReplicas: 1
    maxReplicas: 4
    autoscalerPolicyRef:
      name: queue-depth
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

OME replaces the ScaledObject with a HorizontalPodAutoscaler, and reports the reference as shadowed:

```bash
kubectl get inferenceservice llama-3-2-1b-instruct -n llama-demo \
  -o custom-columns='SOURCE:.status.components.engine.autoscaler.specSource,SHADOWED:.status.components.engine.autoscaler.shadowedPolicyRef.name,RESOLVED:.status.components.engine.autoscaler.conditions[?(@.type=="AutoscalerResolved")].reason'
```

```output
SOURCE   SHADOWED      RESOLVED
isvc     queue-depth   InlinePrecedence
```

OME still renders the shadowed policy on every reconcile, and reports the render in `shadowedPolicyRef`, so you can preview what the policy would dispatch while the inline settings keep running:

```bash
kubectl get inferenceservice llama-3-2-1b-instruct -n llama-demo \
  -o custom-columns='SHADOWED:.status.components.engine.autoscaler.shadowedPolicyRef.name,PORTABLE:.status.components.engine.autoscaler.shadowedPolicyRef.portableDigest,WOULD-RENDER:.status.components.engine.autoscaler.shadowedPolicyRef.wouldRenderDigest'
```

```output
SHADOWED      PORTABLE           WOULD-RENDER
queue-depth   pv1:9f2c41d07a8e   rv1:5b82d4c91f0a
```

`portableDigest` identifies the policy's spec, and `wouldRenderDigest` identifies the render the policy would produce for this component if you removed the inline block: [Conditions and status](#conditions-and-status) describes both. Edit the shadowed policy, and the digests follow it. When the shadowed policy fails to render — it's missing, invalid or names an unbound provider — `shadowedPolicyRef` carries only the name, with no digests, and the component keeps running on the inline settings: an inline `autoscaler` wins even over a broken policy.

To go back to the policy, remove the `autoscaler` block: OME hands its HorizontalPodAutoscaler over to KEDA again. The policy renders from the same inputs, so the component's `policy.resolvedDigest` equals the `wouldRenderDigest` you previewed, unless the policy, the component's replica bounds or the provider binding changed in between. [Confirm that a policy edit landed](#confirm-that-a-policy-edit-landed) shows how to read it.

## Fail-closed behavior

When OME can't use a component's policy, the component holds: it keeps its last autoscaler, and never falls back to the runtime's settings or the default. OME holds in these cases:

| Reason | When |
| --- | --- |
| `PolicyNotFound` | The policy is missing from the InferenceService's namespace, or the controller started before the AutoscalerPolicy CRD was installed. |
| `PolicyInvalid` | The policy fails to render for the component, for example because it names an unbound provider, or the reference carries the reserved `kind: ClusterAutoscalerPolicy`, which admission normally rejects. |
| `ClassUnavailable` | The policy's class is `KEDA`, and KEDA's CRDs were missing when the controller started. |

While a component holds, OME keeps creating and updating its workload, and only the autoscaler is frozen. The component keeps the autoscaler that its policy last rendered; on a RawDeployment component, changes to `minReplicas` and `maxReplicas` still reach it. If the policy never rendered for the component, OME leaves its current autoscaler, if any, as it is.

The component's `AutoscalerResolved` condition is `False` with the hold's reason, and its message starts with `holding last-known-good scaler:`. When a hold starts, OME also records an `AutoscalerPolicyHold` Warning event on the InferenceService, such as `engine autoscaler is holding last-known-good (PolicyNotFound): AutoscalerPolicy "queue-depth" not found in namespace llama-demo`.

A held component recovers once you create or fix the policy, or bind its provider. If the CRD or KEDA was missing when the controller started, restart the controller after you install it.

## Metric providers

A `prometheus` trigger names a metric provider in `providerRef` instead of a Prometheus address, so the same policy works in any cluster that binds the name. The cluster administrator binds provider names in the `ome-resources` value `ome.metricProviders`, as [Turn on the feature](#turn-on-the-feature) does, and canary analysis uses the same [bindings](../../reference/rollouts/canary-analysis.md#metric-provider-bindings).

A binding's optional `authSecretRef` names the Secret `name` and `key` that hold a bearer token for the provider. This version of `values.yaml` adds one to `cluster-prometheus`:

```yaml title="values.yaml"
ome:
  autoscalerPolicy:
    enabled: true
  metricProviders:
    cluster-prometheus:
      serverAddress: "http://ome-prometheus.ome.svc:9090"
      authSecretRef:
        name: prometheus-bearer
        key: token
```

Create the Secret in each namespace that uses the provider. OME renders the provider's triggers with `authModes: bearer` in their metadata and an `authenticationRef` named `ome-metric-provider-<provider>`, so the triggers for `cluster-prometheus` point at `ome-metric-provider-cluster-prometheus`. Before it creates or updates a component's ScaledObject, OME creates a KEDA [TriggerAuthentication](https://keda.sh/docs/latest/concepts/authentication/) with that name in the InferenceService's namespace. The object points KEDA at the binding's Secret:

```bash
kubectl get triggerauthentication ome-metric-provider-cluster-prometheus -n llama-demo \
  -o jsonpath='{.spec.secretTargetRef[0].parameter}{"\n"}{.spec.secretTargetRef[0].name}{"\n"}{.spec.secretTargetRef[0].key}{"\n"}'
```

```output
bearerToken
prometheus-bearer
token
```

OME never reads the Secret: KEDA resolves it through the TriggerAuthentication when it queries the provider, so the token never passes through OME, and never appears in the policy, the rendered trigger or the InferenceService's status. Don't edit or delete the object: OME restores it the next time it reconciles a component whose policy uses the provider. OME never deletes one, so the object stays in the namespace after the components that used the provider are gone.

Autoscaling triggers don't send the binding's `headers`. OME picks up a change to the bindings on its own.

!!! warning "Every binding needs a serverAddress"
    While the feature is on, one binding without `serverAddress` stops OME from creating or updating the workload of any InferenceService, including the ones that don't use the provider. [InferenceServices stop updating](../../guides/operate-ome/metrics.md#inferenceservices-stop-updating) shows how to find the binding.

## Conditions and status

A policy's status says whether it's valid, how many components use it, and the digest that identifies its spec:

| Field | Description |
| --- | --- |
| `conditions`, type `Ready` | `True` with the reason `TemplatesValid` when the policy is valid and its providers are bound. Otherwise `False`, with the reason `ProviderUnknown` or that of the first validation problem. |
| `conditions`, type `InUse` | `True` with the reason `Attached` when a component references the policy, otherwise `False` with the reason `NoConsumers`. |
| `attachedComponents` | How many components reference the policy. An engine and a decoder count as two. |
| `observedGeneration` | The `metadata.generation` the status reflects. |
| `portableDigest` | A digest of the spec with the API's defaults applied: `pv1:` and 12 hex digits. Two policies with the same settings get the same digest, in this cluster or another one, whether or not their files spell out the defaulted fields. |

List the policies with their `Ready` reason:

```bash
kubectl get autoscalerpolicies -n llama-demo \
  -o custom-columns='NAME:.metadata.name,CLASS:.spec.class,READY:.status.conditions[?(@.type=="Ready")].status,REASON:.status.conditions[?(@.type=="Ready")].reason,ATTACHED:.status.attachedComponents'
```

```output
NAME          CLASS   READY   REASON           ATTACHED
cpu-60        HPA     True    TemplatesValid   <none>
queue-depth   KEDA    True    TemplatesValid   1
```

ATTACHED is `<none>` while the policy is unused. [`kubectl ome get autoscalerpolicies`](../../reference/kubectl-ome/get.md#autoscalerpolicies-columns) shows the same status, with the digest in its wide output.

On the InferenceService, `status.components.<component>.autoscaler.policy` records the render that the component's live autoscaler came from, and `shadowedPolicyRef` describes a reference that an inline `autoscaler` outranks:

| Field | Description |
| --- | --- |
| `policy.name` | The policy that rendered the component's autoscaler. |
| `policy.observedGeneration` | The policy's `metadata.generation` at the render. |
| `policy.portableDigest` | The policy's `portableDigest` at the render. |
| `policy.resolvedDigest` | A digest of the render itself — the rendered autoscaler, the component's effective replica bounds and the provider's server address: `rv1:` and 12 hex digits. Unlike `portableDigest`, it isn't portable: the same policy can digest differently per component and per cluster. |
| `shadowedPolicyRef.name` | The policy that an inline `autoscaler` outranks. |
| `shadowedPolicyRef.portableDigest`, `.wouldRenderDigest` | The shadowed policy's spec digest, and the `resolvedDigest` its render would get if you removed the inline block. Both are empty when the shadowed policy fails to render. See [Inline settings win](#inline-settings-win). |

While a component [holds](#fail-closed-behavior), `policy` keeps the generation and digests of the last render that succeeded — the render the frozen autoscaler came from — so you can see which settings are standing. [Read the result in status](component-autoscaling.md#read-the-result-in-status) describes the `autoscaler` block's other fields.

OME sets `AutoscalerResolved` only on components with a reference:

| Status | Reason | When |
| --- | --- | --- |
| `True` | `RenderedFromPolicy` | The policy rendered the component's autoscaler. |
| `True` | `InlinePrecedence` | An inline `autoscaler` outranks the reference. |
| `False` | `PolicyNotFound`, `PolicyInvalid` or `ClassUnavailable` | The component holds; see [Fail-closed behavior](#fail-closed-behavior). |
| `False` | `UnsupportedDeploymentMode` | The component is [`MultiNode`](../architecture/deployment-modes.md) (deprecated), which gets no autoscaler. |

### Confirm that a policy edit landed

A policy edit reaches a component's autoscaler through a render, and the digests prove the render happened. Edit `queue-depth` — raise the trigger's `threshold` from `"5"` to `"8"`, say — and apply it. The policy's status picks up the new spec:

```bash
kubectl get autoscalerpolicy queue-depth -n llama-demo \
  -o custom-columns='GENERATION:.metadata.generation,OBSERVED:.status.observedGeneration,DIGEST:.status.portableDigest'
```

```output
GENERATION   OBSERVED   DIGEST
2            2          pv1:7d41c0b3a95e
```

OME re-renders every component that references the policy. Compare the engine's provenance with the policy:

```bash
kubectl get inferenceservice llama-3-2-1b-instruct -n llama-demo \
  -o custom-columns='POLICY:.status.components.engine.autoscaler.policy.name,GENERATION:.status.components.engine.autoscaler.policy.observedGeneration,PORTABLE:.status.components.engine.autoscaler.policy.portableDigest,RESOLVED:.status.components.engine.autoscaler.policy.resolvedDigest'
```

```output
POLICY        GENERATION   PORTABLE           RESOLVED
queue-depth   2            pv1:7d41c0b3a95e   rv1:3e95b1c84d06
```

The engine's `observedGeneration` and `portableDigest` match the policy's generation and digest: its live ScaledObject was rendered from the spec you just applied. The same check works across clusters: two clusters running the same policy spec report the same `portableDigest`, so you can compare a component's provenance in one cluster against the policy in another without reading the rendered objects. [Multi-cluster placement](#multi-cluster-placement) builds on that equality: before OME places a service on a cluster, the cluster's copy of the policy must digest the same as the control plane's.

When the digests don't match and the component's `AutoscalerResolved` condition is `False`, the component is [holding](#fail-closed-behavior): the provenance shows the render that's still standing, not the spec you applied. Fix what the condition's message names, and the digests catch up on the next render.

`resolvedDigest` tells renders apart rather than specs: it also changes when you change the component's `minReplicas` or `maxReplicas`, or the cluster's [provider binding](#metric-providers), with no policy edit at all. Use `portableDigest` to see that an edit landed, and `resolvedDigest` to see that a component's render changed — or, after you remove an inline block, that it matches the `wouldRenderDigest` previewed in [Inline settings win](#inline-settings-win).

## Multi-cluster placement

On a multi-cluster control plane, an InferenceService with a [`spec.placement`](../../reference/api/ome.v1beta1.md#ome-io-v1beta1-PlacementSpec) block is a source: OME places it by writing a derived InferenceService to each workload cluster it chooses, as [Rollouts and traffic](inference-services.md#rollouts-and-traffic) outlines. Multi-cluster placement is alpha and still in development. The examples here place `llama-3-2-1b-instruct` on two workload clusters, `worker-a` and `worker-b`, with `queue-depth` attached to its engine as in [Attach a policy to an InferenceService](#attach-a-policy-to-an-inferenceservice).

The derived InferenceServices carry the components' `autoscalerPolicyRef`, but OME never copies the policy itself: each member cluster renders the autoscaler from its own copy, with its own [provider bindings](#metric-providers), exactly as this page describes for one cluster. A member cluster that placement may choose needs:

- The feature [turned on](#turn-on-the-feature) there, declared by the label `ome.io/autoscaler-policy: v1beta1` on its [WorkloadCluster](../../reference/api/labels-and-annotations.md#workloadcluster-labels).
- Its own copy of each policy the source references — the same name in the same namespace — whose `portableDigest` equals the control plane's, which it does exactly when the specs match, as [Confirm that a policy edit landed](#confirm-that-a-policy-edit-landed) describes.

Distribute the policies to the members yourself, with GitOps for example: placement checks the copies, but nothing in OME creates them. The control plane cluster needs its own copy too, in the source's namespace. That copy is the anchor every member is compared against, and placement holds while it's missing or invalid.

### The placement preflight

Before OME writes the derived InferenceServices, the placement controller checks every candidate cluster, and records the verdict in the source's `PlacementPolicyPreflight` condition. A candidate whose WorkloadCluster lacks the capability label is skipped. On a labeled candidate, OME reads each referenced policy live from the member's API server — each read gets the [member-read deadline](#preflight-settings), 5 seconds by default — and skips the candidate when a policy is missing there, or digests differently from the anchor.

| Status | Reason | When |
| --- | --- | --- |
| `True` | `Passed` | At least one candidate is eligible. The message counts them and names each skipped cluster with its reason, such as `1 of 2 candidates eligible; skipped: worker-b (CapabilityMissing: cluster does not carry ome.io/autoscaler-policy=v1beta1)`. |
| `False` | `CapabilityMissing`, `PolicyMissingOnMember`, `DigestMismatch` or `MemberGetTimeout` | No eligible candidate remains, or the cluster serving a `Single`-mode source became ineligible, with the skip's reason. Across several skips, the most serious reason wins: `DigestMismatch`, then `PolicyMissingOnMember`, `MemberGetTimeout` and `CapabilityMissing`. A missing or unreadable anchor copy also reads `PolicyMissingOnMember`, with a message that names the control plane. |
| `False` | `InvalidRef` | A reference on the source has a shape every member's webhook would deny, such as the reserved `kind: ClusterAutoscalerPolicy`. |
| `False` | `InvalidPolicy` | The anchor copy is invalid, so every member would reject the autoscaler rendered from it. The message lists the problems. |

`False` blocks placement: OME writes no new derived InferenceServices, and the clusters already serving the source keep serving — the source holds where it is rather than move onto an unverified cluster. Skipping alone doesn't block: while a candidate remains eligible, the condition stays `Passed`, and placement avoids the skipped clusters. A timed-out or failed read is transient — the reason `MemberGetTimeout` covers both — so a cluster that the last successful check verified stays eligible while OME retries. A missing policy or a digest mismatch is a verdict, and makes the cluster ineligible at once.

!!! warning "Cap Split placements"
    With a policy reference and `spec.placement.mode: Split` or `SplitByCapacity`, set `spec.placement.split.maxReplicasPerCluster`. The policy renders against each home's own derived replica bounds, and without the per-cluster cap that ceiling is unbounded, so a [fallback](#define-a-policy) of `fromComponent: MaxReplicas` on one home can scale it to the whole fleet's count. The webhook warns on apply — the warning ends with `(UnboundedSplitCeiling)` — but never rejects, and placement runs. The API lists `UnboundedSplitCeiling` among the preflight condition's reasons, but OME doesn't write it.

### Track the homes after fan-out

Once placed, each home — a cluster serving the source, as the condition messages put it — reports its render in the derived InferenceService's own status, with the provenance and conditions of [Conditions and status](#conditions-and-status), and a member whose copy breaks after placement [holds fail-closed](#fail-closed-behavior) on its own cluster. The control plane lifts that state onto the source, so you read the whole fleet from one object:

- `status.placement.candidates[].autoscaling` mirrors each home: `policies` pairs each policy's name with the `portableDigest` the home observed, `components` maps each referencing component to its `resolvedDigest` and `ready`, and `ready` says whether every referencing component rendered from the policy.
- The `AutoscalerPolicyReady` condition sums the homes up:

| Status | Reason | When |
| --- | --- | --- |
| `True` | `AllHomesResolved` | Every home reports resolved digests, no home carries the service yet, or the rest are within the skew deadline. The message says which. |
| `False` | `ResolveTimeout` | A home has carried the placed service longer than the [skew deadline](#preflight-settings), 15 minutes by default, without reporting a resolved digest. |
| `False` | `FieldPruned` | Three re-applies in a row saw a member's API server drop the applied `autoscalerPolicyRef`: its InferenceService CRD is too old for the field, so the member would scale without the policy, silently. |
| `False` | `MemberFailedClosed` | A home reports `AutoscalerResolved: False` for a referencing component: that member is [holding its last autoscaler](#fail-closed-behavior). |

When more than one applies, `FieldPruned` outranks `MemberFailedClosed`, which outranks `ResolveTimeout`. A home where an inline `autoscaler` [outranks the reference](#inline-settings-win) doesn't count as resolved, but doesn't trip `ResolveTimeout` either: its `wouldRenderDigest` proves the member resolved the policy. The skew clock lives in the controller's memory, so a controller restart starts it over, and `ResolveTimeout` fires late rather than falsely. Removing the last reference from the source removes both conditions.

Read the two conditions together:

```bash
kubectl get inferenceservice llama-3-2-1b-instruct -n llama-demo \
  -o custom-columns='PREFLIGHT:.status.conditions[?(@.type=="PlacementPolicyPreflight")].reason,POLICIES:.status.conditions[?(@.type=="AutoscalerPolicyReady")].reason'
```

```output
PREFLIGHT   POLICIES
Passed      AllHomesResolved
```

And each home's observed digest, to find the cluster that holds a policy edit back:

```bash
kubectl get inferenceservice llama-3-2-1b-instruct -n llama-demo \
  -o jsonpath='{range .status.placement.candidates[*]}{.cluster}{"\t"}{.autoscaling.ready}{"\t"}{.autoscaling.policies[0].portableDigest}{"\n"}{end}'
```

```output
worker-a	true	pv1:7d41c0b3a95e
worker-b	true	pv1:7d41c0b3a95e
```

A home that shows an older `portableDigest` than the policy's own status hasn't received the edited copy yet: update the policy there, and compare the digests again as in [Confirm that a policy edit landed](#confirm-that-a-policy-edit-landed).

### Preflight settings

The preflight's two settings are `ome-resources` chart values under `ome.autoscalerPolicy.preflight`. The chart ships both as `0`, which means the controller's built-in default:

| Value | Default | What it sets |
| --- | --- | --- |
| `memberGetTimeoutSeconds` | 5 | The deadline for each live policy read on a member, so one hung member API server can't stall placement. A read past it skips the cluster with `MemberGetTimeout`. |
| `skewDeadlineSeconds` | 900 | How long a home may carry the placed service without reporting a resolved digest before `AutoscalerPolicyReady` turns `False` with `ResolveTimeout`. |

Change them with a chart upgrade, which restarts the controller. The controller reads them only when it starts, so an edit of [the ConfigMap](../../guides/operate-ome/configure-the-controller.md#tune-the-config-cache) itself needs a restart too.

## Delete a policy

The webhook refuses to delete a policy that a component references. With the engine using `queue-depth`, as in [Attach a policy to an InferenceService](#attach-a-policy-to-an-inferenceservice):

```bash
kubectl delete autoscalerpolicy queue-depth -n llama-demo
```

```output
Error from server (Forbidden): admission webhook "autoscalerpolicy.ome-webhook-server.validator" denied the request: AutoscalerPolicy "queue-depth" is referenced by 1 component(s): llama-3-2-1b-instruct/engine; remove the refs first, or set the ome.io/allow-in-use-delete="true" annotation to force deletion
```

Remove the references first. A component whose reference you remove takes its settings from the next source in [Which setting wins](component-autoscaling.md#which-setting-wins).

To delete a policy that components still reference, annotate it first:

```bash
kubectl annotate autoscalerpolicy queue-depth -n llama-demo ome.io/allow-in-use-delete=true
kubectl delete autoscalerpolicy queue-depth -n llama-demo
```

```output
autoscalerpolicy.ome.io/queue-depth annotated
autoscalerpolicy.ome.io "queue-depth" deleted from llama-demo namespace
```

The components that referenced it then hold with the reason `PolicyNotFound`, and keep their last autoscaler:

```bash
kubectl get inferenceservice llama-3-2-1b-instruct -n llama-demo \
  -o custom-columns='SOURCE:.status.components.engine.autoscaler.specSource,POLICY:.status.components.engine.autoscaler.policy.name,RESOLVED:.status.components.engine.autoscaler.conditions[?(@.type=="AutoscalerResolved")].reason'
```

```output
SOURCE   POLICY        RESOLVED
policy   queue-depth   PolicyNotFound
```

## Next steps

- [Component autoscaling](component-autoscaling.md): the `autoscaler` block that a policy renders into, and which setting wins.
- [InferenceService](inference-services.md): the components that reference a policy.
- [kubectl ome autoscale](../../reference/kubectl-ome/autoscale.md): show the autoscaling in effect for each component, and which source supplied it.
