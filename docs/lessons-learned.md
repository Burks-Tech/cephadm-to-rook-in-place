# Lessons learned

## Operating discipline that made it work

1. **Hold-first.** Every unexpected result stops the run; whoever is executing captures evidence and tells the person who decides; nothing auto-reverts; no improvised second remedy. Timing budgets are alerts and pause points. The only automatic stop in the whole project was a 15-minute ceiling in the mon rename loop, and it held, it did not revert.
2. **Plan the whole runbook, then run each step in one motion.** On a failed gate, restore to the last snapshot (lab) or hold (production) and re-plan. No mid-run fixes.
3. **A declared allowlist of expected warnings,** and nothing else is acceptable. Alerting must be green at every gate with the declared silences in place. Write down what must never be allowlisted: inactive PGs for more than 15 s, quorum critical, a second host down, `SLOW_OPS` after T0+60 s, scrub errors, MDS errors, a new mon letter, any Rook mon remove or failover, any CRUSH or PG movement, clock skew, an OSD crash, an OSD down over 90 s outside the planned bounce, an unexplained BMC alert.
4. **Baseline diffing.** Every step is compared with the last baseline using hashes and an explicit allowance list. Check the baseline's own stability first (two baselines 15 minutes apart must compare clean).
5. **One change at a time.** No second Ceph change, GitOps merge or dependency-bot merge during a window. Avoid backup windows and known site-wide disturbances.
6. **Correct over zero-downtime.** Do the clean end state in a planned window rather than building a workaround that has to be unwound later. This is why RGW was rebuilt instead of adopted, why SMB was replaced instead of carried over, and why a size 2/1 pool stays at size 2.
7. **Keep workloads running** through host windows, except the heavy writers. A host window is the same degraded state as a host reboot.
8. **A person at the rack** during every power transition and a rollback disk in a bag until the host has soaked.
9. **Evidence outside the cluster:** a per-window log with timestamps, baseline summaries (hashes and names, never keys), BMC pre/post values, prepare-job logs. Chain windows so drift between them is visible.
10. **Separate deciding from doing.** The person who holds go/no-go at the marked gates (before the disk write, before adoption, before anything irreversible) should not be the one typing the commands at that moment. It made the holds cheap: stopping was the default, not a failure.

## What we would do differently

- **Raise `paxos_max_join_drift` before the first mon rename.** The lab (idle, tiny stores) never showed join drift; production did on the first rename.
- **Test every probe override in the lab**, including MDS. The one block we had not lab-tested (MDS `startupProbe`) is the one that failed.
- **Build the freeze from the GitOps object graph, not from a list of workloads.** Suspend owners and their parents, and look for writers that are not CronJobs (Velero's kopia maintenance). It cost two hours in the S3 window.
- **Capture identities you might need later before the hosts go away**: the Samba machine SID (we lost it and dropped Windows ACLs as a result), and any image digest you validated (the upstream Samba image we tested had been garbage-collected by the time we needed it).
- **Do the deliberate mgr handover** (disable the cephadm module first, then fail over) instead of letting the mgr drift onto Rook during the conversions. It caused no harm, but we did not prove it safe.
- **Remove temporary host pins at the end of each host's window**, not at decommission. We forgot one set.
- **Plan the hardware boot path per controller model before the first window.** The first window's overrun was entirely firmware boot discovery.
- **Put the jumbo-frame gate on the routed path from the start.** A same-VLAN jumbo ping passed while routed traffic was black-holed.
- **Keep soaks honest.** Our later soaks were short; nothing went wrong, but the evidence for first-night behaviour is thin.

## Lab lessons

- The lab must copy production's oddities on purpose (shared DB devices, hand-named LVs, an EC default data pool, a realm-less RGW zone, a size-1 pool), or it will pass where production fails. Ours still missed load-dependent behaviour (mon join drift on a busy cluster, MDS cache recall on a large filesystem).
- The harness can cost more than the migration: six of eight lab runs failed on tooling and false-positive triggers, never on the migration. A runbook with short, real checks and a person deciding on holds beat a strict auto-abort harness by a factor of three in time.
- Keep evidence off the machines you snapshot and roll back. Snapshots do not cover detached disks.

## General versus environment-specific

"General" means it follows from Ceph or Rook behaviour and should apply to any cephadm-to-Rook in-place migration with the same versions. "Environment-specific" is a property of our setup.

### General

| Topic | What applies |
|---|---|
| Identity | Seed `rook-ceph-mon` and `rook-ceph-mon-endpoints` from the live cluster before any CephCluster; mons must be letters; same IPs; `maxMonId` = highest index; port 6789 in `data` |
| Mon rename | `orch daemon rm --force` + `orch daemon add mon host:ip=letter`; epoch +2 per rename; one at a time; `paxos_max_join_drift` before the first; force-add by bare IP; election strategy workaround |
| CephCluster | Image = running digest; `storage.nodes: []`; mon health off; loosened probes; `skipUserCreation`; `osd_crush_update_on_start=false`; no `placement.all`; `prometheusEndpoint` set; mon count versus Node count |
| First reconcile | bootstrap-osd deleted, orchestrator cleared on 20.2.2 to 20.2.4, cipher change on 20.2.4+, blocklist entries, dashboard certificate and admin password |
| OSD adoption | Name-only `storage.nodes` entry; prepare job matches `ceph.cluster_fsid`; same ids and keys; host name = CRUSH bucket; no topology labels; memory target = 0.8 x limit; one ROOK-2 bounce per OSD at most |
| Zero-movement proof | The hash set; balancer off and `noautoscale`; chained baselines |
| Safety | `noout` per host; strictly one host at a time; `ok-to-stop`; EC margin analysis; hold-first |
| Pools and CRs | One CR per commit; POOLRULE; `deviceClass`; size assert; `nodelete`; never delete a CR; Orphan / prune-off delivery |
| MDS | MDS-only CR; old standby first, old active last; no `startupProbe` override without a handler; sizing in Git |
| RGW | Rook cannot serve a realm-less zone: rebuild; Rook rewrites `.rgw.root` pg_num; dashboard RGW key is copied from the mgr into `dashboard-admin`; avoid re-issuing Go proxies for AWS CRT clients |
| SMB | Rook has none; config-key store holds the users; do not copy CTDB; persist `/var/lib/samba`; capture the SID or drop ACL xattrs |
| Decommission | Disable cephadm and smb modules, remove `mgr/cephadm/*` config-keys, stale rows and entities, with a secret backup first |
| Process | Cold-boot proof; WWN-only disk selection; sweep hand-maintained references; parent-aware GitOps freeze |

### Environment-specific

| Area | What it was | Where it bites |
|---|---|---|
| OS | Talos Linux v1.14.2 with a storage schematic; Kubernetes v1.36; image-factory installer | VLAN selector, install disk by WWN, kubelet version drift, `--preserve` on upgrades |
| Servers | Dell R430 (HBA330, shared-LOM iDRAC, Intel 82599) and R730xd (HBA330 or PERC H730, Mellanox ConnectX-4 Lx, dedicated iDRAC); iDRAC8 firmware 2.86 | Boot mode, option ROM, one-time boot, iDRAC load, hidden drive identities |
| Storage layout | LVM OSDs with DB LVs on a shared NVMe, EC 5+2 on exactly 7 hosts, a size 2/1 pool | Window risk (no second failure tolerated), `ok-to-stop` |
| Network | Ceph on a native VLAN and Kubernetes on a tagged VLAN on the same port; routed statelessly on a Cisco Nexus 92160YC-X VRF with symmetric host routes | ARP flux, asymmetric routing, the fleet route, trunk configuration |
| GitOps | Flux v2 with parent/child Kustomizations | Freezes undone by reconciles |
| Ingress | Traefik with the Gateway API, cert-manager, a CDN tunnel for off-site access, split-horizon DNS | RGW endpoint design |
| Monitoring | An external Prometheus/Alertmanager with silences | Allowlists, window mode, stale silences |
| Fans | A custom IPMI fan controller per host | Thermal trip, BMC load |
| Boot media | Reused enterprise SSDs, one per host, old disk kept as rollback | Old signatures, hidden serials |

Translating to your environment: replace "Talos" with whatever OS you will install, as long as it can activate the LVM volume groups, give the node the hostname used as CRUSH bucket, and run Kubernetes. The Dell-specific recipe has no equivalent elsewhere beyond "prove the cold boot".
