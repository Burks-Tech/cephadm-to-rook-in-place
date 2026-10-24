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

We benchmarked block (RBD), file (CephFS) and object (S3) storage after the decommission [PROD]: fio on an RBD and a CephFS volume, `rbd bench`, and warp against the object store, all from our fastest Kubernetes node (25GbE), plus S3 over HTTPS from a 2.5GbE workstation on the LAN. A first pass ran in the morning; a re-run that afternoon with more parallelism repeated its single-job results. A monitor sampled cluster IO, the worst OSD commit latency and slow ops every 10 s and would have stopped the load on slow ops; it never fired. All test objects were removed afterwards, no pools were created and `rados bench` was not run.

This page gives the findings, not the figures:

- **No regression.** Every metric we could compare with measurements from before the migration was equal or better. We had no earlier RBD or RGW baseline, and the earlier CephFS numbers came from a different client, so the comparison is rough. **If you do this, take your own baseline before you start.** We wish we had.
- **One client running one fio job is not the cluster's limit.** With four parallel jobs the same client got clearly more RBD bandwidth and IOPS; CephFS on HDD gained mainly on sequential writes. Use several jobs, or several clients, before drawing conclusions about the cluster.
- **Most pods are limited by their node's link.** Most of our Kubernetes nodes are on 1G, so pods there hit their own link long before the cluster's limits. The 25G node was not the limit in any test.
- **Check the client's own path first.** The LAN S3 test first ran at exactly 1 Gbit/s. The LoadBalancer IP was announced from a 1G node (fixed below), and the test workstation also sat behind a 1G switch uplink, so the result was capped twice. After both fixes the workstation was limited only by its own link. An iperf3 run from the client to a fast node shows a cap like that at once.
- **Benchmark the CephFS kernel client with `direct=1` if your kernel has the page-cache leak.** Ours leaks on buffered CephFS writes (Ceph tracker #74156, fixed in Linux 7.0.10 and 7.1). fio's O_DIRECT writes bypass the page cache, and the client's memory did not move during the CephFS write tests.
- **Dynamic CephFS PVCs bound but could not mount.** Fixed below.

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
