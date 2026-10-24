# Pitfalls we hit

Each entry: symptom, root cause, fix, prevention. The class says whether it is general to any cephadm-to-Rook migration, specific to hardware or network, or specific to our tooling. Hardware-specific entries are for Dell 13th-generation servers (R430, R730xd) with iDRAC8; read them as examples of the class of problem.

| ID | Pitfall | Class |
|---|---|---|
| P-1 | UEFI boot of the Talos installed disk fails on Dell 13G | Hardware |
| P-2 | HBA330 legacy option ROM exposes one disk; PERC and NIC PXE variants | Hardware |
| P-3 | MAC-only `deviceSelector` stacks VLANs on VLANs | Talos, general |
| P-4 | Disk letters shift between boots | General |
| P-5 | ARP flux black-holes routed jumbo traffic | Hardware plus network, general lesson |
| P-6 | MDS-only CR with a `startupProbe` override fails | Rook, general |
| P-7 | EMC-branded drives hide serial and model | Hardware |
| P-8 | Fan controller plus thermal trip | Site, hardware |
| P-9 | iDRAC hang under polling load | Hardware |
| P-10 | One-time boot consumed by F2 or dropped by a BMC reset | Hardware |
| P-11 | AWS CRT client fails behind a TLS-terminating proxy | General (any Go-based proxy) |
| P-12 | Parent Flux Kustomization un-suspends children | GitOps, general |
| P-13 | Old OS, RAID and LVM metadata on reused boot disks | General |
| P-14 | Stale hand-written Endpoints and references after hosts convert | General |
| P-15 | Rook mon health check evicts a mon because of a `Completed` pod | Rook, general |
| P-16 | Mon rename stalls (paxos drift, ignored joins, CLI crash) | Ceph, general |
| P-17 | RGW: adopt versus rebuild, and port collisions | Rook, general |
| P-18 | Secrets leaking into tool output | Process |
| P-19 | A CephFS standby on the old stack takes the standby-replay slot | Ceph, general |
| P-20 | Mgr dashboard port clashes | Rook plus cephadm, general |
| P-21 | Rook rewrites `pg_num` of the shared `.rgw.root` pool | Rook, general |
| P-22 | Dynamic CephFS PVCs bind but do not mount | ceph-csi, general |
| P-23 | A LoadBalancer IP answered from a slow or multi-homed node | MetalLB, network |

## P-1. UEFI boot of the Talos installed disk fails (Dell 13G)

- **Symptom.** After `apply-config` the installer wrote the disk and rebooted; the box showed "Boot Failed" and PXE errors, NVRAM held only stale entries, F11 "Boot From File" found no filesystems; the node never joined. In a boot test, sd-boot failed with `Error loading EFI binary \EFI\Linux\Talos-<version>.efi: Not found`.
- **Root cause.** This firmware cannot load Talos' sd-boot UKI or does not expose the installed ESP (siderolabs/talos#11909). Exact cause not determined.
- **Fix.** BIOS (legacy) boot mode; Talos then installs GRUB. Reinstall with the same render.
- **Prevention.** Stage `BootMode=Bios` with a BIOS config job before the window. Generic corollary: verify the installed boot path with a cold boot, not just the installer. Details: [dell-13g-talos-boot.md](dell-13g-talos-boot.md).

## P-2. HBA330 legacy option ROM exposes one disk

- **Symptom.** BIOS-mode "No boot device available"; the hard-disk sequence listed only the bay-0 OSD HDD. On ConnectX hosts the NIC's PXE ROM ran before the disk.
- **Root cause.** Legacy boot through the HBA330 exposes a single boot device, defaulting to the first drive; the controller-level Legacy BIOS setting was Disabled on a host that had run UEFI.
- **Fix.** Legacy BIOS Enabled at controller level; Legacy OpROM Boot Device = Yes on the boot SSD; for a PERC H730 in HBA mode "Select Boot Device"; Hard drive C: first, PXE demoted. HII-only on iDRAC8 2.86: use the console.
- **Prevention.** Do the HII work in the same F2 session as the swap; cold-boot proof before adoption.

## P-3. MAC-only `deviceSelector` stacks VLANs on VLANs

- **Symptom.** Node Ready, but Talos kept creating the Kubernetes VLAN on top of its own VLAN links: `<nic>.100`, `<nic>.100.100` and hash-named links, all carrying the Ceph and Kubernetes IPs, with connected routes landing on a hash-named link, still growing.
- **Root cause.** `deviceSelector: hardwareAddr: <MAC>` also matches every VLAN child interface, because VLAN sub-interfaces share the parent's MAC.
- **Fix.** `physical: true` under `deviceSelector`; `apply-config --mode no-reboot` re-renders; stale VLAN links are not removed live and vanish at the next cold reboot.
- **Prevention.** The line is in the patch template with a REQUIRED comment; the P6 gate is "exactly the NIC plus one VLAN link, no hash-named links".

## P-4. Disk letters shift between boots

- **Symptom.** The new boot disk was `sdb` in the first maintenance boot, `sdc` in the next, `sda` after install; OSD disks moved too. A planned step "wipe sdb" would have hit a 10 TB OSD disk. Not executed.
- **Root cause.** Enumeration order behind an HBA is not stable; a virtual CD adds a device too.
- **Fix and rule.** Select by WWN only: `install.disk: /dev/disk/by-id/wwn-0x<wwn>`, confirmed with `talosctl get disks` (`wwid naa.<...>`, lower case). Never `sdX`, never `wipe sdX` in a runbook, never OSD disks under `machine.disks`.

## P-5. ARP flux black-holes routed jumbo traffic

- **Symptom (after the fifth host's adoption).** Ceph looked perfect: all OSDs up, all PGs active+clean, no slow ops. But 80+ kernel RBD writes to one OSD sat stuck for 30+ minutes on three Kubernetes nodes (`/sys/kernel/debug/ceph/*/osdc` in the hundreds), writer pods stuck in ContainerCreating or D state, IO pressure "full" at 85 to 96% on four nodes, git-over-HTTP timeouts, `libceph: osdN ... socket closed` every 15 minutes. TCP: client to `<ceph-ip>:6800` ESTABLISHED with about 895 KB un-ACKed in Send-Q while the host's Recv-Q was 0: large segments black-holed, small packets passed. Restarting the OSD did not help. ceph-csi's RBD plugin restarted itself several times ("gRPC call stuck for 10m0s").
- **Detection.** From a hostNetwork pod on a worker: `ping -M do` to the host's Ceph IP: 1472 OK; 4000 and 8972 100% loss; to the host's Kubernetes-VLAN IP: 8972 OK; from another Ceph host (same VLAN): 8972 OK. On the L3 switch (`show ip arp vrf <storage-vrf>`): the host's Ceph IP resolved to the MAC of its **onboard 1 Gb NIC** (MTU 1500), not the storage NIC.
- **Root cause.** Linux answers ARP for any local address on any interface (`arp_ignore=0`). The onboard port was `ignore: true` in Talos but its link was up (the BMC shares that LOM through the same cable), on the same VLAN as the storage NIC. It answered ARP for the Ceph IP; the switch learned that MAC; routed traffic from Kubernetes nodes entered through the 1500-byte port and anything larger was dropped. Same-VLAN clients learn the correct MAC, so a jumbo ping from another Ceph host does not detect it.
- **Fix.** Talos `machine.sysctls`: `net.ipv4.conf.{all,default}.arp_ignore=1` and `net.ipv4.conf.{all,default}.arp_announce=2`; apply with `--mode no-reboot`; verify under `/proc/sys`; then `clear ip arp <ceph-ip> vrf <storage-vrf>` on the switch. Result: DF 8972 0% loss, outstanding requests 135 to 1 within 30 s, writers back. Applied to every converted host and put in every remaining patch. The next host, with the sysctls pre-installed, still needed the `clear ip arp` because the switch cache held its maintenance-mode MAC; the jumbo gate caught it.
- **Prevention.** Sysctls in every patch for multi-homed storage nodes; the P6 jumbo gate from a worker pod to the Ceph IP; compare the switch ARP MAC with the NIC MAC. The structural fix is to remove the L2 overlap (re-address Ceph onto the routed VLAN, or keep the BMC-shared port out of the storage VLAN).

## P-6. MDS-only CR with a `startupProbe` override fails

- **Symptom.** `CephFilesystem` phase `Failure`, no MDS pods. Operator: `rook-ceph-mds-<fs>-a Deployment is invalid: spec.template.spec.containers[0].startupProbe: Required value: must specify a handler type`.
- **Root cause.** The draft's `metadataServer.startupProbe` carried only timing fields. Rook uses a user-supplied `startupProbe` **verbatim** (no default handler is merged in), whereas a `livenessProbe` override is merged with Rook's handler. Our lab probe tests had covered mon, mgr and OSD only.
- **Fix.** Remove the `startupProbe` override; keep the liveness override. The failed apply's side effects were limited to a cephx entity and two config rows, reused on the next attempt. Cost: an 11-minute hold.
- **Prevention.** A server-side dry run does not catch it (the failure appears when the operator renders the Deployment). Test the exact `metadataServer` block in a lab, or include a complete handler (exec, tcpSocket or httpGet) in any `startupProbe` override.

## P-7. EMC-branded boot drives hide serial and model

- **Symptom.** A drive labelled HGST appears in Talos with a generic model string and a blank serial (also blank in the BMC); the WWN was not on the label. One also carried an old LVM PV.
- **Root cause.** EMC firmware on array drives.
- **Fix.** The maintenance gate holds until a person reads the label of the drive physically in the bay and confirms bay and WWN. Identification chain: bay (BMC physical disk id, bus target), size, 512-byte sectors, absence of the old boot disk's WWN.
- **Cost.** Holds of 2 and 9.5 minutes.

## P-8. Fan controller and a CPU thermal trip

- **Symptom.** The first host died with "CPU 1 has a thermal trip" about 75 minutes into the window. A custom IPMI-over-LAN fan controller had switched the fans from Dell's automatic profile to a static low duty cycle while the host sat in POST/BIOS/installer with no OS.
- **Root cause.** A static fan profile applied across firmware boot phases on a 1U server.
- **Fix.** Hold the controller at 0 replicas for the window; its graceful exit restores the automatic profile (a SIGKILL might not). A CLI scale is reverted by the GitOps root, so the hold is a commit per window with a restore commit afterwards. Alert if a controller has zero available replicas outside a window.
- **Prevention.** Do not run a custom fan curve across firmware phases.

## P-9. iDRAC hang under polling load

- **Symptom.** `racadm serveraction` timed out five times while power stayed on; "unable to communicate with power management firmware". `racreset` did not fix it. Pulling AC did.
- **Root cause.** Continuous load: dozens of IPMI-over-LAN sessions from fan controllers, Redfish scrapes taking 14 to 31 s each, ad-hoc SSH.
- **Fix.** `racadm racreset soft`, then a 10-minute no-polling period.
- **Prevention.** Hold the BMC pollers; "150 s idle after Off, one power-up, no BMC commands until Talos answers".

## P-10. One-time boot consumed by F2 or dropped by a BMC reset

- **Symptom.** The staged one-time virtual-CD boot did not happen: the host booted a disk instead (once, a leftover GRUB from a previous OS on the "new" SSD).
- **Root causes.** One-time entries are consumed by any intervening POST or F2 exit; `racreset` resets `FirstBootDevice`; a setting changed seconds before power-on lost a race with "iDRAC is not ready".
- **Fix and prevention.** Stage early, idle 150 s, read back and re-set after any reset or F2 session, or use F11 > Virtual CD.

## P-11. AWS CRT client fails behind a TLS-terminating proxy

- **Symptom.** Through a reverse proxy with TLS termination in front of RGW: `aws s3 cp` downloads and `aws s3 rm` fail with `SignatureDoesNotMatch` (403 on the preceding HeadObject); listing and uploads work; the same command succeeds direct to RGW. Go clients (rclone, restic) and aws-cli with `preferred_transfer_client = classic` are unaffected.
- **Experiment.** An echo backend behind the proxy, client sending an explicit `Content-Length: 0`: the backend saw the header for a direct GET and via the proxy on PUT, but NOT via the proxy on GET or DELETE.
- **Root cause.** Go's `net/http` transport writes `Content-Length: 0` only for methods that conventionally carry a body (POST, PUT, PATCH) and omits it for GET/HEAD/DELETE with an empty body. Traefik terminates TLS and re-issues each request through Go's reverse proxy, so the header is dropped. The aws-cli v2 CRT transfer client signs `content-length:0` on bodyless requests; RGW rebuilds the canonical request from the headers it receives, so the recomputed SigV4 signature differs. (The signature link is inferred from the failure pattern plus direct success, not tested against the echo backend itself.)
- **Fix.** Do not put a re-issuing proxy in front of RGW for these clients: TLS passthrough (SNI route, RGW terminates TLS) or, as we did, a dedicated LoadBalancer IP with RGW terminating TLS. Alternatives: document `classic`. A CDN that proxies HTTP has the same class of problem off the LAN. `rgw_sigv4_insecure=true` is an older workaround.

## P-12. Parent Flux Kustomization un-suspends children

- **Symptom.** Writers scaled to 0 and their Kustomizations suspended came back after an unrelated reconcile.
- **Root cause.** The children are objects managed by a parent; the parent re-applies the child manifest from Git (`suspend: false`).
- **Fix.** Suspend the parent as well; resume the parent first, then the children; or commit the freeze. Full procedure: [flux-freeze.md](flux-freeze.md).

## P-13. Old OS, RAID and LVM metadata on reused boot disks

- **Symptom.** A "blank" spare boot disk carried a Windows GPT plus Intel IMSM fake-RAID metadata (assembled as md126/md127). Talos maintenance logged "inactive MD arrays detected" and force-ran them degraded after 30 s. Other disks carried a Proxmox ZFS install, an old LVM PV, a GPT with a ZFS partition.
- **Root cause.** Reused media; a planned workstation wipe had been skipped, and an early runbook said `wipe: false`.
- **Fix and rule.** `install.wipe: true` on the install disk only. The maintenance gate holds only if an **OSD** disk shows md or foreign signatures; the new boot disk's old content is expected. Never declare OSD disks in `machine.disks` (old partitions there would block volume mounting).

## P-14. Stale hand-written Endpoints and other references after hosts convert

- **Symptom.** After two hosts with RGW daemons were converted, 19 of 30 new connections from the operator pod to the S3 ClusterIP failed. Backups looked healthy only because clients retry. Probes for the dead backends were dead too.
- **Root cause.** A selector-less Service with hand-written Endpoints listing three cephadm RGW hosts; two daemons disappeared with their hosts and nobody updated the Endpoints. The conversions also removed one of two SMB nodes and a backup ingress (accepted knowingly).
- **Fix.** Reduce the Endpoints to the live backend; remove dead probe targets.
- **Prevention.** Before each window, list every cephadm service on the host and every consumer-side reference that names it: static Endpoints, haproxy backends, probe and scrape targets (the mgr scrape targets change when the mgr moves), DNS records. ceph-csi mon lists are unaffected because mon IPs never change.

## P-15. Rook mon health check evicts a mon because of a `Completed` pod

- **Symptom (lab run 7).** After enabling Rook's mon health checks, Rook logged `Both mons "c" and "c" are on node X`, evicted mon c and created mon e on an ordinary Kubernetes worker.
- **Root cause.** `evictMonIfMultipleOnSameNode` lists every pod labelled `app=rook-ceph-mon` and skips only `mon_canary` and unscheduled pods, with no phase filter. A `Completed` mon pod left by a node shutdown or conversion next to the live replacement reads as two mons on one node.
- **Fix and prevention.** Pin `placement.mon` to the storage nodes early; sweep terminal (Succeeded or Failed) mon pods before enabling the health check and after every node reboot (one lingers after each); keep `healthCheck.daemonHealth.mon.disabled: true` until the last mon is a Rook pod; gate on the mon record (each mon once, mapping = expected node) and on no two mon pods of any phase on one node. The ConfigMap can be clean while the pods are not: check the pods.

## P-16. Mon rename stalls

See [guide.md 4.3](guide.md#43-step-3-rename-the-mons-to-rook-letters-at-the-same-ips): join drift (`paxos_max_join_drift`, the exact option name, read the value back), join requests ignored under one leader (`election_strategy disallow` plus `disallowed_leader`), force-add with a bare IP, the CLI crash with bracketed address vectors, the stale `CEPHADM_STRAY_DAEMON`, and memory-autotune config-row noise. Raise `paxos_max_join_drift` before the first rename.

## P-17. RGW: adopt versus rebuild, and port collisions

Rook cannot serve a realm-less zone; every adoption path requires hand-editing live RGW metadata, so we rebuilt ([rgw-rebuild.md](rgw-rebuild.md)). Separately: a new RGW on the legacy port 7480 would have been picked up by the cephadm haproxy as an old-zone backend and answered 404 for old buckets; and with `hostNetwork: true` the RGW collides with the mgr dashboard's 8080/8443 on mgr nodes. We used 8081/8444 behind a Service.

## P-18. Secrets leaking into tool output

- **Symptoms.** A throwaway VNC password appeared in a process listing (passed on argv) and was rotated; another was echoed back by the iDRAC CLP shell; `ceph config dump | grep rgw` printed the dashboard's RGW access and secret keys; `mgr/dashboard/MULTICLUSTER_CONFIG` contained a dashboard JWT (already expired when read) that a name-based filter for `KEY|SECRET` did not catch.
- **Prevention.** Passwords by file, stdin or `SSH_ASKPASS` only, never argv; hash instead of print; filter config dumps by name **and** by value (anything matching `key|secret|password|token|jwt|multicluster|credential`, any value starting with `eyJ`); rendered Talos configs contain secrets and are deleted after the window; seed objects are never committed; key material moves between tools through pipes or 0600 temp files, never the terminal.

## P-19. A CephFS standby on the old stack takes the standby-replay slot

When Rook enables `allow_standby_replay`, the mon picks the cephadm standby (lower gid, matching `mds_join_fs`) as standby-replay. Stop the old standby first, assert a Rook daemon is `up:standby-replay`, then stop the old active. Details: [mds-adoption.md](mds-adoption.md#the-failover-ordering-trap).

## P-20. Mgr dashboard port clashes

- **Symptom.** A cephadm standby mgr crashed its dashboard and prometheus modules ("No socket could be created (::, 8080) Address already in use") several times, which surfaced as `RECENT_MGR_MODULE_CRASH` and a dead mgr scrape target. One crash pair was triggered by Rook writing `PROMETHEUS_API_HOST` (a module reload).
- **Root cause.** The CephCluster set the dashboard to plain HTTP on port 8080 (behind a reverse proxy). `server_port` is a cluster-wide mgr setting, so the cephadm mgr on the last host tried to bind 8080 too, where the cephadm haproxy RGW frontend already listened. Rook mgr pods (hostNetwork) hold 8080 on their nodes, which in turn is why the new RGW could not use 8080 there.
- **Fix.** Harmless once the last cephadm mgr was gone (archive the crash reports by id, not with `archive-all` blindly). Choose dashboard and RGW ports with every hostNetwork listener on the storage nodes in mind.

## P-21. Rook rewrites `pg_num` of the shared `.rgw.root` pool

Creating a CephObjectStore made Rook apply the store's `metadataPool` settings plus a hardcoded `pg_num` and `pg_num_min` of 8 to the existing `.rgw.root` pool (shared with the legacy zone), merging it from 32 to 8 PGs. Crush rule and size were unchanged. Expect it and allow it in the POOLRULE gate; setting it back is futile, Rook re-applies it. Details: [rgw-rebuild.md](rgw-rebuild.md#deviations-and-findings).

## P-22. Dynamic CephFS PVCs bind but do not mount

- **Symptom.** A PVC on the dynamic CephFS StorageClass is Bound, the pod hangs in ContainerCreating with `rados: ret=-1, Operation not permitted` at NodeStage.
- **Root cause.** The ceph-csi node key had no OSD cap on the CephFS metadata pool. Static PVs do not need it; dynamic PVs do, because NodeStage reads the volume journal (omap in the metadata pool, namespace `csi`). All our CephFS PVCs were static, so it surfaced only after the migration.
- **Fix** [PROD]. Add `allow r tag cephfs metadata=<fs>` to the node key's `osd` cap (read-only is enough), or use a second key with that clause and its own StorageClass.
- **Gotchas.** ceph-csi caches its RADOS connection per user, so a cap change appears not to work while pods keep retrying; do not restart the nodeplugin (it kills fuse mounts). Clean up test subvolumes with `--group_name csi`. Verify with a pod that mounts and writes, not with Bound. Details: [after-the-migration.md](after-the-migration.md#dynamic-cephfs-pvcs-need-a-metadata-pool-cap-on-the-node-key).

## P-23. A LoadBalancer IP answered from a slow or multi-homed node

- **Symptom.** LAN S3 throughput sat exactly at the 1 Gbit line rate, even from a 2.5G client, and the LB IP had moved between nodes 12 times in 16 hours.
- **Root cause.** MetalLB L2 mode answers ARP from one speaker; with the default advertisement that was a 1G node. The speakers did not run on the tainted storage nodes, so the only fast candidate was one control-plane node. The test client turned out to sit behind a 1G switch uplink as well, so the result was capped twice; check the client's own path (iperf3 to a fast node) before blaming the server side.
- **Fix.** A dedicated `L2Advertisement` for the S3 pool with `nodeSelectors` limited to 10G/25G nodes, the speaker tolerating the storage taint, and `interfaces:` pinned to the Kubernetes VLAN interface so a multi-homed storage host never answers ARP for the LB IP on the Ceph network (the [P-5](#p-5-arp-flux-black-holes-routed-jumbo-traffic) class of failure). After both fixes the same client was limited only by its own link ([after-the-migration.md](after-the-migration.md#performance-after-the-migration)).

## Smaller items

- Expect alert noise at each window: PSU-lost alerts at each shutdown and power-on on effectively single-PSU hosts, disk-space prediction alerts on a fresh Talos `/var`, a system-health alert from the event log entry for the pulled drive, `TargetDown` for removed cephadm mgr and node exporters, and silences expiring mid-window (or outliving the window and masking the next real outage).
- Writer pods stuck after one adoption were not only ARP: two RBD images had no watchers (never mapped) and a pod's volume was mapped through a node whose IO pressure was 85 to 90%.
- Hosts with SMART-failed boot SSDs are risky during phase 1 (a mon rename writes 200 MB plus a moved store to each); rename them last.
- A recurring, unrelated site-wide blip (API server lease renewals failing at the same time every night) restarted ceph-csi provisioner sidecars. Know your background noise before you attribute anything to the migration.
