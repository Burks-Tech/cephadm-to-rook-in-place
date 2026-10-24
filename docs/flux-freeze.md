# Freezing writers when a GitOps controller keeps undoing the freeze

Several steps in this migration need some writers stopped: host windows (heavy CephFS writers, to keep recovery short) and above all the RGW copy window, which needed **zero writes** to the old S3 zone between the snapshot and the cutover. With Flux managing the workloads, `kubectl scale` and `kubectl patch` are not a freeze. We learned this the expensive way: the RGW freeze was undone twice and cost about two hours ([rgw-rebuild.md](rgw-rebuild.md)).

## What goes wrong

1. **Flux reverts kubectl-level changes.** kustomize-controller re-applies every Kustomization on its interval (30 minutes here, jittered). Replicas, CronJob `suspend`, CloudNativePG `ScheduledBackup` `suspend` patched with kubectl are put back, and fields Flux does not own may be stripped. A "zero requests for 3 minutes" check taken before that reconcile proves nothing; ours passed for exactly that reason.
2. **A suspended child is un-suspended by its parent.** Child Kustomizations are objects in a parent Kustomization. `flux suspend kustomization <child>` only sets `spec.suspend: true` on the live object; the parent's next reconcile re-applies the child manifest from Git (`suspend: false`), and the child then restores its workloads. This reverted our freeze a second time eight minutes after the "fix", and it had earlier reverted a CLI scale-down of fan controllers during a host window.
3. **Writers nobody listed.** Velero creates `*-kopia-maintain-job-*` Jobs on its own schedule that write into the backup bucket. They are not CronJobs, pausing the Velero `Schedule` does not cover them, and they retry after an outage, so they write as soon as the endpoint comes back.
4. **Writers that cannot be paused.** CloudNativePG continuous WAL archiving keeps writing whatever the `ScheduledBackup` state. It only stops when the endpoint is unreachable, and then it retries and WAL accumulates on the database volume (check free space). Plan a delta copy for these buckets.

## The procedure that worked

Freeze (parents last, so children cannot be re-created underneath you):

```bash
flux suspend kustomization <owner-1> <owner-2> ...     # every Kustomization that owns a writer
flux suspend kustomization apps                        # and every parent of those (here: apps, infrastructure)
flux suspend kustomization infrastructure
flux suspend helmrelease velero -n velero              # helm-controller would restore replicas
kubectl -n velero scale deploy/velero --replicas=0     # stops kopia maintenance job creation
# then the kubectl-level freezes: Velero Schedule paused, CronJobs suspend=true,
# ScheduledBackups suspend=true, writer Deployments/StatefulSets scaled to 0,
# and remove the in-cluster route to the old backend (Service/Endpoints) if clients reach it that way
```

Prove it, twice:

- A zero-write watch on the backend's own request log (3 minutes or more) **after** suspending.
- Take the source snapshot **after** the watch, not before.
- Repeat the watch **immediately before** the irreversible step (stopping the old service). Compare a fresh snapshot of the source with the one you copied from; any difference means a writer got through.
- Recovery when it fails: a delta pass (`rclone sync` for buckets with deletes, `rclone copy` for the rest) after a new watch and snapshot.

Resume in reverse order:

```bash
flux resume kustomization infrastructure
flux resume kustomization apps
flux resume kustomization <owner-1> <owner-2> ...
flux resume helmrelease velero -n velero               # restores Velero's replicas
# then undo the kubectl-level freezes Flux has not already reverted
flux get kustomizations -A | grep -i true              # nothing left suspended
```

A Kustomization with `wait: true` can take a few minutes to become Ready after resume; if a StatefulSet stays at 0 replicas, reconcile its Kustomization. `flux suspend` followed by `flux resume` is also the quickest way to unstick a `wait: true` Kustomization.

## Writer checklist for an S3 freeze (ours)

- Velero: Schedule paused, Deployment at 0, HelmRelease suspended.
- Offsite copy jobs (rclone, restic) suspended.
- Every CloudNativePG `ScheduledBackup` suspended through its owner Kustomization; WAL archiving handled by a delta copy.
- The container registry's CI runners and any app that writes objects.
- Anything that reaches the backend outside Kubernetes (VMs, guest backup forwarders): firewall or stop it.

## Alternative: commit the freeze

For a long or recurring window, commit `suspend: true` / `replicas: 0` to Git instead and revert the commit afterwards. It is slower to apply but cannot be undone by a reconcile. We did this for the fan controllers (a hold commit per host window and a restore commit after it).
