# Host conversion: one cephadm host to a Talos node adopted by Rook

This is phase 2 of the [guide](guide.md): one host at a time, stop the cephadm daemons, replace the boot disk and install Talos, join the Kubernetes cluster, add one line to the CephCluster, and Rook starts the same OSDs and the same mon on it. [PROD] on seven hosts. Hardware details are for Dell 13th-generation servers; read them as examples of the class of problem, and see [dell-13g-talos-boot.md](dell-13g-talos-boot.md) for the boot recipe itself.

Example addresses: Ceph network `192.0.2.0/24` on the native VLAN, Kubernetes network `198.51.100.0/24` on tagged VLAN 100. Host `ceph-N` has Ceph IP `192.0.2.2N` and Kubernetes IP `198.51.100.4N`.

## Contents

- [Overview and policy](#overview-and-policy)
- [Preparation, before the window](#preparation-before-the-window)
- [P0: baselines and preflight gates](#p0-baselines-and-preflight-gates)
- [P1: open the window](#p1-open-the-window)
- [P2: switch port and boot media](#p2-switch-port-and-boot-media)
- [P3: stop the cephadm daemons (T0)](#p3-stop-the-cephadm-daemons-t0)
- [P4: shutdown, disk swap, boot recipe](#p4-shutdown-disk-swap-boot-recipe)
- [P5: maintenance gate, then install by WWN](#p5-maintenance-gate-then-install-by-wwn)
- [P6: cold-boot proof, network, jumbo and ARP gate](#p6-cold-boot-proof-network-jumbo-and-arp-gate)
- [P7: add the node, Rook adopts the OSDs and the mon](#p7-add-the-node-rook-adopts-the-osds-and-the-mon)
- [P8: ROOK-2 bounce, close the window, soak](#p8-rook-2-bounce-close-the-window-soak)
- [Rollbacks](#rollbacks)
- [After the first hosts: pin the mons](#after-the-first-hosts-pin-the-mons)
- [The Talos patch per storage host](#the-talos-patch-per-storage-host)
- [Hosts that carry services](#hosts-that-carry-services)

## Overview and policy

| Step | What |
|---|---|
| P0 | Baselines and preflight gates |
| P1 | Open the window: quiesce writers, silences, `noout` |
| P2 | Switch port and boot media staged |
| P3 | Stop the cephadm daemons (T0), window gate |
| P4 | Shutdown, swap the boot disk, hardware boot recipe |
| P5 | Maintenance gate, then install by WWN |
| P6 | Cold-boot proof, network, jumbo and ARP gate |
| P7 | Add-node CR change, Rook adopts the OSDs and the mon, zero-movement gate |
| P8 | ROOK-2 bounce, unset `noout`, remove the cephadm memory mask, soak |

Policy for every window:

- Hold-first. Any result not on the declared allowlist stops the run. Timing numbers are alert and pause points, not aborts.
- Zero-issue standard: the alerting must show nothing (apart from silenced allowlist items) at each gate.
- Workloads keep running, except the heavy writers (downloaders and importers in our case), which are scaled down for the window. No `ceph osd pause` (it stops reads too and leaves kernel clients in D state) and no quota or full-flag tricks (they hand applications ENOSPC).
- No second change at the same time. No other GitOps merges, dependency-bot merges or Ceph changes during a window.
- Never start the next host before the previous soak passes and a person agrees. Mixed hosts for days is fine.
- A person is at the rack from before power-off until the Talos API answers (and for any rollback).

Expected shape of one window (measured; see [the timings table](guide.md#a3-window-timings)): about 30 to 45 minutes from T0 to node Ready and about 35 to 45 minutes from T0 to all OSDs up, then recovery of the degraded writes (4 to 10 minutes) and a soak. The first window took 3 h 25 min because of firmware boot problems.

## Preparation, before the window

No cluster impact.

1. **Read-only fact pass on the host.** Model, storage controller, NIC (MAC, driver), switch port and its configuration, boot disk bay, serial and WWN, every OSD disk WWN and LVM PV UUID, LV tags, daemons that run on the host, `ceph osd ok-to-stop <its OSD ids>`, BMC pre-values. Save `lsblk`, `pvs`, `lvs -o +lv_tags`, `efibootmgr -v` and `ip -br a` output.
2. **Talos machine config for the host**, built from the base worker config plus a small per-host patch ([example](../examples/talos/storage-node-patch.yml)). Validate with `talosctl validate --config <render> --mode metal --strict`, and diff against the previous host's render: only hostname, MAC, IPs, ignored ports and install WWN may differ. A rendered config contains secrets: keep it in a scratch directory and delete it after the window.
3. **The add-node commit prepared, not pushed:** one entry in `spec.storage.nodes`.
4. **Boot media:** the Talos ISO served from a network share to the BMC's remote file share (not browser virtual media, which drops when the viewer closes). The new boot disk labelled, health-checked (SMART wear, power-on hours, grown defects), 512-byte logical sectors, and recorded by WWN. Keep the old boot disk untouched, bagged and labelled "ROLLBACK" until the host has soaked.
5. **BMC console access** (we enabled VNC on the iDRAC with a throwaway password supplied by file or stdin, never argv) so that console work and any boot failure can be seen.
6. **Hold anything that fights the hardware:** in our case a custom fan controller ([P-8](pitfalls.md#p-8-fan-controller-and-a-cpu-thermal-trip)).
7. **Switch port:** trunk configuration prepared.
8. **Alerting:** silences prepared; a "window mode" with a faster scrape; the expected alerts listed.
9. **Rollback path decided:** which of R-A, R-B or R-C applies at each step, and who decides.

## P0: baselines and preflight gates

Preflight, all read-only:

```bash
ceph -s; ceph health detail; ceph pg stat; ceph osd tree down; ceph quorum_status -f json
ceph osd ok-to-stop <host OSD ids>                 # expect ok_to_stop true
ceph fs status; ceph fs get <fs> | grep standby_count_wanted
kubectl -n rook-ceph get cephcluster; kubectl -n rook-ceph get deploy -l app=rook-ceph-mon
```

Gate (all must hold): health equals the declared allowlist; every PG active+clean (record the count); no recovery, backfill or misplaced objects; all OSDs up and in; quorum complete; `ok-to-stop` true; every filesystem keeps a standby that is not on this host; no scrub storm; Rook CephCluster Ready, operator quiet for 10 minutes, mon Deployments exactly as before (Pending pods for unconverted hosts), the mon letter for this host in the ConfigMap mapping equals the `ceph mon dump` entry for this host's IP, `maxMonId` unchanged; the GitOps system all Ready; the host's `lvs` shows every osd-block and osd-db LV with `ceph.cluster_fsid` equal to the fsid; the other boot disks are not getting worse (a worsening one is a hold: it is the likeliest second failure); the BMC is reachable; the switch port is as expected.

Then capture the baseline: the placement hash (`osdmaptool --test-map-pgs-dump-all`), CRUSH and rule dumps, pool details, and a per-OSD baseline for this host's OSDs: `sha256(ceph auth get-key osd.N)` prefix, `ceph osd metadata N` (hostname, `bluefs_dedicated_db`, `default_device_class`, devices), `ceph config get osd.N osd_memory_target` and the `osd/host:<h>` config rows. Chain your baselines: each window's pre-window baseline is compared with the previous window's final state, so drift between windows is visible.

## P1: open the window

1. **Quiesce the writers** you chose. If a GitOps controller owns them, suspend it first or it reverts your scale-down at the next reconcile, and **suspend the parent Kustomization too** ([flux-freeze.md](flux-freeze.md)):

```bash
flux suspend kustomization apps                                  # the PARENT
for a in <writers>; do flux suspend kustomization "$a"; done
kubectl scale sts/<writer> --replicas=0 ...
ceph osd pool stats <data pools>                                # client write rate near zero
```

2. **Alerting:** window mode on; apply the declared silences (each covering window plus recovery, 4 to 6 hours); the alerting gate must be green again with the silences in place. Check the expiry against the expected window length (we had two close calls), and expire every window silence when the window closes, or real outages on that host stay masked for hours.
3. **`ceph osd set-group noout <host>`.** Gate: `OSD_FLAGS ... host <h> has flags noout`.
4. Fan controller (or any other thing that talks to the BMC) held. Verify.

## P2: switch port and boot media

**Switch port.** Each Ceph host had one port in an access VLAN (the Ceph network). The Kubernetes network must reach the host too. Decision: Kubernetes VLAN tagged on the same port, Ceph VLAN native. Applying it is safe while the old OS still runs, because untagged frames stay in the native VLAN. On NX-OS:

```
configure terminal
interface Ethernet1/N
  description <short>                    # long descriptions error out; one command per line
  switchport
  switchport mode trunk
  switchport trunk native vlan 1
  switchport trunk allowed vlan 1,100
  spanning-tree port type edge trunk
  mtu 9216
  no shutdown
end
copy running-config startup-config
```

Verify `show interface ethernetN switchport` (trunk, native and allowed VLANs) and that the host MAC is still learned in the native VLAN. Expect one packet lost at renegotiation. Pitfalls on our NX-OS: a `description` longer than the limit failed ("value of property descr is out of range"), and a description argument swallowed the rest of a `;`-joined line so the trunk command never ran (it happened more than once). Send one command per line, via stdin.

**Boot media staging** (our hardware; see [dell-13g-talos-boot.md](dell-13g-talos-boot.md) for why it has to be BIOS mode): before shutdown, one BMC command per call (iDRAC8 rejects `;`-joined racadm):

- `racadm remoteimage -c -l <nfs-share-path-to-iso>`; `set iDRAC.VirtualMedia.Attached 1`
- `set LifecycleController.LCAttributes.CollectSystemInventoryOnRestart Disabled` (avoids long POSTs and Lifecycle Controller races; restore afterwards)
- `set iDRAC.ServerBoot.FirstBootDevice vCD-DVD` and `BootOnce Enabled`
- BIOS boot mode UEFI to BIOS as a pending value plus a BIOS config job (executes at the next POST)
- save every pre-value to a file first and diff at restore; read everything back before shutdown

## P3: stop the cephadm daemons (T0)

```bash
ssh <host> "sudo systemctl stop ceph-<fsid>.target"       # allow up to 120 s; record T0
# if the host runs SMB/CTDB, stop those units first
```

Expected: the host's OSDs down but in (`noout`), its mon out of quorum (6 of 7; one election if it was the leader), standby MDS gone, a mgr failover if it held the active mgr, the EC pool active+undersized+degraded on 6 of 7 shards (about 800 to 960 PGs degraded, zero inactive), a client stall of at most 25 s at T0 (peering). Gate within 60 s: OSDs down not out, quorum 6/7, `ceph pg dump_stuck inactive` empty, both filesystems have an active MDS, health equals the allowlist. Hold: any inactive PG beyond a 15 s blip, a second OSD or mon down, quorum below 6.

Do not be alarmed by `N remapped pgs` in `ceph -s` right after T0: on the last host we saw 62, all in the EC pool, all `pg_temp` entries giving a temporary primary to EC PGs whose shard-0 OSD was on the stopped host. No up-set change, no backfill. The hash gate at P7 is the verdict, not this counter.

Rollback (host still up on the old OS): `systemctl start ceph-<fsid>.target`, then the P1 block in reverse.

## P4: shutdown, disk swap, boot recipe

1. `sudo shutdown -h now` (or a BMC graceful shutdown), confirm Off (10 to 45 s). Rule that cost us dearly: **after Off, no BMC commands for at least 150 s, then one power-on, and no BMC polling until the Talos API answers** ([P-9](pitfalls.md#p-9-idrac-hang-under-polling-load), [P-10](pitfalls.md#p-10-one-time-boot-consumed-by-f2-or-dropped-by-a-bmc-reset)). Blink the disk LED before shutdown if you want it (it fails when the host is off).
2. A person swaps the boot disk. Identify it by bay and serial; pull only that disk; do not touch the OSD bays. One host was hot-swapped with the OS still running after Ceph was stopped, saving a POST.
3. A person powers the host on and does the hardware boot recipe in the firmware setup ([dell-13g-talos-boot.md](dell-13g-talos-boot.md)), then lets the staged one-time virtual CD boot load the installer. On Dell 13G, F2 followed by save-and-exit consumes the one-time boot device: read it back and re-set it before the final reboot, or use the F11 boot menu to pick the virtual CD.

For other hardware the specifics will differ; the generic rule is that the installed disk must be proven to boot cold before anything else happens (P6).

## P5: maintenance gate, then install by WWN

Poll the Talos API with `talosctl ... --insecure --nodes <ceph-ip> --endpoints <ceph-ip>` and check the exit code, not a grep of its text (a poll that grepped error text declared the host "up" 12 minutes early). Read-only gate:

```bash
T="--insecure --nodes <ceph-ip> --endpoints <ceph-ip>"
talosctl get disks $T; talosctl get discoveredvolumes $T; talosctl get lvs $T
talosctl get links $T; talosctl get addresses $T; talosctl get routes $T
```

Gate: the new boot disk is found **by WWN** (`wwid naa.<wwn>`, lower case; the `/dev/disk/by-id/wwn-0x<wwn>` symlink exists; 512-byte logical sectors), the old boot disk is absent, every OSD disk is present with an unchanged WWN, `lvm2-pv` on all OSD and DB disks with PV UUIDs equal to the pre-window `pvs`, all osd-block and osd-db LVs present, the Ceph NIC up with the right MAC and the Ceph IP. **HOLD if any OSD disk is an md member or has a foreign signature, or the WWN or model is unexpected.** The new boot disk may carry old content (Windows partitions, fake RAID, ZFS, an LVM PV): that is expected and `install.wipe: true` erases it ([P-13](pitfalls.md#p-13-old-os-raid-and-lvm-metadata-on-reused-boot-disks)). A person gives a GO because the next command writes the boot disk; it is the first step whose undo is expensive.

Apply:

```bash
talosctl apply-config --insecure --nodes <ceph-ip> --endpoints <ceph-ip> --file <render>
# Windows talosctl: flags after the subcommand
```

Immediately afterwards detach the ISO (`remoteimage -d`, `Attached 2` = AutoAttach), because the ISO has `talos.halt_if_installed` and booting it again with an installed disk halts before the API. "Applied configuration without a reboot" is normal: Talos installs, then kexecs. The node joins and goes Ready in 30 s to 2 minutes.

## P6: cold-boot proof, network, jumbo and ARP gate

**Cold-boot proof, mandatory, before adoption.** The first boot after an install is a kexec and proves nothing about the disk's boot path. Do `talosctl reboot --mode powercycle --nodes <k8s-ip>` and watch the node return Ready with uptime under 2 minutes, in the expected boot mode, ISO detached, same links, LVs intact. This caught two different "no boot device" problems before Rook was involved.

**Network check.** Links must be exactly: the Ceph NIC (Ceph IP, native VLAN, MTU 9000) plus `<nic>.100` (Kubernetes IP, MTU 9000). No stacked VLAN links ([P-3](pitfalls.md#p-3-mac-only-deviceselector-stacks-vlans-on-vlans)). No address on ignored ports. Connected routes for the Ceph range on the NIC and the Kubernetes range on the VLAN link, default route on the Ceph VLAN. Do not carry a fleet-wide route for the Ceph range via the switch onto this host.

**Jumbo gate, from the right place.** Run `ping -M do -s 8972` from a hostNetwork pod on an **ordinary Kubernetes worker** (a netshoot-style image, because busybox ping cannot force DF; the namespace must allow hostNetwork) to both the host's Ceph IP (the routed path) and its Kubernetes IP; also 1472. A ping from another Ceph host (same VLAN) does NOT exercise the routed path, and passed 8972 while one OSD's traffic was being black-holed ([P-5](pitfalls.md#p-5-arp-flux-black-holes-routed-jumbo-traffic)). Also compare the switch's ARP entry for the host's Ceph IP with the NIC's MAC.

Also gate: `rp_filter` is 0 or loose; kubelet registered with the Kubernetes IP; node label `ceph-storage-node=true`, taint `ceph-storage-node=true:NoSchedule`, and **no `topology.kubernetes.io/*` labels** (Rook turns them into CRUSH buckets, which is data movement); label `kubernetes.io/hostname=<h>` equals the CRUSH host bucket. Rook's crash collector, exporter, a standby mgr and the host's mon pod start on their own once the node is Ready.

## P7: add the node, Rook adopts the OSDs and the mon

One new entry in the CephCluster, delivered as its own commit:

```yaml
spec:
  storage:
    nodes:
      - name: <host>                 # must equal the node's kubernetes.io/hostname label
        resources:
          requests: {cpu: "500m", memory: "<target>"}      # request = osd_memory_target
          limits:   {memory: "<ceil(target / 0.8)>"}       # Rook sets osd_memory_target = 0.8 x limit
        # no devices, no deviceFilter
```

Rook has no memory autotune. It passes `osd_memory_target` on the daemon command line as 80% of the pod memory limit, and the command line beats the config DB. Carry the old per-host cephadm value across: limit = target / 0.8, request = target (Burstable, not BestEffort), no CPU limit. A mis-sized limit becomes an OOM kill, where cephadm had no cgroup limit. Do not set per-class OSD resources (`osd-hdd` etc.); they override node-level settings.

Merge and reconcile at once instead of waiting for the interval (`flux reconcile source git flux-system`, `flux reconcile kustomization <rook-cluster-ks> --with-source`). Watch with `kubectl -n rook-ceph get pods -w`.

What Rook does (the mechanism, [SRC] and [PROD]):

1. A prepare job `rook-ceph-osd-prepare-<host>` is created for every node in `storage.nodes`, even with no devices. It runs `ceph-volume lvm list --format json` on the host's LVM (through nsenter), skips every whole disk that has child devices (so the shared NVMe and the HDDs carrying OSD LVs never enter its device list), and keeps every LV whose tag `ceph.cluster_fsid` equals the Rook Secret's fsid. It logs `N ceph-volume lvm osd devices configured on this node` with exactly the host's OSD ids. If the fsid does not match it reports zero OSDs and still completes: that silent zero is the symptom of an fsid mismatch.
2. It runs only a read-only `sgdisk --print`. No `zap`, `wipefs`, `lvm batch|prepare|create`, `mkfs` or `--osd-id`. There is no wipe path unless `cleanupPolicy.wipeDevicesFromOtherClusters` or a cleanup confirmation is set (keep both off).
3. One Deployment per OSD id is created. Init containers: `activate` (`ceph-volume lvm activate --no-systemd --bluestore <id> <uuid>` into a tmpfs directory, with the key rebuilt from the BlueStore label), `expand-bluefs` (a no-op when sizes match), `cephx-keyring-update` (`ceph auth get-or-create osd.N ...`, idempotent for OSDs created by `ceph-volume`, so the key is unchanged), `chown`. The block, db and wal symlinks come from the LV tags, so shared-NVMe, hand-named and two-extent DB LVs need no special handling. Main container: `ceph-osd --foreground --id N --fsid <fsid> --crush-location="root=default host=<h>"`.
4. The mon pod for this host (letter fixed in phase 1) starts once the node is Ready (hostname nodeSelector plus toleration). `init-mon-fs` runs `ceph-mon --mkfs` on an empty hostPath (`/var/lib/rook/mon-<letter>/data`, on the Talos EPHEMERAL partition), probes the other mons, adopts the real monmap and syncs the store. Because its name and address are already in the monmap, it just joins. Rook never runs `ceph mon add`; the monmap epoch does not change.
5. The OSD memory limit and the OSD memory target are applied; device class setting treats `-EBUSY` (already has a class) as success, so classes are kept.

Gates (poll every 5 s):

1. Prepare pod exists within 4 minutes and is Completed within 5; its log shows exactly the host's ids; `grep -i -E 'zap|wipefs|lvm (batch|prepare|create)|--osd-id|mkfs'` on its log is empty.
2. The OSD Deployments appear within about 2.5 minutes; `--crush-location=root=default host=<h>`; resource limits as configured; no Deployment for any other id.
3. All OSDs up and in within 10 minutes (observed 10 to 181 s); `ceph osd tree` unchanged; `ceph osd metadata` unchanged (hostname, `bluefs_dedicated_db`, class); per-OSD key hashes equal the baseline; `ceph config show osd.N osd_memory_target` shows the expected value (use `config show`, not `config get`).
4. Mon `rook-ceph-mon-<letter>` Running, quorum complete within 10 minutes, same name and address (`v2:ip:3300 + v1:ip:6789`), monmap epoch equal to the baseline, mon Deployments exactly a..g, **no new letter, no mon pod in Completed, Error or Evicted state**, no "failing over" or "mon remove" in the operator log.
5. **Zero-movement gate:** the placement hash, CRUSH, rules, pools and tree hashes identical to the baseline; `ceph pg stat` shows nothing misplaced or remapped. (The tree hash and placement hash include OSD up/acting state, so they only match when every OSD is up.)
6. PGs back to active+clean within 30 minutes (the window's degraded writes recover at up to about 500 MiB/s; 4 to 10 minutes observed).
7. A hostNetwork pod on an ordinary worker reaches the host's Ceph IP on 3300 and 6789, and replies come back correctly (asymmetric-routing check).

Hold triggers: a prepare job listing other ids or any destructive string; a Rook mon with a letter beyond the last; any OSD crash loop; any OSD down more than 90 s outside the ROOK-2 bounce; remap or CRUSH change; mon not in quorum at +10 minutes; operator reconcile over 10 minutes; `SLOW_OPS`. Rollback is R-C. Do not run `ceph osd purge`, `osd destroy` or any `orch` command.

## P8: ROOK-2 bounce, close the window, soak

**Trigger the planned bounce while the host is still under `noout` and quorum is complete**, rather than letting it fire inside the next host's window:

```bash
# silence "OSD down" alerts for ~1 h
for i in <host OSD ids>; do kubectl -n rook-ceph get deploy rook-ceph-osd-$i -o jsonpath='{.metadata.annotations.deployment\.kubernetes\.io/revision}{"\n"}'; done > rev-before.txt
kubectl -n rook-ceph rollout restart deploy/rook-ceph-operator
kubectl -n rook-ceph logs deploy/rook-ceph-operator -f --since=1m | grep -i -E 'updating OSD|ok-to-stop|error'
```

Expect "updating OSD <id>" for each adopted OSD that lacks a device class, exactly one revision bump each, pods back within about 1 to 5 minutes, then the same hash gate. No bump for an OSD with nothing to change is fine (NVMe OSDs whose class was already set, HDD OSDs with a dedicated SSD DB device). A bump of 2 or more, an unexpected mon restart, or any remap is a hold. Each host's bounce does not re-bump earlier hosts' OSDs.

Close the window:

```bash
ceph osd unset-group noout <host>
ceph config rm osd/host:<h> osd_memory_target            # remove the cephadm mask so the config DB tells the truth
for i in <ids>; do ceph config show osd.$i osd_memory_target; done
ceph config dump | grep <h>                              # no host mask left (and no public_addr pins you meant to remove)
```

Do not `ceph orch host rm` and do not touch `osd_memory_target_autotune` during windows. Wait for PGs to be active+clean, then resume the GitOps controllers **parent first, then children**, and confirm the writers are back (a Kustomization with `wait: true` can take over 2 minutes; if a StatefulSet stays at 0, reconcile it).

Restore the BMC (`remoteimage -d`, `Attached 2`, CSIOR Enabled, boot device back to Normal; diff against the pre-values), restore the fan controller, update monitoring probes for the new Kubernetes IP (Talos API :50000 and ICMP), drop the host's dead cephadm node-exporter and ceph-exporter scrape targets, expire the window silences, keep the old boot disk bagged.

Soak: at least 30 minutes active (all PGs clean at the baseline count, zero remap, quorum complete, no `SLOW_OPS`, no new crash, no pod restarts on the host, operator quiet, client pods equal the pre-window list, alerting green), then a passive soak overnight. In practice our later soaks were short; the earlier hosts' hashes stayed identical throughout.

## Rollbacks

Documented, never needed in production. A lab per-host rollback took 2 min 24 s on VMs.

| ID | When | Procedure |
|---|---|---|
| R-A | Before the new boot disk is written (P2 to P5) | Host on the old OS: `systemctl start ceph-<fsid>.target`, clear the one-time boot, detach media, restore BMC settings, unset `noout`, resume writers. Host off or in maintenance: detach media, power off, **a person re-seats the old boot disk**, power on (BIOS boot mode back to UEFI if you changed it, and the controller's legacy-boot state restored; the firmware may have pruned the old boot entry), the cephadm units start by themselves, verify OSDs up and mon back, unset `noout`. 5 minutes if still on the old OS, 25 to 35 after the swap. |
| R-B | Talos installed, Rook not yet adopting (P5 to P6) | As R-A plus `kubectl delete node <h>` once the host is off (Rook then removes the crash collector and exporter Deployments, ROOK-1). |
| R-C | Rook adopted (P7 onward) | 1) Scale the operator to 0 and suspend the Rook cluster Kustomization **first**. 2) Delete only the Rook daemons: the host's OSD Deployments, the prepare job and the `rook-ceph-osd-<h>-status` ConfigMap; scale the host's mon Deployment to 0 and label it `ceph.rook.io/do-not-reconcile=true`. **Never `ceph osd purge/destroy/rm`, never `mon remove`.** `noout` is still set, so the OSDs go down but stay in. 3) Revert the add-node commit. 4) Power off, `kubectl delete node`. 5) Re-seat the old boot disk (R-A). 6) The cephadm mon resyncs from quorum under the same name and address; the OSDs come up from the same LVs with the same ids and keys. Verify zero remap, unset `noout`, bring the operator back only after the revert. Side effect, nothing to do: Rook deleted `client.bootstrap-osd` in phase 1. 30 to 45 minutes. |
| R-D | After `noout` is released | A new reverse window: set `noout` again first, then R-C. |

If the old boot disk dies on rollback (several of ours were SMART FAILED), the cluster stays degraded but serving; either complete the Talos conversion instead, or reinstall the old OS and re-adopt the OSDs with cephadm while the cephadm module is still enabled. After cephadm is decommissioned that re-adoption is no longer available, which is why imaging the old disk offline is cheap insurance.

## After the first hosts: pin the mons

Phase 1 left mon placement unpinned (a required nodeAffinity there would have stalled the detect-version Job). After the first storage node is Ready, add `placement.mon.nodeAffinity` requiring `ceph-storage-node In ["true"]` as its own commit, outside any window. It rolls the running Rook mons one at a time (a restart on the same store). In production it rolled two mons about 40 seconds apart, with quorum never below complete in 9-second samples. The reason is lab run 7: without the pin, a Rook mon could be failed over onto an ordinary Kubernetes node.

Mon health checks (`healthCheck.daemonHealth.mon.disabled`) stay on `true` until all seven mons are Rook pods. A dead Rook mon is not replaced automatically in the meantime; your external alerting is the only guard.

## The Talos patch per storage host

Full example: [examples/talos/storage-node-patch.yml](../examples/talos/storage-node-patch.yml). Rules baked into it:

- `deviceSelector` with `physical: true` (VLAN sub-interfaces share the MAC, [P-3](pitfalls.md#p-3-mac-only-deviceselector-stacks-vlans-on-vlans)).
- `arp_ignore=1` and `arp_announce=2` sysctls on every multi-homed storage node ([P-5](pitfalls.md#p-5-arp-flux-black-holes-routed-jumbo-traffic)).
- Install disk by WWN only, `wipe: true` (only the install disk is wiped). Never declare OSD disks under `machine.disks`; never use `wipe sdX` anywhere in a runbook ([P-4](pitfalls.md#p-4-disk-letters-shift-between-boots)).
- No fleet route for the Ceph range via the switch.
- Label `ceph-storage-node=true`, taint `ceph-storage-node=true:NoSchedule`, no topology labels. Make sure anything that should run on every node (CNI, kube-proxy, log shippers, node exporters) tolerates the taint.
- Pin kubelet to the cluster's version: our base config pinned an older kubelet than the cluster ran; the first converted host joined on the older one and was fixed with a `--mode=no-reboot` apply of a kubelet-image patch. Later hosts had it in the render.
- `talosctl upgrade` on storage nodes must use `--preserve` and the storage-schematic installer (a stock installer strips the extensions); `talosctl reset` wipes the mon store (it resyncs from quorum, but only while quorum exists).

## Hosts that carry services

Before each window, list every cephadm service on the host (RGW, haproxy/keepalived ingress, SMB/CTDB, mgr, MDS, monitoring, exporters) and every consumer-side reference that names it: static Endpoints, haproxy backends, probe targets, scrape targets, DNS records. The services do not move by themselves. In our order:

- Converting two hosts with RGW daemons left a hand-written Endpoints object pointing at dead backends ([P-14](pitfalls.md#p-14-stale-hand-written-endpoints-and-other-references-after-hosts-convert)).
- The host holding the only active MDS of the large filesystem could only be converted after the MDS handover ([mds-adoption.md](mds-adoption.md)).
- The last host carried RGW, the ingress VIP, SMB and the monitoring stack; RGW was rebuilt and cut over the same evening, just before that window ([rgw-rebuild.md](rgw-rebuild.md)), and the SMB outage was accepted until its replacement ([smb-replacement.md](smb-replacement.md)).
