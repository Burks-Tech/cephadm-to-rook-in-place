# After the migration: health, performance and follow-ups

What we checked and fixed in the first day after the last host and the cephadm decommission. [PROD] unless marked otherwise.

## Health the morning after

- `HEALTH_OK` (only the `AUTH_INSECURE_*` checks are muted, for older `aes` keys pending the cipher migration). 7 mons in quorum, an active and a standby Rook mgr, 2 active MDS plus 2 standby-replay, 3 Rook RGW daemons, 44/44 OSDs up and in, 1561 PGs active+clean in 25 pools.
- OSD commit latency 0 to 2 ms at idle. HDD utilisation 35 to 43 % (VAR 0.88 to 1.08), hosts within 2 percentage points of each other: the adoption did not unbalance anything.
- Slow-op history: 9 historic slow ops, each about 10 s waiting on sub-ops or peering, all inside the host-conversion windows. None since.
- Crash reports: only the old cephadm mgr's port-clash crashes from the last day ([P-20](pitfalls.md#p-20-mgr-dashboard-port-clashes)); archived.
- Daemon restarts: the first converted host's OSDs and mon showed 2 restarts each from its conversion day; everything else 0.
- GitOps: every Kustomization and HelmRelease Ready, nothing left suspended.

## Performance after the migration

All test objects (namespace, PVCs, PVs, subvolume, RBD image, RGW user and bucket) were removed afterwards. No pools were created and `rados bench` was not run. The RBD and CephFS tests ran from a Kubernetes node with a 25G NIC, so they measure Ceph rather than a 1G client link. fio: libaio, `direct=1`, `numjobs=1`, 60 s per test. A monitor sampled cluster IO, the worst OSD commit latency and slow ops every 10 s, with an automatic kill on slow ops; it never fired.

### RBD (replicated 3x on SSD, 8 OSDs; 20 GiB PVC, 8 GiB file)

| Test | Bandwidth | IOPS | avg lat | p99 | p99.9 |
|---|---|---|---|---|---|
| seq write 1M QD16 | 517 MB/s | 492 | 32 ms | 61 ms | 72 ms |
| seq read 1M QD16 | 1167 MB/s | 1113 | 14 ms | 37 ms | 49 ms |
| rand write 4k QD32 | 56 MB/s | 13.6k | 2.3 ms | 4.3 ms | 11.5 ms |
| rand read 4k QD32 | 136 MB/s | 33.3k | 0.95 ms | 2.0 ms | 5.3 ms |

`rbd bench` from the toolbox: 448 MiB/s write and 1.9 GiB/s read (4M, 16 threads); 4K random 49.2k write IOPS (inflated by the librbd writeback cache; trust fio) and 51.2k read IOPS.

### CephFS (data pool EC 5+2 on HDD; 4 GiB file, kernel client)

| Test | Bandwidth | IOPS | avg lat | p99 | p99.9 |
|---|---|---|---|---|---|
| seq write 1M QD16 | 267 MB/s | 254 | 62 ms | 139 ms | 1216 ms |
| seq read 1M QD16 | 708 MB/s | 675 | 23 ms | 100 ms | 165 ms |
| rand write 4k QD32 | 5 MB/s | 1107 | 29 ms | 165 ms | 246 ms |
| rand read 4k QD32 | 50 MB/s | 12.3k | 2.6 ms | 26 ms | 62 ms |

The 4K random read figure is optimistic: the 4 GiB hot set was mostly in OSD cache.

### RGW

| Test | Result |
|---|---|
| In-cluster, warp mixed, 16 MiB objects, 16 concurrent, 2 min | 706 MiB/s total (GET 530, PUT 177 MiB/s). HDD OSD commit latency rose to 80 to 183 ms under PUT load; no slow ops. |
| In-cluster, warp mixed, 64 KiB objects, 32 concurrent, 1 min | 3733 objects/s (GET 1680, PUT 560, STAT 1120, DELETE 373) |
| LAN workstation (2.5G) over HTTPS, 1 GiB as 16 x 64 MiB, 8 concurrent | PUT 111 MB/s, GET 112 MB/s: a hard 1 Gbit cap (see below) |

### Compared with before

We had no earlier RBD or RGW baseline. For CephFS the earlier numbers came from a workstation client during the previous Ceph upgrade, so the comparison is rough, but every comparable metric was equal or better (sequential write 103 to 267 MB/s, sequential read 359 to 708 MB/s, 4K random write 840 to 1107 IOPS). Nothing points to a regression from moving to Rook. **If you do this, take your own baseline before you start.** We wish we had.

### What the tests found

1. **Most clients are on 1G links**, so pods on those nodes cap at about 110 MB/s to Ceph whatever the cluster can do.
2. **The S3 LoadBalancer IP was announced from a 1G node.** MetalLB in L2 mode picks one speaker to answer ARP for the IP, and with the default advertisement that was a 1G control-plane node (it also moved 12 times in 16 hours). All LAN S3 traffic took one 1G hop. Fixed below.
3. **Dynamic CephFS PVCs bound but could not mount.** Fixed below.

## S3 LoadBalancer: announce only from fast nodes, on the right interface

Change, merged [PROD]:

- A dedicated `L2Advertisement` for the RGW address pool with `nodeSelectors` limited to nodes with a 10G or 25G link on the Kubernetes VLAN: the storage hosts (where the RGW pods run anyway) and one fast control-plane node. Every other address pool keeps the default advertisement, which now excludes the storage nodes.
- The MetalLB speaker DaemonSet tolerates the storage taint so it can run on the storage hosts at all.
- **`interfaces:` pins the ARP responder to the Kubernetes VLAN interface** of each node (one advertisement per interface name, because the VLAN interface name differs by NIC model: Talos names the tagged sub-interface `<parent>.<vlan>`). The storage hosts are multi-homed; without the pin a speaker could answer ARP for the LB IP on the Ceph network, which is the same failure class as the ARP flux in [P-5](pitfalls.md#p-5-arp-flux-black-holes-routed-jumbo-traffic).
- `externalTrafficPolicy` stays `Cluster`. The SMB Service was left alone: it is `Local` with a single pod on a fast node.

Expect one brief interruption when the announcing node changes on merge.

## Dynamic CephFS PVCs need a metadata-pool cap on the node key

Symptom: a PVC on the dynamic CephFS StorageClass binds, but the pod hangs in ContainerCreating with `rados: ret=-1, Operation not permitted` at NodeStage (ceph-csi v3.18.1).

Cause: we had split ceph-csi's CephFS key into a provisioner key and a node key, and the node key had no OSD cap on the metadata pool. That is enough for **static** PVs (the MDS mediates all metadata), but for **dynamic** PVs ceph-csi reads the volume journal (omap in the metadata pool, namespace `csi`) at NodeStage. Every CephFS PVC we had in use was static, which is why nothing broke during the migration and why our "verify dynamic provisioning" step, which only checked that the PVC was Bound, never caught it.

Diagnosis with throwaway users (one fresh user, Secret and StorageClass copy per trial; the live key untouched):

| mon | osd metadata clause | result |
|---|---|---|
| `allow r` + upstream `*=*` osd profile | `*=*` | mounts |
| `allow r` | none | fails, `Operation not permitted` |
| `allow r fsname=<fs>` | `allow rw tag cephfs metadata=<fs>` | mounts |
| `allow r fsname=<fs>` | `allow r tag cephfs metadata=<fs>` | mounts |
| `allow r` | `allow r tag cephfs metadata=<fs>` | mounts |

So **read-only on the metadata pool is enough**, and an fsname-scoped `mon` cap is fine.

Two gotchas that made the first attempt look like a failure:

- **ceph-csi caches its RADOS connection per user.** Changing a key's caps does not take effect while a pod keeps retrying NodeStage, because each retry refreshes the cached connection. Do not restart the CephFS nodeplugin to flush it: it hosts the ceph-fuse mounts, which die with the pod. Use a fresh key, or apply the caps, delete the stuck pods and PVCs so the retries stop, and test again only after the connection has idled out (treat 15 minutes as the floor).
- **Clean-up of a test subvolume needs `--group_name csi`:** `ceph fs subvolume rm <fs> <subvol> --group_name csi`. The positional group form can report ENOENT for a subvolume that exists.

The change [PROD], applied to the live node key in a quiet window: `ceph auth caps` replaces all caps, so resubmit the existing ones plus `allow r tag cephfs metadata=<fs>` on the `osd` cap. After a 15-minute idle (the caching gotcha above), a fresh dynamic PVC mounted and the pod wrote and read a file; existing static mounts were unaffected. A zero-risk alternative is a second key with the extra clause, its own Secret and a new StorageClass pointing `node-stage-secret-name` at it; a fresh key also avoids the caching problem. Verify with a pod that mounts a throwaway dynamic PVC and writes a file, not just with a Bound PVC.

This is not specific to Rook, but a migration is exactly when you discover which CSI paths you never exercised.

## The Ceph dashboard with an external Prometheus

Once cephadm's Prometheus, Grafana and Alertmanager were gone with their host, the dashboard needed external ones. What it took:

- **Endpoints through the CephCluster, not `ceph dashboard set-...`:** `dashboard.prometheusEndpoint` for Prometheus, and `cephConfig.mgr` entries for `mgr/dashboard/ALERTMANAGER_API_HOST`, `GRAFANA_API_URL` and `GRAFANA_FRONTEND_API_URL`. Rook applies `cephConfig` with `config assimilate-conf` and never unsets a key later. `GRAFANA_API_URL` is the mgr-side address; `GRAFANA_FRONTEND_API_URL` is what the browser iframes, and it must be HTTPS if the dashboard is served over HTTPS (browsers block mixed-content frames).
- **Scrape the mgr and ceph-exporter every 15 s.** The 20.2.4 dashboard's overview charts use `rate(...[1m])` (for example `sum(rate(ceph_pool_wr[1m]))`). At a 60 s scrape interval a 1-minute window holds one sample, `rate()` returns nothing, and the charts are flat even though every series exists. It looks like a labelling problem and is not.
- **Scrape whichever mgr is active.** The prometheus module answers only on the active mgr; go through a Service that follows the active mgr, or scrape all of them.
- **Grafana embedding:** `allow_embedding` on in Grafana (and no `X-Frame-Options: deny`), anonymous Viewer access to an org that holds the ceph-mixin dashboards with the UIDs the dashboard expects, and a Prometheus datasource **named `Dashboard1`**, which the ceph-mixin dashboards reference. The dashboard's daemon-logs tab links to Grafana Explore with `orgId=1` hardcoded.
- **The daemon-logs (Loki) tab does not appear under Rook.** The dashboard shows it only if the orchestrator reports running `loki` and `promtail` daemons. Under Rook the orchestrator lists only Rook's own Ceph daemon pods, so the tab stays hidden however you run Loki. Use Grafana directly for logs.

## Still open

- The cluster-wide items in [guide.md 7.3](guide.md#73-still-to-do-planned): legacy RGW zone and pool removal (planned for seven days after the S3 cutover), re-enabling mon health checks, removing the `osd_crush_update_on_start` guard, turning the balancer and autoscaler back on, the cipher migration.
- Client network: moving the busiest clients off 1G links matters more for throughput now than anything on the Ceph side.
