# cephadm-to-rook-in-place

A production migration write-up: cephadm to Rook, in place.

This repository documents how we moved a running production Ceph cluster from **cephadm** (Ubuntu hosts, Docker containers) to **Rook** (Talos Linux hosts, Kubernetes), one host at a time, **without moving any data**. The fsid, mon names and addresses, OSD ids, cephx keys, pools and CRUSH map were kept. Every OSD was started by Rook from the same LVM volumes it had under cephadm.

We looked for a public report of anyone doing this and did not find one. Rook does not support it, and the Rook maintainers have said for years that they cannot test or document such a path. So this is not a recommendation. It is a record of what one team did, what broke, and how each step was checked, written so that the next person can judge the risk for their own cluster.

Author: David Burks

## Results

| Item | Value |
|---|---|
| Cluster | 7 bare-metal hosts, 44 OSDs (HDD, SATA/SAS SSD, NVMe, LVM OSDs with DB volumes on shared NVMe), 7 mons, 2 CephFS filesystems, one S3 store, an SMB service |
| Data layout | an erasure-coded 5+2 CephFS data pool on exactly 7 hosts (no spare failure domain), about 243 TiB raw, 97 TiB used |
| Before | Ceph 20.2.4 Tentacle, cephadm on Ubuntu 24.04 |
| After | Ceph 20.2.4 Tentacle, Rook v1.21.0, Talos Linux v1.14.2, Kubernetes v1.36 |
| Hosts converted | 7 of 7, October 2026, one window per host |
| Data movement | none: placement, CRUSH, rule, pool and tree hashes were identical before and after every host's adoption |
| Typical host window | 30 to 45 minutes from stopping cephadm on the host to all of its OSDs up under Rook (the first host took 3.5 hours because of firmware boot problems) |
| CephFS | both filesystems handed to Rook MDS daemons; the measured client-visible stall was 6 to 8 seconds per filesystem |
| S3 | rebuilt as a new Rook object store and copied over in one evening window (Rook cannot serve a realm-less RGW zone) |
| SMB | replaced by a single Samba (sambacc) pod; Rook has no SMB support |
| cephadm | mgr module disabled and its state removed; cluster `HEALTH_OK` |
| Performance afterwards | No regression: every metric we could compare with the (thin) earlier measurements was equal or better, tested from a 25G Kubernetes node and a 2.5G LAN client. Method and findings: [docs/after-the-migration.md](docs/after-the-migration.md#performance-after-the-migration) |

What is not done yet is listed in [Status](#status).

## Status

| Part | State |
|---|---|
| Lab rehearsal (4 VMs, cephadm to Talos and Rook, 8 runs) | done |
| Phase 1: Rook operator in, mons renamed to Rook letters, identity seeded, CephCluster with no storage nodes, cephadm paused | done in production |
| Phase 2: all 7 hosts reinstalled with Talos and adopted by Rook | done in production |
| MDS handover (both filesystems) | done in production |
| RGW rebuild and client cutover | done in production; the legacy zone and its pools are still on disk as a fallback, removal is planned after a 7-day soak |
| SMB replacement | done in production |
| cephadm decommission (module off, config-keys, stale config rows and cephx entities removed) | done in production |
| Post-migration health and performance check, S3 LoadBalancer moved to fast nodes, dynamic CephFS PVC node-key cap fixed | done in production |
| Re-enabling Rook's mon health checks, removing the `osd_crush_update_on_start` guard, turning the balancer and autoscaler back on | not done yet |
| Adopting pools and filesystems as full Rook CRs (beyond MDS-only) | rehearsed in the lab only |
| Moving from the external ceph-csi to Rook's CSI | rehearsed in the lab only |

Each claim in the documents carries an evidence label: **[PROD]** executed on the production cluster with a log, **[LAB]** executed in the lab only, **[SRC]** read in Rook or Ceph source code, **[PLANNED]** written down but not executed.

## Who this is for

People who run a cephadm cluster (or another non-Rook Ceph deployment with `ceph-volume lvm` OSDs) and want it managed by Rook without buying a second set of disks or copying petabytes. You should be comfortable reading Rook and Ceph source when the documentation is silent, and you should expect to build a lab first.

If you can afford a second cluster and a data copy, that is the supported path and you should take it.

## Prerequisites (short version)

The full checklist is in [docs/guide.md section 3](docs/guide.md#3-phase-0-feasibility-and-lab-rehearsal). The hard requirements:

- Every OSD is a `ceph-volume lvm` OSD with complete LV tags (`ceph.cluster_fsid`, `ceph.osd_id`, `ceph.osd_fsid`, `ceph.type`, and `ceph.db_device` where a DB exists). Rook adopts these by tag. Raw-mode OSDs were not tested.
- The replacement OS can activate the existing LVM volume groups at boot (Talos does).
- The Kubernetes node name of each storage host equals its existing CRUSH host bucket name, and no `topology.kubernetes.io/*` labels are set on it.
- Mon IP addresses stay the same throughout.
- All `ceph orch` work is finished before Rook first reconciles (on Ceph 20.2.2 to 20.2.4 Rook clears the orchestrator backend).
- `ceph osd ok-to-stop` is true for one host at a time, and you understand your failure margin while one host is down.
- A lab that copies the odd parts of your production cluster.

## The flow in one picture

```
Phase 0  Lab rehearsal, baselines (hashes), cluster clean-up
Phase 1  Rook operator (no CSI)  ->  freeze cephadm scheduling  ->  rename mons to a..g at the same IPs
         ->  hand-made rook-ceph-mon Secret + mon-endpoints ConfigMap  ->  CephCluster with storage.nodes: []
         ->  ceph orch pause                                  (every daemon is still a cephadm container)
Phase 2  Per host: noout, stop cephadm units, swap only the boot disk, install Talos by WWN,
         cold-boot proof, network/jumbo/ARP gate, add one storage.nodes entry,
         Rook adopts the same OSD ids and the same mon, hash gate, unset noout
Phase 3  Service daemons: MDS via MDS-only CephFilesystem CRs, RGW rebuilt as a new store,
         SMB as a plain Samba pod
Phase 4  Last host, then disable cephadm and remove its state
```

## Documents

| Document | Content |
|---|---|
| [docs/guide.md](docs/guide.md) | The main write-up: invariants, phase 1 in detail, per-phase summaries, gates and rollbacks, references |
| [docs/host-conversion.md](docs/host-conversion.md) | The per-host window (P0 to P8), adoption mechanism, gates, rollbacks, measured timings |
| [docs/mds-adoption.md](docs/mds-adoption.md) | Handing CephFS to Rook MDS daemons with an MDS-only CR, the failover ordering trap, measured stalls |
| [docs/rgw-rebuild.md](docs/rgw-rebuild.md) | Why the realm-less RGW zone was rebuilt rather than adopted, the store as built, the copy window and what went wrong in it |
| [docs/smb-replacement.md](docs/smb-replacement.md) | Replacing the cephadm SMB service with a single sambacc pod |
| [docs/dell-13g-talos-boot.md](docs/dell-13g-talos-boot.md) | Getting Talos to boot cold on Dell R430/R730xd with HBA330 and PERC H730 controllers |
| [docs/flux-freeze.md](docs/flux-freeze.md) | Freezing writers when a GitOps controller keeps undoing your freeze |
| [docs/pitfalls.md](docs/pitfalls.md) | Every problem we hit: symptom, cause, fix, prevention |
| [docs/after-the-migration.md](docs/after-the-migration.md) | Health and performance after the migration, the S3 LoadBalancer fix, dynamic CephFS PVC caps, the dashboard with an external Prometheus |
| [docs/lessons-learned.md](docs/lessons-learned.md) | Operating discipline, what is general versus environment-specific, what we would do differently |
| [examples/](examples/) | Sanitized manifests (CephCluster, seed objects, MDS-only CephFilesystem, CephObjectStore, Talos storage-node patch, Flux Kustomizations), the zero-movement gate script and the RGW copy helpers |

## Updates

This write-up will change as the remaining items are done (legacy RGW zone removal, mon health checks, guards) and as corrections come in. Changes are recorded in [CHANGELOG.md](CHANGELOG.md), and each published state is a tagged release (`v1.0.0` is the first). Cite a tag if you need a stable reference.

## Disclaimer

Unsupported by the Rook and Ceph projects. Tested on one cluster, one Rook version (v1.21.0) and one Ceph release (20.2.4, lab 20.2.3). Rook and Ceph behaviour quoted from source code is for those versions. Your hardware, network and failure margins are different. Every step before the final decommission was reversible host by host, which is the only reason the risk was acceptable to us; read the rollback sections before you start, not after.

## Licence

Documentation under CC BY 4.0, scripts and example manifests under MIT. See [LICENSE.md](LICENSE.md).

## Questions and corrections

Questions, corrections and reports of your own attempt (successful or not) are welcome as issues. See [CONTRIBUTING.md](CONTRIBUTING.md).
