---
title: Configure the controller
description: "Tune the OME controller manager's command-line flags, including reconcile concurrency, leader election and runtime-revision garbage collection."
---

Set the OME controller manager's command-line flags to speed up changes to your InferenceServices, keep its leader stable during API server slowdowns, or apply ConfigMap edits sooner. [Controller manager flags](../../reference/operate-ome/controller-manager-flags.md) lists them all, with their defaults. The chart can also [add environment variables and files to the manager pod](#add-environment-variables-and-files-to-the-manager-pod), such as proxy settings and a private CA bundle.

<div class="prerequisites" markdown>

- OME installed with the `ome-crd` and `ome-resources` charts, as the releases `ome-crd` and `ome` in the namespace `ome`. See [Install OME](../../getting-started/install.md).
- Helm and `kubectl` access to the cluster.
- The values file that you installed `ome-resources` with. If you don't have it, `helm get values ome -n ome` prints the values that you set.

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

Set `kubeAPIQPS` and `kubeAPIBurst` together: with a QPS but no burst, the manager exits when it starts.

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

- the `omeAgent`, `multicluster`, `omenativeStatus` and `eventRecorder` entries;
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
  -e 'Invalid leader election timing|Failed to create Kubernetes client set|invalid --multicluster-role' \
  -e 'flag provided but not defined|invalid (boolean )?value ".*" for (flag )?-' \
  -e 'Failed to initialize [a-zA-Z -]+ configuration'
```

| Message | Cause and fix |
| --- | --- |
| `Invalid leader election timing` | The lease values break a rule in [Lease timing](#lease-timing). The error gives the values and the rule. |
| `Failed to create Kubernetes client set`, with `burst is required to be greater than 0` | `kubeAPIQPS` is set without `kubeAPIBurst`. Set `kubeAPIBurst` above 0. |
| `invalid --multicluster-role` | Set `ome.multicluster.role` to `control-plane`, or leave it empty. |
| `flag provided but not defined`, `invalid value` or `invalid boolean value` | A patched argument is misspelled, isn't in your OME version, or has a bad value, such as `3d` for a duration. |
| `Failed to initialize deployment configuration`, or the same for another entry | That entry of the `inferenceservice-config` ConfigMap is invalid, and the rest of the line says why. For `replicas` in the `deploy` entry, see [Set replica defaults](set-replica-defaults.md). For `Failed to initialize event recorder configuration`, the `eventRecorder` entry breaks a rule in [Tune the per-object event budget](#tune-the-per-object-event-budget). |

Fix the value and upgrade again, as in Step 1. Correct a patched argument with `kubectl edit deployment ome-controller-manager -n ome`, which prints `deployment.apps/ome-controller-manager edited` when you save a change.

## Next steps

- [Controller manager flags](../../reference/operate-ome/controller-manager-flags.md): the manager's flags, with their defaults and Helm values.
- [Set up alerting](alerting.md): turn on the chart's alerts for when the control plane stops reconciling or the admission webhook fails.
- [Collect metrics](metrics.md): tune the Prometheus that the chart deploys, which also scrapes the manager's metrics.
- [Run the model agent](model-agent.md): configure the node agent that downloads models and reads their metadata.
- [Set accelerator quotas](accelerator-quota.md): limit the accelerators that InferenceServices can use.
