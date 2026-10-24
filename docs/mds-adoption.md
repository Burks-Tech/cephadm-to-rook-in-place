# MDS handover: CephFS from cephadm MDS daemons to Rook

[PROD], executed in one 46-minute session (including an 11-minute hold) between two host windows. Both filesystems now run on Rook MDS pods (an active plus a standby-replay each) and no cephadm MDS unit is active. It had to come before converting the hosts that held the only active MDS of one filesystem and the only standby of the other.

Example manifest: [examples/filesystem/cephfilesystem-mds-only.yml](../examples/filesystem/cephfilesystem-mds-only.yml).

## Starting state

Two filesystems, called `fs-a` and `fs-b` here.

- `fs-a`: metadata pool 3x replicated on SSD, default data pool EC 5+2 on HDD, filesystem name differs from the pool names, `max_mds 1`, one active and no standby at the time (earlier conversions had removed its standbys). 14.3 M inodes.
- `fs-b`: both pools replicated (data 2/1), one active and one standby, quota set.

Clients: kernel mounts, ceph-fuse mounts through ceph-csi, three non-Kubernetes kernel clients. Everything mounts by **filesystem name**, so keeping names and ids meant CSI, SMB and external clients needed no change.

## The MDS-only CephFilesystem CR

Rook can run MDS daemons for an existing filesystem without creating or touching any pool. In v1.21.0 [SRC], `doFilesystemCreate` looks the filesystem up first and only then updates; `fs new` is never run for an existing filesystem. With no data pools Rook creates no pool and no filesystem and only starts MDS pods. The CRD still requires both keys to exist:

```yaml
apiVersion: ceph.rook.io/v1
kind: CephFilesystem
metadata:
  name: <fs-name>                  # equals the existing filesystem name
  namespace: rook-ceph
  annotations: {kustomize.toolkit.fluxcd.io/prune: disabled}
spec:
  metadataPool: {}
  dataPools: []
  preserveFilesystemOnDelete: true
  metadataServer:
    activeCount: 1                 # equals max_mds
    activeStandby: true            # true = a warm standby-replay daemon per rank
    resources:
      requests: {cpu: "500m", memory: "2Gi"}
      limits:   {memory: "8Gi"}    # Rook sets mds_cache_memory_limit = 0.5 x limit => 4 GiB
    livenessProbe:
      probe: {initialDelaySeconds: 10, timeoutSeconds: 30, periodSeconds: 30, failureThreshold: 10}
    # NO startupProbe override (see P-6)
    placement:
      nodeAffinity: {requiredDuringSchedulingIgnoredDuringExecution: {nodeSelectorTerms: [{matchExpressions: [{key: ceph-storage-node, operator: In, values: ["true"]}]}]}}
      tolerations: [{key: ceph-storage-node, operator: Exists}]
      podAntiAffinity:
        requiredDuringSchedulingIgnoredDuringExecution:      # active and standby of ONE fs never share a node
          - topologyKey: kubernetes.io/hostname
            labelSelector: {matchLabels: {app: rook-ceph-mds, rook_file_system: <fs-name>}}
        preferredDuringSchedulingIgnoredDuringExecution:     # keep the two filesystems apart
          - weight: 50
            podAffinityTerm:
              topologyKey: kubernetes.io/hostname
              labelSelector: {matchLabels: {app: rook-ceph-mds}}
```

In this form Rook does only: per-daemon `mds_join_fs` and `mds_cache_memory_limit` config rows; `fs set <fs> allow_standby_replay true` (when `activeStandby: true`) and `standby_count_wanted`; `fs subvolumegroup create <fs> csi` (idempotent; it adds an empty `csi` group on a filesystem that lacked one, an expected diff); and cephx entities `mds.<fs>-a` and `-b`. No pool is read or written.

`activeStandby: false` means one Deployment and no standby at all: **Rook cannot create plain standbys**, only standby-replay. We accepted standby-replay (faster failover, a second warm cache) and the cache change from 16 GiB to 4 GiB.

Design choices to copy:

- All sizing lives in the manifest delivered by your GitOps tool from the start. A `kubectl patch` of a GitOps-owned CR is reverted at the next reconcile and rolls the MDS again.
- The `metadataServer` block is **identical** in the MDS-only form and the later fully adopted form (with pools added), so promoting one to the other does not roll the MDS. Verify: MDS Deployment uid and generation equal before and after.
- Budget memory as 2 x limit per filesystem (active plus warm standby-replay) and watch the per-node total: adding two 8 GiB MDS to hosts already at 70 to 80% of about 62 GiB allocatable pushed some to 95%. The soft cross-filesystem anti-affinity helped.
- The liveness probe is loosened because the MDS admin socket can stall for tens of seconds during cap recall on a busy filesystem, and a probe kill is a failover for every client.
- Never delete these CRs: deletion runs `fs fail`. To park an MDS, scale the operator to 0 and the MDS Deployments to 0.

## The failover ordering trap

When a rank fails, the mon first picks the standby-replay daemon following that rank, and only then an available standby, preferring (lowest gid) one whose `join_fscid` matches. cephadm standbys have matching `mds_join_fs` and lower gids than any Rook daemon. When Rook flips `allow_standby_replay true`, the *cephadm* standby becomes the standby-replay and the Rook daemons stay plain standbys. Stopping the cephadm active would then fail over to the cephadm standby, not to Rook.

So:

1. Apply the Rook CR.
2. **Stop the cephadm standby first.** The mon then assigns a Rook daemon as standby-replay.
3. Verify a Rook MDS is `up:standby-replay`.
4. **Only then stop the cephadm active.**

The gid order of the two Rook daemons is not controllable, so automation must poll for the exact `<fs>-a|b up:active` and remember that `fs dump` lists a standby-replay daemon with the rank it follows (two entries for rank 0). A lab harness aborted on exactly this: it took the first of the two rank-0 entries and read `standby-replay` while the handover had already succeeded.

## Sequence as executed

| Step | Action | Result |
|---|---|---|
| M0 | Preconditions and baseline (`fs dump`, pool detail, rule dump, osdmap, filtered MDS config rows, `auth ls` names and caps, per-filesystem `session ls` by client type, mount sweep) | gate all PASS except known script noise |
| M1 | GitOps scaffolding commit with a sentinel ConfigMap proving that deleting the child Kustomization leaves objects in place (Orphan semantics) | PASS |
| M2 | Commit 1: MDS-only CR for `fs-b` | first attempt FAILED ([P-6](pitfalls.md#p-6-mds-only-cr-with-a-startupprobe-override-fails)); fix merged; Ready 11 minutes later |
| M3 | Stop the cephadm *standby* of `fs-b` | no failover |
| M4 | Stop the cephadm *active* of `fs-b` (`systemctl disable --now`) | Rook active in about 6 s |
| M5 | Commit 2: MDS-only CR for `fs-a` | Ready |
| M6 | Stop the cephadm active of `fs-a` | Rook active in about 8 s |
| M7 | Acceptance drills: `ceph mds fail <fs>:0` per filesystem, delete the active pod, operator scale 0 then 1 | roles swap, 3 to 4 s each; operator restart changes nothing |
| M8 | Leave cephadm MDS units disabled, not removed | FINAL gate: hashes equal the baseline, health = known set |

## Measured stalls [PROD]

1 Hz `mds stat` sampling plus client probes (applications doing real reads and writes):

| Event | Stop to Rook active | No active MDS | Client-visible |
|---|---|---|---|
| `fs-b`: cephadm active stopped | about 6.0 s | about 4.8 s | worst op 3.98 s in 389 ops, 0 errors |
| `fs-a` (14.3 M inodes): cephadm active stopped | about 8 s | up to 7 s | slow ops 5.6 s and 5.56 s, 0 errors across about 1150 ops |
| Drill: `ceph mds fail fs-b:0` | about 3.8 s | about 3 s | roles swapped |
| Drill: `ceph mds fail fs-a:0` | about 3 s | about 3 s | roles swapped |
| Drill: delete the active pod | about 3 s | 2 to 3 s | replacement became standby-replay |
| Adding standbys, stopping cephadm standbys, operator restart | no failover | none | none |

Journal replay of a 14 M-inode filesystem was not a long pole because standby-replay keeps the cache warm. The lab (small filesystems) saw 9 to 13 s with client stalls of about 5 s; the plan budgeted 10 to 60 s. These are one run per filesystem at 1 Hz, so the client worst case is probe latency, not a bound.

## Gates after each step

- Filesystem names, fscids, pool ids and names unchanged; `osd pool ls detail` unchanged after masking volatile fields; POOLRULE; PG up/acting identical.
- Per-daemon cache limit equals 0.5 x the limit.
- Operator log has no `upgradeMDS`, `fs new`, `osd pool create` or `pool size is changed`.
- Auth additions are only the four `mds.<fs>-a|b` entities.
- Session counts unchanged (none evicted, including non-Kubernetes clients).
- A mount sweep of every real application mount (read, write, remove, with a timeout).
- CSI nodeplugin pods not restarted (restarting a ceph-csi CephFS nodeplugin kills every fuse mount on its node).

Expected side effects: `MDS_INSUFFICIENT_STANDBY` appears while a filesystem has no standby and clears when the CR lands; new blocklist entries only for the stopped cephadm MDS; `CEPHADM_STRAY_DAEMON` grows (the Rook MDS daemons look like strays to cephadm).

## Rollback

Hold, then decide. Before M3: scale the operator and the Rook MDS Deployments of that filesystem to 0 (never delete the CR). After M3/M4/M6: `systemctl enable --now` the cephadm unit with Rook scaled to 0, valid only while that host is unconverted. After the host converts only the Rook-side rollback exists. Not exercised in production.

## Later

At decommission we removed cephadm's service-level `mds.<fs> mds_join_fs` config rows (Rook uses per-daemon sections `mds.<fs>-a/-b`) and the five cephadm MDS cephx entities.
