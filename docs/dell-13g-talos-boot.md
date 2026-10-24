# Booting Talos cold on Dell 13th-generation servers (R430, R730xd)

This cost the first host window more than two hours. The rest of the hosts took minutes once the recipe was known. It is hardware-specific; the general lesson is in the last section.

Hardware: Dell PowerEdge R430 (HBA330 Mini, Intel 82599 10G, iDRAC8 sharing an onboard LOM port) and R730xd (HBA330 Mini, or PERC H730 Mini in HBA mode; Mellanox ConnectX-4 Lx 25G; dedicated iDRAC8 port). iDRAC8 firmware 2.86. Talos v1.14.2 installed from the ISO over the iDRAC remote file share.

## The recipe

1. **Stage before shutdown** (racadm, one command per call; iDRAC8 rejects `;`-joined racadm):
   - `racadm remoteimage -c -l <nfs-share>:/<path>/talos-<version>-metal-amd64.iso` and `set iDRAC.VirtualMedia.Attached 1`
   - `set LifecycleController.LCAttributes.CollectSystemInventoryOnRestart Disabled` (shorter POST, fewer Lifecycle Controller races; restore afterwards)
   - `set iDRAC.ServerBoot.FirstBootDevice vCD-DVD` and `set iDRAC.ServerBoot.BootOnce Enabled`
   - BIOS boot mode `Bios` as a pending value, plus a BIOS config job (`jobqueue create BIOS.Setup.1-1`) that executes at the next POST
   - Save every pre-value to a file first; diff against it at restore. Read everything back before shutdown.
2. **Shutdown, then leave the iDRAC alone for at least 150 s.** One power-on. No BMC polling until the Talos API answers.
3. **In F2 during the first POST** (these settings are HII-only: no racadm or Redfish attribute on iDRAC8 2.86; drive them over the console, we used the iDRAC VNC server):
   - System BIOS > Boot Settings: Boot Mode = BIOS (the staged job normally did it; verify).
   - HBA330: Device Settings > controller > Controller Management > Change Controller Properties: **Legacy BIOS = Enabled** (it was Disabled on a host that had been running in UEFI). Then Physical Disk Operations > the new boot SSD > **Legacy OpROM Boot Device = Yes** > Apply.
   - PERC H730 in HBA mode: Controller Management > **Select Boot Device** = the new SSD (enable the controller's legacy boot support if offered).
   - System BIOS > Boot Settings > BIOS Boot Settings > Boot Sequence: Hard drive C: first. On ConnectX hosts, demote or untick the NIC's PXE (FlexBoot) entry; it ran before the disk.
4. **F2 save-and-exit consumes the one-time boot device.** Read back `FirstBootDevice`, `BootOnce`, `Attached` and `remoteimage -s` and re-set them before the final reboot, or simply use F11 > BIOS Boot Menu > Virtual CD at the next POST. On one host the consumed one-time boot made the firmware boot the new SSD's leftover GRUB from a previous OS install instead of the installer.
5. Install Talos from the maintenance API (by WWN), then **detach the ISO immediately** (`remoteimage -d`, `Attached 2` = AutoAttach): the Talos ISO has `talos.halt_if_installed` and booting it again with an installed disk halts before the API comes up.
6. **Cold-boot proof:** `talosctl reboot --mode powercycle`, node Ready within about 2 minutes, `/sys/firmware/efi` absent (BIOS mode), system disk is the new SSD.
7. Restore: CSIOR Enabled, `FirstBootDevice` Normal, virtual media AutoAttach; keep BIOS boot mode.

## Why: what failed

### UEFI boot of the installed disk fails

- **Symptom.** In a boot test, the virtual-CD sd-boot menu appeared and its default entry failed: `Error loading EFI binary \EFI\Linux\Talos-<version>.efi: Not found`. In the real window, `apply-config` ran, the installer wrote the disk and rebooted, then the box showed "Boot Failed: Ubuntu" and PXE-E18; the NVRAM BootOrder held only stale Ubuntu and PXE entries; F11 "Boot From File" said "There are no filesystems available"; the node never joined. A second ISO boot then halted (`talos.halt_if_installed`) with no API.
- **Cause.** This Dell UEFI firmware cannot load Talos' sd-boot UKI or does not expose the installed ESP (siderolabs/talos#11909 describes similar reports). We did not determine the exact firmware-level cause.
- **Fix.** BIOS (legacy) boot mode. Talos then installs GRUB (EFI, BIOS, BOOT, META, STATE, EPHEMERAL partitions). Reinstall with the same rendered config (`wipe: true`, same WWN).

### The HBA330 legacy option ROM exposes one disk

- **Symptom.** After the BIOS-mode install and a power cycle: "No boot device available ... Current boot mode is set to BIOS". The BIOS hard-disk drive sequence listed exactly one device: the bay-0 OSD HDD. The boot SSD in a later bay was not offered and could not be reordered.
- **Cause.** Legacy BIOS boot through the HBA330 exposes a single "boot device", defaulting to the first drive. Separately, the controller-level Legacy BIOS setting was Disabled on a host that had run in UEFI.
- **Fix.** Legacy BIOS Enabled at controller level; Legacy OpROM Boot Device = Yes on the boot SSD. The PERC H730 in HBA mode has the equivalent "Select Boot Device" and cold-booted without further changes.

### The iDRAC hangs under polling load

- **Symptom.** After the first host hung, `racadm serveraction powercycle/powerdown` returned "Timeout while waiting for server to perform requested power action" five times while power stayed on; the LC log said "unable to communicate with power management firmware". `racadm racreset` did not fix it (and dropped the one-time boot). Pulling both AC cords did; then one power-up worked. On another host the iDRAC web UI and Redfish hung during POST (ports open, HTTPS timeouts).
- **Cause.** The iDRAC was hit continuously: in about 2 minutes of its log, 62 IPMI-over-LAN sessions from fan-controller pods, 23 Redfish sessions from a metrics exporter (a 60 s scrape taking 14 to 31 s on iDRAC8), and ad-hoc SSH.
- **Fix and prevention.** `racadm racreset soft` over SSH (150 s to 5 min), then a 10-minute quiet period with only Talos-API polling. An AC pull is the real hard reset. Hold anything that polls the BMC for the window; keep console captures to one per 30 s.

### One-time boot lost

- **Causes seen.** A one-time entry is consumed by any intervening POST or F2 exit; an iDRAC `racreset` resets `ServerBoot.FirstBootDevice` to Normal; a boot setting changed 6 seconds before power-on lost a race with "iDRAC is not ready". iDRAC8 exposes no BootNext in Redfish.
- **Prevention.** Stage everything long before the power-on and idle at least 150 s; after any iDRAC reset or F2 session, read back and re-set.

### A custom fan curve and a thermal trip

The first host died with "CPU 1 has a thermal trip" about 75 minutes into the window. A custom IPMI fan controller had switched the fans from Dell's automatic profile to a static low duty cycle while the host sat in POST, BIOS setup and the installer, with no OS load balancing. Hold such controllers for the window (their graceful exit should restore the automatic profile; a SIGKILL may not). If the controller is GitOps-managed, a CLI scale-down is reverted; hold it with a commit. See [P-8](pitfalls.md#p-8-fan-controller-and-a-cpu-thermal-trip).

## Drives that hide their identity

EMC-branded SAS SSDs (relabelled HGST) appeared in Talos with a generic model string and a blank serial (also blank in the iDRAC), and the WWN was not printed on the label. Identification chain we used: bay (iDRAC physical disk id, bus target in `talosctl get disks`), size, 512-byte logical sectors, absence of the old boot disk's WWN, and a person reading the label of the drive physically in the bay. Accept "serial not verifiable in software"; never rely on the serial alone. A SAS address shown by the BMC can be the LU WWN + 1; use the `/dev/disk/by-id/wwn-0x...` form Talos shows and verify it.

## Other small things

- `racadm storage blink` fails when the host is off.
- stdin racadm sessions exit 255 even when every `set` succeeded; verify by read-back.
- Passwords for the iDRAC VNC server: by file or stdin, never on argv (it shows in process listings), and the iDRAC CLP shell may echo a password back.
- `talosctl dmesg` and some subcommands reject `--insecure`; Windows `talosctl` wants flags after the subcommand.
- Expect alerts at each power transition: PSU-lost on effectively single-PSU hosts, a system-health event for the pulled drive.

## The general lesson

Whatever your hardware, **prove the installed disk boots cold before anything else happens**. The first boot after a Talos install is a kexec and proves nothing about the firmware's boot path. A power cycle caught two different "no boot device" problems on our hosts before Rook was involved, when the rollback was still cheap.
