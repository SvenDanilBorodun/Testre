# SPEC A — Phase 0: the WSL field fix (release 2.41.1)

Status: **build-ready spec, architect A, 2026-11-25; the owner's answers to Q1–Q4 (2026-11-25) are folded in
(§14).** Base: `main` at `66382d27` (= `v2.41.0`/`dba3ac81` plus two plan-only doc commits). Proposed build branch:
`feat/phase0-wsl-field-fix` from `main` (not created by the architect). Decisions file: `PLAN.md` + `VERIFICATION.md`
§7 of this folder + §14 of this spec. Scope: `PLAN.md` §6 „Phase 0" and §8 row A, plus the CI/release change for
`release/2.41` the owner approved (WP-A8). Specs B and C are written in parallel and share no file with this one;
Spec C's C4-00 (main stops building WSL) is gated on WP-A8 existing.

**What 2.41.1 ships (owner Q1 = a):** Phase 0 (WP-A1…A6, A8, A7) **plus the arm check's stillness-window fix**
(`physical_ai_server/workflow/autocal/service.py::_rest_window`, the frozen-stream guard in `physical_ai_server.py`,
`test/test_autocal_service.py`, with its own `CLAUDE.md`/changelog/known-issues edits, dated 2026-11-25). That fix
is NOT a work package of this spec: another session lands it on `main`; this spec only requires it on `main` before
the tag (§12 step 1) and keeps its doc edits (WP-A7).

Proof legend used throughout: **CONFIRMED** = observed by a probe listed in §16 (command and output recorded);
**PLAUSIBLE** = argued from source or documentation, not observed; **ASSUMPTION** = a coder must not rely on it
without the named proof. Everything about real Windows, real WSL and Windows PowerShell 5.1 is at best PLAUSIBLE
here and is proven only by CI (pwsh 7 on Linux, mocked Windows surfaces) and by spike **S0** on a real PC (§12).

---

## 0. The bug, proven on today's code

1. **CONFIRMED (probe P4).** `import_edubotics_wsl.ps1` deletes a disk it did not create. With this account
   unregistered and another account's `ext4.vhdx` at the default `-InstallRoot`
   (`$env:ProgramData\EduBotics\wsl`), a fake `wsl` that answers `--import` like WSL's `CreateVhd` on an existing
   file (`Fehlercode: Wsl/Service/RegisterDistro/ERROR_FILE_EXISTS`, nothing created), then:
   - finalize (the GUI's automatic path, Sequence A of research 03 §3.4): rc 1, marker
     `FAILED … Die EduBotics-Umgebung wurde nicht eingerichtet. | Bitte das Protokoll prüfen (häufige Ursachen: zu
     wenig freier Speicherplatz, beschädigter Download) …`, **the other account's disk is gone**;
   - the installer's Step 4 (`-AllowDestructiveReimport`, Sequence B): rc 1, triad „Prüfen Sie: Antivirus-Ausnahme,
     genug Speicherplatz, WSL2 aktiviert.", **the disk is gone**.
   The deleting line is `import_edubotics_wsl.ps1` (failure branch after `wsl --import`):
   `try { Remove-Item -Path (Join-Path $InstallRoot 'ext4.vhdx') -Force -ErrorAction SilentlyContinue } catch {}`.
2. **CONFIRMED (P4-C).** Both classifier twins call that failure nothing: `Get-WslFailureClass` → `""`, code
   `ERROR_FILE_EXISTS`; `wsl_bridge.classify_wsl_failure` → `('', 'ERROR_FILE_EXISTS')`.
3. **CONFIRMED (P13, WSL master source).** WSL's same-path guard `_ValidateDistributionNameAndPathNotInUse` reads only
   the CALLER's `LxssKey`; `RegisterDistribution` sets `deleteFlags = LXSS_DELETE_DISTRO_FLAGS_VHD` only after
   `CreateVhd` returned, so a disk WSL did not create is never deleted by WSL, and a disk it did create is deleted by
   WSL itself on failure. The script's „Clean up partial VHDX" therefore never cleaned anything WSL had not already
   cleaned; it only ever deleted a pre-existing disk. (Fielded WSL builds may differ from master: PLAUSIBLE, S0.)
4. **CONFIRMED (P5).** No executed test could have caught it: the harness's `Get-Item` mock rejects `-Force`, so the
   importer's pre-existing-`InstallRoot` branch exits 1 before `wsl --import` under the mocks, and the fake `wsl`
   ignores the target directory. VERIFICATION §6 C13 („no executed PowerShell test covers the delete branch or
   `ERROR_FILE_EXISTS`") holds and is sharper: the branch was unreachable in the harness.
5. **CONFIRMED (P8).** The 2026-11-20 field state (registered distro, `ext4.vhdx` gone) reaches the GUI's start
   probe and gets the generic Disk remedy: „WSL2 konnte eine virtuelle Festplatte nicht einbinden, die es zum Starten
   braucht (Fehlercode: ERROR_FILE_NOT_FOUND)." followed by „Bitte den PC neu starten …" and an IT text that names
   `ProgramData` — no file named, no way back offered. Today no product path can re-set that account up: the
   installer's `ShouldImportDistro` finds the distro registered, its stamp probe gets no proof, and it skips
   (`robotis_ai_setup.iss::ShouldImportDistro`).

---

## 1. Design decisions (decided by the architect unless marked as an owner question in §14)

| # | Decision | Why |
|---|---|---|
| D-A1 | **The importer never deletes a disk.** The `Remove-Item … ext4.vhdx` block is deleted; no `.ps1`, `.iss` or GUI module may delete a `*.vhdx`, fenced by a static test. | P4. WSL cleans the disks it creates (P13). |
| D-A2 | **A disk file at the import target that is not this account's registered disk is never imported over.** The import refuses with exit **13** and names it: this account's own leftover / another account's (named) / unknown owner. | „Refuse a foreign one" (PLAN §6). Owner is read from the file's owner SID, which current WSL sets explicitly to the importing user (P13). A refusal is safe without proof; the owner only picks the words. |
| D-A3 | **The default import target is per Windows account: `%LOCALAPPDATA%\EduBotics\wsl`**, in every context the importer runs in (the installer's Step 4, finalize, the GUI). An explicit `-InstallRoot` keeps working under the same refusal rules. | Two accounts can then never meet at one path (research 03 §3.4 fix 3). Chosen over BRIEF's `ProgramData\EduBotics\wsl\<SID>`: any standard user can create (and own, or junction) a folder under `%ProgramData%`, while a profile folder is ACLed to its owner, SYSTEM and Administrators. Existing registrations keep their `ProgramData` disk untouched; a later consented rebuild moves them to the new default naturally. |
| D-A4 | **Every `wsl --unregister` of the EduBotics distro goes through ONE guarded function per caller**, which allows it only when the registration's disk is proven missing, or proven this account's (owner SID = current user SID), or — un-elevated only — when ownership is unknown. Refused → exit **13** with its own sentence, nothing unregistered. | WSL deletes the registration's disk under `CoImpersonateClient` (P13): an ELEVATED caller can delete any WSL disk (the DACL grants Administrators), so an elevated rebuild/uninstall whose registration points at another account's disk (the state the bug leaves behind) would delete it. Un-elevated, the DACL protects a foreign disk; an owner of `BUILTIN\Administrators`/`SYSTEM` (possible from older WSL builds, ASSUMPTION) proves nothing and is treated as unknown. |
| D-A5 | **A registration whose disk file is proven missing is named and can be re-set up with consent** (import switch `-ReplaceMissingDisk`, GUI dialog). Without consent the import exits **14**. | The 2026-11-20 field state has no way back today (§0.5). Unregistering a registration whose file does not exist deletes nothing (P13: WSL skips a missing VHD). „Proven missing" = `ItemNotFoundException` / `FileNotFoundError` on the exact path from `HKCU\…\Lxss\{GUID}\BasePath` + `VhdFileName`; any other error is „unknown" and never missing. |
| D-A6 | **The GUI imports for its own Windows account, un-elevated.** A distro that is absent (and WSL works, and no flag routes to finalize) is imported by the GUI running `import_edubotics_wsl.ps1` as the logged-in user — no UAC. The GUI's consented rootfs rebuild and the missing-disk repair go the same way. | „Import without admin per user" (PLAN §6). `wsl --import` and `wsl --unregister` of one's own distro need no admin (INSTALL_SPLIT_PLAN §0, PLAUSIBLE; S0 proves). Every distro change then happens in the account that owns it. |
| D-A7 | **finalize gains `-SkipDistroWork`; the GUI always passes it.** In that mode finalize does only the elevated part (prerequisites, reboot verdict, restart record, flag clear) and never touches a distro. Its default mode (import + pull) is kept, unchanged, for manual runs and for its existing executed tests. | Keeps the heavily fenced default-mode contract and its ~40 executed tests intact; removes the wrong-account import an over-the-shoulder UAC causes (a teacher's credentials would otherwise import and pull 10+ GB into the TEACHER's profile, then the student imports again). |
| D-A8 | **The installer's Steps 4/5 stay as they are** (elevated, acting on the installing account, now per-user by D-A3). Only the uninstaller's `wsl --unregister` is replaced by a guarded script, and the two consent boxes name the account they act on. | Minimal Pascal change in a patch (Pascal is compile-checked only by the W5 prove-build). Accepted cost: when another admin runs Setup over the shoulder, that admin's profile also gets a distro + images (today it gets them in `ProgramData`). Trigger to revisit in „does not do". |
| D-A9 | **The importer imports nothing when it runs as SYSTEM** (exit 0, German line). | An Intune/SYSTEM install would otherwise put a distro into the system profile (D-A3); the GUI imports for the real user at first start. |
| D-A10 | **The importer reports machine-readable lines** `EDUBOTICS_REPORT_<KEY>=<value>` (keys in WP-A1) so the GUI picks its German words from what the import proved, as the finalize marker does today. | The GUI runs the import un-elevated and captures stdout; no marker file needed. |
| D-A11 | **Phase 0 changes no image source, rootfs, migration, Modal or cloud code.** `wsl_rootfs/` is untouched except `README.md` (exempt from `rootfs-version-guard`), so no `ROOTFS_VERSION` bump. (The release's server images DO change, through the stillness fix the owner put into 2.41.1 — Q1.) | Proven by the WP file lists. The release pipeline rebuilds and retags every image (§12, R2). |
| D-A12 | **`release/2.41` gets CI and a maintenance release mode before its first fix** (WP-A8, owner Q3 = a, the one pre-approved ask-first item): `ci.yml` also runs on `release/**`; `release.yml` decides from git ancestry whether a tag's commit is on `main` (everything as today) or only on a `release/*` branch (maintenance: W1, W2a, W2b, W2, W3 skipped; W4, W5, W5b and the summary run). **WP-A8 lands before the `v2.41.1` tag**, because `release/2.41` is cut from that tag and every later tag on it runs the `release.yml` and `ci.yml` of its own commit; the 2.41.1 run then exercises the new graph's normal path once before any maintenance tag depends on it. | `ci.yml` runs only for `main` today and `release.yml` would deploy the branch's old cloud code, which `refuse_rollback.sh` refuses once `main` has deployed newer (§13 #7; P18, P22). |
| D-A13 | **The safe part ships alone if S0-3 fails** (owner Q2 = a): WP-A1, A5, A6, A8 and a „safe-only" A7 go into the tag; A2–A4 stay parked (§12 step 4b). | The deletion must not wait for the per-user flow. |

The owner-decided alternative paths that were considered and rejected are in §14 (Q2 keeps a fallback if S0 shows a
standard user cannot import).

---

## 2. The disk-state space — required behaviour and its executed test

„Target" = the import's `InstallRoot` (default `%LOCALAPPDATA%\EduBotics\wsl`), its file `ext4.vhdx`.
„Registered disk" = `BasePath` (stripped of `\\?\` / `\\?\UNC\`) + `\` + `VhdFileName` of this account's `EduBotics`
registration. Tests are in `robotis_ai_setup/tests/test_installer_pwsh_executed.py` unless another file is named.

| # | State | Entry point(s) | Required behaviour | Exit | Test (WP) |
|---|---|---|---|---|---|
| S1 | not registered, no file at target | Step 4, finalize default, GUI | create dir (refuse reparse point / non-dir), 20 GB + SHA checks, import | 0 | existing `FinalizeEndToEndTest` imports, now into `LOCALAPPDATA` (A1) |
| S2 | not registered, file at target, owner = this account | all | refuse, delete nothing, `DISK_OWN_PROBLEM_DE` | 13 | `DiskSafetyExecutedTest.test_a_disk_at_the_target_is_never_imported_over_or_deleted[own]` (A1) |
| S3 | not registered, file at target, owner = another account | all | refuse, name the account, `DISK_OTHER_PROBLEM_DE` | 13 | same test `[other]` (A1) |
| S4 | not registered, file at target, owner unreadable / a group SID | all | refuse, `DISK_UNKNOWN_PROBLEM_DE` | 13 | same test `[unknown]`, `[group]` (A1) |
| S5 | not registered, another account's disk in the SHARED `ProgramData\EduBotics\wsl` (Sequence A/B shape) | all with default root | import into this account's own target; the shared disk's bytes unchanged | 0 | `test_sequence_a_leaves_the_shared_programdata_disk_alone` (A1) |
| S6 | explicit `-InstallRoot` = shared folder holding another account's disk | manual | refuse as S3 | 13 | `test_an_explicit_shared_install_root_refuses_too` (A1) |
| S7 | target free at the check, WSL answers `ERROR_FILE_EXISTS` (a file appeared, or another own distro uses the path) | all | never delete; re-probe; present → S2–S4 words; absent → S4 words | 13 | `test_wsl_refusing_an_existing_file_is_named_not_deleted` (A1) |
| S8 | import fails after WSL created the file (WSL killed before its own cleanup) | all | delete nothing; classify as today (hypervisor 11 / other 1) | 11 / 1 | `test_a_failed_import_deletes_nothing` (A1) |
| S9 | target is a reparse point / not a directory | all | refuse (existing code, now executed) | 1 | `test_a_reparse_point_target_is_refused` (A1) |
| S10 | registered, stamp = shipped | all | skip import, data kept (existing) | 0 | existing `test_a_matching_stamp_keeps_the_distro` |
| S11 | registered, VM does not start for the stamp probe | all | never rebuilt, exit 11 + report kind/code (existing + report lines) | 11 | existing `test_a_dead_vm_*` + `test_the_hypervisor_report_carries_kind_and_code` (A1) |
| S12 | registered, stamp differs / absent, no consent | Step 4 declined, finalize default, GUI race | refuse (existing) | 12 | existing |
| S13 | registered, stamp differs / absent, consent (`-AllowDestructiveReimport` / `-Force`) | Step 4, GUI rebuild | gate (D-A4) → unregister → import into own target | 0 / 13 | `test_the_rebuild_gate` matrix (A1) + existing stampless/different-stamp tests with an own disk (A1) |
| S14 | registered, registered disk proven missing, no consent | GUI, finalize default | name the file, nothing changed | 14 | `test_a_missing_disk_is_named_and_rebuilt_only_with_consent` (A1) |
| S15 | registered, registered disk proven missing, `-ReplaceMissingDisk` | GUI repair | re-check missing → unregister (deletes nothing) → import into own target | 0 | same test (A1) |
| S16 | `-ReplaceMissingDisk` but the disk is present / unknown | GUI race | never unregister; normal path | 11 / 12 / 0 | same test, last subTest (A1) |
| S17 | registration unresponsive (WSL not answering) | all | refuse before any disk logic (existing) | 1 | existing `test_consent_cannot_rebuild_over_a_wsl_that_does_not_answer`, `test_an_unresponsive_wsl_never_imports` |
| S18 | registry store names `EduBotics` but `BasePath`/`VhdFileName` unreadable or empty (disk path unknown) | rebuild | elevated → refuse 13; un-elevated → allow | 13 / 0 | `test_the_rebuild_gate[unknown-path]` (A1) |
| S19 | running as SYSTEM | Step 4 under Intune | import nothing | 0 | `test_system_account_imports_nothing` (A1) |
| S20 | `LOCALAPPDATA` empty and no `-InstallRoot` | any | refuse | 1 | `test_no_localappdata_refuses` (A1) |
| S21 | locked / in-use `ext4.vhdx` at target | all | Get-Item may still succeed (owner read may fail) → S2–S4 refusal; never deleted | 13 | covered by S2–S4 (owner `throws` cell); real lock: S0-8 |
| S22 | registered disk present but not this account's (the post-bug „double-pointing" state) | GUI start | the VM does not start (access denied un-elevated) → Disk remedy as today (owner unreadable un-elevated, cannot be named — residual R10) | — | `DistroStartProbeTest.test_a_present_disk_keeps_the_disk_words` (A4) |
| S23 | registered disk present, start fails naming WSL's own `system.vhd` | GUI start | WSL-system-disk sentence, not the ext4 one | — | `DistroStartProbeTest.test_wsl_system_disk_failure_is_named` (A4) |
| S24 | registered disk proven missing | GUI start | `DISK_MISSING_*` dialog, „Jetzt neu einrichten?" (default Nein) → GUI runs import `-ReplaceMissingDisk` | — | `DistroStartProbeTest.test_a_missing_disk_is_named_and_offers_the_rebuild` (A4) |
| S25 | uninstall, consent „Ja", registered disk own / missing | uninstaller (elevated) | unregister | — | `UninstallGuardExecutedTest` (A5) |
| S26 | uninstall, consent „Ja", registered disk other / unknown | uninstaller | keep, log the reason | — | same (A5) |

---

## 3. Every path that can delete, unregister, overwrite or re-import (code truth, read 2026-11-25)

| Path (file::symbol) | What it does | States that reach it today | After this spec |
|---|---|---|---|
| `installer/scripts/import_edubotics_wsl.ps1` failure branch after `wsl --import` (`Remove-Item … 'ext4.vhdx'`) | **deletes** whatever `ext4.vhdx` is at `InstallRoot` | any failed import; deletes a FOREIGN disk whenever WSL refused it as existing (P4) | **deleted** (WP-A1) |
| `import_edubotics_wsl.ps1` upgrade branch (`wsl --unregister $DistroName *>$null`) | WSL deletes the registration's disk (under the caller's token) | registered + VM up + stamp differs/absent + `-Force` or `-AllowDestructiveReimport` | only inside `Invoke-GuardedUnregister` (D-A4) (WP-A1) |
| `import_edubotics_wsl.ps1` `wsl --import` | creates a disk + registration | not registered, or after the unregister above | refuses a file at the target (D-A2); default target per account (D-A3) (WP-A1) |
| `installer/robotis_ai_setup.iss` `[Run]` Step 4 (`Check: ShouldImportDistro`, `-AllowDestructiveReimport`, elevated = installing account) | runs the import above | install/upgrade without a pending reboot; consent box on a proven stamp mismatch/absence | unchanged call; the script is safe; consent box names `{username}` (WP-A5) |
| `.iss` `[UninstallRun]` `wsl.exe --unregister EduBotics` (`Check: ConfirmDistroWipe`, elevated) | WSL deletes the UNINSTALLING account's registered disk | uninstall + „Ja"; can delete another account's disk in the double-pointing state (D-A4, PLAUSIBLE) | replaced by `uninstall_unregister_distro.ps1` (guarded) (WP-A5) |
| `installer/scripts/finalize_install.ps1` Phase 1 (`& import_edubotics_wsl.ps1 @importArgs`) + Phase 2 pull | runs the import (elevated, as the UAC account) | GUI: absent distro; flag pending; rootfs mismatch consent | default mode kept for manual runs (passes 13/14 through, WP-A1); the GUI uses `-SkipDistroWork` (WP-A2/A3) |
| `gui/app/gui_app.py::_prompt_finalize_install` (+ `reason="rootfs_mismatch"` → `-AllowDestructiveReimport`) | elevated finalize; consented rebuild | absent distro, flag, rootfs mismatch, „Einrichtung abschließen (Administrator)" button | always `-SkipDistroWork`; rebuild moves to `_prompt_rebuild_distro` → un-elevated import (WP-A3) |
| `gui/app/gui_app.py::_run_prerequisite_checks_body` absent branch | routes to elevated finalize | every launch with no registration for this account | un-elevated import for this account (WP-A3) |
| `installer/scripts/migrate_from_docker_desktop.ps1` step 3 | `wsl --unregister` of `docker-desktop`, `docker-desktop-data` only | Docker Desktop present | unchanged; allow-listed in the fence (exact names, cannot match `EduBotics`) |
| `gui/app/docker_manager.py::factory_reset` | `docker volume rm` of the data volumes inside the distro (consented dialog) | „Daten zurücksetzen" | unchanged (not a disk delete) |
| `.iss` `[InstallDelete]` | wipes `{app}\gui`, `{app}\scripts`, `{app}\wsl_rootfs` | every upgrade | unchanged (no disk) |
| `.iss::CurStepChanged(ssInstall)` | `docker compose down` in the installing account's distro | every install | unchanged (residual: a student's running stack is not stopped by another admin's Setup) |
| `preflight_system.ps1` check 5 / `gui_app.py` exit-0 branch / `device_manager.diagnose_usb_environment` | ADVISE „den EduBotics-Installer erneut ausführen, während Sie mit Ihrem eigenen Konto angemeldet sind — das behebt beide Fälle" | disk in ProgramData + absent for this account | that advice is Sequence B's trigger (P4-B) and cannot fix a standard student's account; removed everywhere (WP-A1, WP-A3) |

Nothing else in `installer/`, `gui/app/`, `gui/app/scripts/` or `wsl_rootfs/` deletes, unregisters or imports
(CONFIRMED by grep P2: the only `*.vhdx` deletion is the import line above; the only `--unregister` sites are the
three listed).

---

## 4. What „import without admin per user" requires, and the cut

What is admin-only today and stays so: the WSL/VMP Windows features (`install_prerequisites.ps1`, DISM, 3010
reboot), the usbipd MSI and its policies (`configure_usbipd.ps1`, `bind_devices.ps1`), the
`%ProgramData%\EduBotics\logs` leaf ACL. These are finalize's Phase 0 and the installer's Steps 1–3 — IT, once per PC.

What needs no admin (PLAUSIBLE: INSTALL_SPLIT_PLAN §0 web sources; S0-3 proves on a real PC): `wsl --import` and
`wsl --unregister` of a distro in the caller's own hive into a folder the caller owns, `wsl -d …`, `docker pull`
inside the distro (the GUI already pulls un-elevated today), reading `HKCU\…\Lxss`.

The cut taken (D-A6/7/8): the GUI imports per account without UAC; finalize keeps its elevated part only when the
GUI calls it; the installer keeps importing for whoever runs it. This is larger than a one-line fix (seven work
packages, §9) but every package is independently verifiable and the first one (WP-A1) alone removes the data loss.
Conflicts with load-bearing contracts, and how they are kept:
- finalize's exit codes and marker — default mode byte-for-byte unchanged in behaviour (plus pass-through of 13/14);
  skip mode is new and documented;
- `.reboot_required` — still finalize's to clear; in skip mode it is cleared after the verdict, because the work it
  guards that needs admin is done, and an absent distro routes the GUI by itself;
- the GUI's „no consent dialog for finalize" rule — unchanged (finalize never imports for the GUI any more, so there
  is nothing destructive to consent to there); the rebuild consent moves to the import;
- `_run_privileged` — untouched; the import runs as a plain child process.

If S0 shows that a standard user cannot import on the target PCs, see owner question Q2.

---

## 5. German wording (Rule §1) — every new situation, where it shows, its twin fence

All PowerShell constants are one-line double-quoted assignments without `"`, `$`, backtick or typographic quotes
(„ “ ” are quote characters to PowerShell) — the same rule `finalize_install.ps1`'s header states; names in them are
set in parentheses, as `preflight_system.ps1` already does. Placeholders `{ordner}`, `{datei}`, `{konto}` are filled
with `.Replace(...)` (literal, CONFIRMED P11) in PowerShell and `.replace(...)` in Python.

| Constant (PS file / GUI) | Text | Shown | Fence |
|---|---|---|---|
| `$DISK_OTHER_PROBLEM_DE` (import) / `DISK_OTHER_PROBLEM_DE` (gui_app) | „Im Ordner {ordner} liegt bereits eine EduBotics-Festplattendatei (ext4.vhdx) des Windows-Kontos ({konto}). EduBotics verwendet sie nicht, überschreibt sie nicht und löscht sie nicht." | import output / finalize marker / GUI dialog „EduBotics-Umgebung kann nicht eingerichtet werden" | `ImportRemedyTwinTest` (A3) byte-equal |
| `$DISK_OWN_PROBLEM_DE` | „Im Ordner {ordner} liegt eine EduBotics-Festplattendatei (ext4.vhdx) dieses Windows-Kontos, die zu keiner eingerichteten EduBotics-Umgebung gehört — vermutlich der Rest eines abgebrochenen Einrichtungsversuchs. Sie kann Daten enthalten; EduBotics überschreibt sie nicht und löscht sie nicht." | same | same |
| `$DISK_UNKNOWN_PROBLEM_DE` | „Im Ordner {ordner} liegt bereits eine EduBotics-Festplattendatei (ext4.vhdx), die zu keiner eingerichteten EduBotics-Umgebung dieses Windows-Kontos gehört. Wem sie gehört, lässt sich nicht feststellen; EduBotics überschreibt sie nicht und löscht sie nicht." | same | same |
| `$DISK_IN_THE_WAY_NEXTSTEP_DE` | „Bitte die IT-Betreuung informieren und ihr das Protokoll zeigen. Die Datei bitte nur dann umbenennen oder entfernen, wenn sicher ist, dass niemand ihre Daten mehr braucht; danach EduBotics erneut öffnen." | with the three above | same |
| `$REBUILD_NOT_OURS_PROBLEM_DE` | „Die EduBotics-Umgebung dieses Windows-Kontos verweist auf die Festplattendatei {datei}, die nicht nachweislich zu diesem Konto gehört. Sie wird deshalb weder neu aufgebaut noch gelöscht." | import / GUI rebuild | same |
| `$REBUILD_NOT_OURS_NEXTSTEP_DE` | „Bitte die IT-Betreuung informieren und ihr das Protokoll zeigen." | with it | same |
| `$DISK_MISSING_PROBLEM_DE` | „Die Festplattendatei der EduBotics-Umgebung dieses Windows-Kontos fehlt: {datei}. Was darin gespeichert war (Datensätze, die nicht zu Hugging Face hochgeladen wurden, heruntergeladene Modelle, die Roboter-Studio-Kalibrierung), ist dort nicht mehr vorhanden." | import / GUI start dialog | `ImportRemedyTwinTest` (A4 adds) |
| `$DISK_MISSING_NEXTSTEP_DE` | „EduBotics kann die Umgebung für dieses Windows-Konto neu einrichten. Dabei wird nur der Eintrag der fehlenden Umgebung entfernt; es wird keine Datei gelöscht." | with it | same |
| `DISK_MISSING_DIALOG_TITLE_DE` / `DISK_MISSING_QUESTION_DE` (GUI only) | „EduBotics-Umgebung neu einrichten" / „Jetzt neu einrichten?" | askyesno, default Nein | GUI unit test (A4) |
| `$SYSTEM_ACCOUNT_SKIP_DE` (import only) | „Die EduBotics-Umgebung wird nicht für das Systemkonto eingerichtet, sondern beim ersten Start von EduBotics für das angemeldete Windows-Konto." | Step 4 log | executed test (A1) |
| `$NO_LOCALAPPDATA_DE` (import only) | „Der Ordner für lokale Programmdaten dieses Windows-Kontos (LOCALAPPDATA) ist nicht bekannt — die EduBotics-Umgebung kann nicht eingerichtet werden." | import output | executed test (A1) |
| import triad (existing line, extended) | „Prüfen Sie: Antivirus-Ausnahme, genug Speicherplatz, WSL2 aktiviert, Zielordner nicht komprimiert oder verschlüsselt." | import output | existing assertions keep their prefix |
| `IMPORT_STARTED_DE` (GUI) | „Die EduBotics-Umgebung wird jetzt für dieses Windows-Konto eingerichtet (das dauert einige Minuten; danach werden die Docker-Images geladen) …" | Protokoll + status | GUI test (A3) |
| `LEGACY_DISK_INFO_DE` (GUI) | „[INFO] Unter ProgramData liegt bereits eine EduBotics-Umgebung eines anderen Windows-Kontos oder einer früheren Einrichtung. Sie bleibt unverändert; dieses Windows-Konto bekommt eine eigene Umgebung (mehrere GB zusätzlicher Speicherplatz)." | Protokoll, only when `%ProgramData%\EduBotics\wsl\ext4.vhdx` is proven present | GUI test (A3) |
| `ACCOUNT_INFO_DE` (GUI) | „Der Administrator-Teil der Einrichtung lief unter dem Windows-Konto „{admin}“. Die EduBotics-Umgebung wird jetzt für dieses Windows-Konto („{konto}“) eingerichtet." | Protokoll after finalize exit 0 when the marker's `user=` differs (casefolded) | existing marker tests, re-pointed (A3) |
| `DISTRO_SETUP_DIALOG_TITLE_DE` (GUI) | „EduBotics-Umgebung kann nicht eingerichtet werden" | dialog title for 13 and failed imports | GUI test |
| `IMPORT_FAILED_PROBLEM_DE` / `_NEXTSTEP_DE` (GUI) | „Die EduBotics-Umgebung wurde nicht eingerichtet." / „Bitte das Protokoll prüfen (häufige Ursachen: zu wenig freier Speicherplatz, ein komprimierter oder verschlüsselter Ordner, ein beschädigter Download). Danach EduBotics erneut öffnen." | exit 1 | GUI test |
| `IMPORT_NOT_STARTED_PROBLEM_DE` / `_NEXTSTEP_DE` (GUI) | „Die Einrichtung der EduBotics-Umgebung konnte nicht gestartet werden." / „Bitte EduBotics neu starten. Hilft das nicht, bitte die IT-Betreuung informieren und ihr das Protokoll zeigen." | runner launch failure | GUI test |
| `IMPORT_SUCCESS_BUT_MISSING_DE` (GUI) | „Die Einrichtung meldet Erfolg, aber die EduBotics-Umgebung fehlt weiterhin. Bitte den PC neu starten (Neu starten, nicht Herunterfahren) und EduBotics danach erneut öffnen." | second absent after a reported import success | GUI test |
| `REBUILD_PROMPT_DE` (GUI, replaces the rootfs dialog body) | „Die EduBotics-Umgebung dieses Windows-Kontos muss neu aufgebaut werden (System-Update).\n\nDabei werden alle lokal gespeicherten Daten dieses Kontos gelöscht:\n  - aufgenommene Datensätze (falls nicht zu Hugging Face hochgeladen)\n  - heruntergeladene Modelle\n  - die Roboter-Studio-Kalibrierung\n\nTipp: Datensätze vorher in der Web-Oberfläche zu Hugging Face hochladen.\n\nJetzt neu aufbauen?" (title unchanged „EduBotics-Umgebung neu aufbauen", default Nein) | rootfs mismatch, exit 12 | GUI test |
| `_setup_unfinished_de` „consent" | „Die EduBotics-Umgebung muss neu aufgebaut werden. Bitte EduBotics erneut öffnen und dem Neuaufbau zustimmen — Datensätze vorher in der Web-Oberfläche zu Hugging Face hochladen." | scan refusal | `ScanRefusalAndSetupContextTest` |
| `WSL_SYSTEM_DISK_PROBLEM_DE` / `_NEXTSTEP_DE` (GUI only) | „WSL2 konnte seine eigene Systemdatei (system.vhd) nicht einbinden. Die Daten der EduBotics-Umgebung sind davon nicht betroffen." / „Bitte die IT-Betreuung bitten, WSL zu reparieren: als Administrator wsl --update ausführen, danach den PC neu starten (Neu starten, nicht Herunterfahren) und EduBotics erneut öffnen." | start-probe dialog | GUI test (A4) |
| `$VIRT_DISK_NEXTSTEP_DE` (finalize) / `VIRT_DISK_NEXTSTEP_DE` (GUI) — reworded | „Bitte den PC neu starten (Neu starten, nicht Herunterfahren) und EduBotics danach erneut öffnen. Hilft das nicht, bitte die IT-Betreuung informieren: prüfen, ob die Festplattendatei ext4.vhdx der EduBotics-Umgebung vorhanden ist (im Benutzerordner unter AppData\Local\EduBotics\wsl oder unter ProgramData\EduBotics\wsl) und nicht von einem Virenschutz- oder Backup-Programm gesperrt wird, ob der Ordner komprimiert oder verschlüsselt ist und ob genug Speicherplatz frei ist; danach WSL mit wsl --update aktualisieren." | existing Disk remedy | existing `test_the_gui_remedies_are_finalizes_words` |
| `VIRT_FIRMWARE_PROBLEM_DE` / `_NEXTSTEP_DE` (GUI, new twin of finalize's) | finalize's text verbatim | GUI-run import exit 11 with report kind Firmware | `test_the_gui_remedies_are_finalizes_words` gains `FIRMWARE` (A3) |
| `device_manager.diagnose_usb_environment` missing-distro sentence | „Die EduBotics-Umgebung ist für dieses Windows-Konto nicht eingerichtet. Bitte EduBotics schließen und erneut öffnen — die Einrichtung startet dann automatisch." | scan diagnosis | `DeviceManagerMissingDistroSentenceTest` (A3) |
| `preflight_system.ps1` check 5, split branch (now `Emit INFO`) | „Unter ProgramData liegt eine EduBotics-Umgebung, die mit dem Windows-Konto ($markerUser) eingerichtet wurde; für das angemeldete Konto ($env:USERNAME) ist dort keine registriert. Sie bleibt unverändert — EduBotics richtet für dieses Konto beim Start eine eigene Umgebung ein." | Protokoll (preflight block) | `PreflightAccountScopeTest` (A1) |
| `preflight_system.ps1` check 5, else branch (now `Emit INFO`) | „Unter ProgramData liegt eine EduBotics-Datenträgerdatei, für das angemeldete Windows-Konto ($env:USERNAME) ist dort aber keine Umgebung registriert. Sie gehört einem anderen Windows-Konto oder einer früheren Einrichtung und bleibt unverändert — EduBotics richtet für dieses Konto beim Start eine eigene Umgebung ein." | same | same |
| `verify_system.ps1` absent distro (Write-WARN, no failure) | „Für dieses Windows-Konto ist noch keine EduBotics-Umgebung eingerichtet — EduBotics richtet sie beim ersten Start ein." | „Installation prüfen" / Setup Step 6 | `VerifyExecutedTest` (A3) |
| finalize skip-mode step line | „Die EduBotics-Umgebung richtet EduBotics anschließend für das angemeldete Windows-Konto ein." | finalize transcript | executed test (A2) |
| `.iss::ConfirmDistroWipe` first line + new last line | „Sollen auch alle EduBotics-Daten des Windows-Kontos {username} gelöscht werden?" … „Die EduBotics-Umgebungen anderer Windows-Konten bleiben unverändert." | uninstall | `.iss` static test (A5) |
| `.iss::ShouldImportDistro` first line | „Die EduBotics-Umgebung des Windows-Kontos {username} muss neu aufgebaut werden (System-Update)." | Step 4 consent | `.iss` static test (A5) |
| `uninstall_unregister_distro.ps1` lines | „EduBotics-Umgebung dieses Windows-Kontos entfernt." / „Die EduBotics-Umgebung wird nicht entfernt: Ihre Festplattendatei {datei} gehört nicht nachweislich zu diesem Windows-Konto." / „WSL antwortet nicht — die EduBotics-Umgebung wird nicht entfernt." | uninstall log | executed test (A5) |

Every `[FEHLER]`/`[WARNUNG]` line must pass `ci.yml::german-strings-lint` (literal ä ö ü ß).

---

## 6. Machine-readable report lines (D-A10)

`import_edubotics_wsl.ps1` prints, with `Write-Host`, zero or more lines of exactly `EDUBOTICS_REPORT_<KEY>=<value>`
(value single-line, no leading/trailing blanks; later lines win). Keys:

| Key | Values | Printed when |
|---|---|---|
| `REMEDY_KIND` | `Disk` `Firmware` `Feature` `Service` `Unclassified` | every exit 11 (inside `Write-HypervisorRemedy`) |
| `WSL_CODE` | `Get-WslFailureCode` result, may be empty | every exit 11 |
| `DISK_ROLE` | `target` (S2–S7) · `registered` (S13 refusal, S14) | every exit 13 / 14 |
| `DISK_PATH` | full path of the file concerned | every exit 13 / 14 |
| `DISK_OWNER_KIND` | `own` `other` `unknown` | every exit 13 |
| `DISK_OWNER` | `DOMAIN\name` from `Get-Acl … .Owner`, may be empty | every exit 13 |

The same remedy words are also put into `$global:EDUBOTICS_REMEDY_PROBLEM_DE` / `$global:EDUBOTICS_REMEDY_NEXTSTEP_DE`
(finalize's default mode passes them into its FAILED marker; same-runspace hand-back as the existing
`$global:EDUBOTICS_WSL_FAILURE_CODE`).

---

## 7. Shared PowerShell helpers (added to `installer/scripts/wsl_distro_state.ps1` by WP-A1)

The file keeps its caller contract (no `exit`, no `throw`, no `$ErrorActionPreference` assignment, Test-Path-guarded
dot-source) and still makes exactly one native call (`wsl --list --quiet`); the new functions use cmdlets only.

| Function | Contract |
|---|---|
| `Get-LxssDistroEntry -DistroName` | `@{ Readable; Found; BasePath; VhdFileName }` from `HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss\*` via the same `Get-ChildItem` + `.GetValue(...)` reads as `Get-LxssDistroNames`; name compared case-insensitively. Unreadable store → `Readable=$false`. |
| `Resolve-LxssDiskPath -BasePath -VhdFileName` (pure) | `""` when either is blank; else `BasePath` with a leading `\\?\UNC\` replaced by `\\`, else a leading `\\?\` removed, trailing `\`/`/` trimmed, `+ '\' + VhdFileName`. |
| `Get-EduBoticsDistroDisk -DistroName` | `@{ Path; State }`, `State` ∈ `Present` `Missing` `Unknown`: path `""` → `Unknown`; `Get-Item -LiteralPath $Path -Force -ErrorAction Stop` succeeds → `Present`; `catch [System.Management.Automation.ItemNotFoundException]` → `Missing`; any other exception → `Unknown`. |
| `Get-EduBoticsDiskOwner -Path` | `@{ Readable; Sid; Name }` from `Get-Acl -LiteralPath $Path -ErrorAction Stop` → `.GetOwner([System.Security.Principal.SecurityIdentifier]).Value` and `.Owner`; any exception → `Readable=$false`. |
| `Get-EduBoticsCurrentUserSid` | `[string]([System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value)`; exception → `""`. **The expression text is a test needle — keep it verbatim.** |
| `Test-EduBoticsElevated` | `[bool]([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)`; exception → `$true` (fail closed). **Same text as finalize's `Elevated:` line, a test needle.** |
| `Resolve-DiskOwnership -CurrentSid -Owner` (pure) | `Own` iff `Owner.Readable`, both SIDs non-empty, equal (ordinal, case-insensitive). `Unknown` iff unreadable, current SID empty, or owner SID is `S-1-5-18` or starts with `S-1-5-32-`. Else `Other`. |
| `Resolve-UnregisterPermission -DiskState -Ownership -Elevated` (pure) | `Allow` iff `DiskState -eq "Missing"`, or `(DiskState -eq "Present" -and Ownership -eq "Own")`, or `(-not $Elevated -and Ownership -ne "Other")`. Else `Refuse`. |

---

## 8. Invariants this change must not break, and their fences

| Invariant | Fence |
|---|---|
| No destructive rebuild without POSITIVE proof (VM up + stamp differs/absent) | `FinalizeEndToEndTest` dead-VM / unresponsive tests (existing, must stay green) |
| „Is it registered?" has three answers, twins agree | `DistroRegistrationExecutedTest` (unchanged) |
| finalize exit codes ↔ GUI mirrors | `FinalizeExitContractTest` (extended by A1 for 13/14; A3 adds `ImportExitContractTest`) |
| The FAILED marker has one writer; UTF-8; `user=`/` distro=` parse | `OneRebootPredicateTest.test_there_is_one_marker_writer`, `PreflightAccountScopeTest`, `test_finalize_marker_twin_lockstep.py` |
| One reboot predicate | `OneRebootPredicateTest` |
| Teardown before the rootfs gate | `PrerequisiteLifecycleTeardownTest` (re-pointed to `_prompt_rebuild_distro`) |
| PS 5.1 traps: BOM, EAP=Continue, native stderr, install guards | `ci.yml::powershell-encoding`, `powershell-native-stderr`, `powershell-install-guards` + `tests/test_ps_guard_scripts.py` |
| Chained scripts end with `exit 0` | `ScriptTerminalExitTest` (A5 adds the new script) |
| German UI | `ci.yml::german-strings-lint` |
| **NEW** No code deletes a `.vhdx`; every `--unregister` is one of the allow-listed guarded sites | `DiskDeletionFenceTest` (A1, tightened by A5) |
| **NEW** The GUI never elevates for a distro change | `GuiNeverElevatesAnImportTest` (A3: AST — `_import_distro_for_this_account` calls neither `_run_privileged` nor `_elevate_and_wait`; `_prompt_finalize_install`'s `ps_args` always contains `-SkipDistroWork` and never `-AllowDestructiveReimport`) |
| **UNFENCED** WSL deletes the disk it created on a failed import; un-elevated `wsl --unregister` cannot delete a foreign disk; the VHDX owner is the importing user's SID | WSL source only (P13) → **S0-1, S0-3, S0-6** |
| **UNFENCED** `[Console]::OutputEncoding` + `exit $LASTEXITCODE` behave under Windows PowerShell 5.1 as under pwsh 7 (P12) | **S0-11** |
| **UNFENCED** `[UninstallRun]` runs before `{app}\scripts` is removed | Inno docs (PLAUSIBLE) → **S0-10** |
| **NEW (A8)** A tag whose commit is only on a `release/*` branch deploys nothing to production (no W1, W2a, W2b, W2, W3) and still builds images, installer and Pi tarball; a tag on `main` behaves exactly as today | `ProductionJobsCannotBeSkippedSilently` (new maintenance arrival + the review mutations), `ReleaseModeIsDecidedByAncestry` (executed, real git), `CiRunsOnReleaseBranches` |
| Production jobs of a `main` release are never skipped, softened or redirected | `ProductionJobsCannotBeSkippedSilently`, `MigrationsAreManual` (existing; must stay green with the maintenance output stated `'false'`) |
| **UNFENCED** A maintenance tag's W4 run re-points the registries' `:latest` at the `release/*` build (`docker-publish.yml` pushes `:latest` on every build) | nothing student-facing pulls `:latest` (CLAUDE.md); §10 trigger |

---

## 9. Work packages

Order: A1 → A2 → A3 → A4 → A5 → A6 → A8 → A7. One commit each. A8 depends on nothing in A1–A6 and may be built in
parallel, but it MUST be on `main` before the `v2.41.1` tag (D-A12). A1–A7 keep `.github/**`, `docker/**`,
`physical_ai_tools/**`, `cloud_training_api/**`, `supabase/**`, `modal_*/**`, `runpod_training/**` and
`wsl_rootfs/**` (except A7's `README.md`) untouched; A8 touches only the `.github/**` and test files it names.
In the „safe-only" outcome of S0-3 (§12 step 4b) the order is A1 → A5 → A6 → A8 → A7 (safe-only).

Test environment notes for every coder:
- Deps-free GUI suite: `cd robotis_ai_setup && python3 -m unittest discover -s tests`. On the owner's Mac `python3`
  is `/opt/miniconda3` 3.13.13 without PyYAML: the baseline is `Ran 3218 … FAILED (errors=10, skipped=92)`, nine
  errors from `ModuleNotFoundError: yaml` and one from `test_supabase_058` walking another plan's gitignored scratch
  directory named `*.py` — both environmental; **no new error or failure may appear**. Prefer a venv with
  `pyyaml` (CI has it).
- Executed PowerShell tests: CI's `python-tests` runs them (pwsh on ubuntu-latest; a missing pwsh FAILS). Locally,
  this machine has the image `review-pwsh-own:local` (pwsh 7.4.7 + Python 3.12.3):
  `docker run --rm -v "$PWD":/repo:ro -w /repo/robotis_ai_setup review-pwsh-own:local python3 -m unittest discover -s tests -p "test_installer_pwsh_executed.py"`
  (baseline: `Ran 57 … OK`, 0 skipped). Report the count and that skips are 0. **CI's run stays the
  authoritative proof**; the local image only shortens the loop.
- Workflow tests (`test_deploy_approval_gate.py`, `test_refuse_rollback.py`, `test_ci_supply_chain_pins.py`) need
  PyYAML: on this machine `/opt/miniconda3/envs/lerobot/bin/python -m unittest discover -s tests -p "<file>"` from
  `robotis_ai_setup/` (baselines 22 / 6 / 21 OK, P23). zizmor 1.25.2 is installed:
  `zizmor --offline --no-progress --min-severity low .github/workflows/` (baseline „No findings", P24; CI runs it
  online as `ci.yml::actions-security`).
- PS guards: `for s in ps_readiness_retry_lint ps_temp_cmdlet_lint ps_cleanup_exit_guard ps_temp_path_normalize_lint ps_native_stderr_lint; do python3 .github/scripts/$s.py || exit 1; done`
- Transliteration grep (from `ci.yml::german-strings-lint`):
  `grep -rnE "(\[FEHLER\]|\[WARNUNG\]|\[STOPP\]).*\b(frueh|kuerzer|schliessen|Zeitluecken|Groesste|Luecken|Moegliche|ueber[a-z]|laeuft|haengt|pruefen|Pruefung|ueberschritten|Schueler|benoetigen|Oberflaeche|uebersprungen|enthaelt|zusaetzliche|Verfuegbar|geaendert|beschaedigt|koennte|faehige|Bildaufloesung)\b" robotis_ai_setup/gui/ robotis_ai_setup/installer/ ; test $? -eq 1`
- New `.ps1` files: UTF-8 **with BOM**, CRLF (`.gitattributes`), `$ErrorActionPreference = "Continue"`, end with `exit 0`.

---

### WP-A1 — The importer never deletes a disk and refuses one it cannot prove is its own

- **summary.** Delete the importer's `Remove-Item … ext4.vhdx`. Make the default import target
  `%LOCALAPPDATA%\EduBotics\wsl` (refuse when `LOCALAPPDATA` is empty; import nothing as SYSTEM). Before any import,
  refuse (exit 13, named, nothing deleted) when a file already sits at the target; treat WSL's `ERROR_FILE_EXISTS`
  the same way. Route every `wsl --unregister` of the EduBotics distro through one guarded function (D-A4). Detect a
  registration whose disk is proven missing: exit 14 without consent, re-set up with `-ReplaceMissingDisk`. Print the
  report lines of §6. Add the helpers of §7 to `wsl_distro_state.ps1`. Let finalize's default mode pass 13/14 through
  with the import's words. Rewrite preflight check 5's advice. Extend the executed harness so every state of §2 rows
  S1–S21 runs, and add the deletion fence.
- **dependsOn:** —
- **layers:** 1 (Windows installer scripts), CI-executed tests
- **files.may:**
  - `robotis_ai_setup/installer/scripts/import_edubotics_wsl.ps1`
  - `robotis_ai_setup/installer/scripts/wsl_distro_state.ps1`
  - `robotis_ai_setup/installer/scripts/finalize_install.ps1` — ONLY: header exit-table lines for 13/14;
    `$EXIT_DISK_NOT_OURS = 13`, `$EXIT_DISK_MISSING = 14` next to the other `$EXIT_*`; one pass-through block
    placed AFTER the existing `if ($importRc -eq $EXIT_VIRT) {` block and written
    `if (($importRc -eq $EXIT_DISK_NOT_OURS) -or ($importRc -eq $EXIT_DISK_MISSING)) {` (not `-eq <digits>`, so
    `test_finalize_consent_branch_actually_exits_the_consent_code`'s first-block regex still finds 12) that calls
    `Fail-WithNextAction` with `$global:EDUBOTICS_REMEDY_PROBLEM_DE` / `_NEXTSTEP_DE` (fallback to
    `"Die EduBotics-Umgebung wurde nicht eingerichtet."` / the existing next-step sentence when empty) and
    `$importRc`; reset both globals to `""` beside the existing `$global:EDUBOTICS_WSL_FAILURE_*` resets.
  - `robotis_ai_setup/installer/scripts/preflight_system.ps1` — ONLY check 5: both `Emit` lines become the two
    `Emit INFO` texts of §5; its comment block loses the „second producer … removal commonly fails" paragraph (false,
    §13 #1) and states the new meaning; add one `Write-Diag "account_scope_user"` line recording whether
    `Join-Path $env:LOCALAPPDATA "EduBotics\wsl\ext4.vhdx"` exists (diagnostic only, try/catch like its neighbours).
  - `robotis_ai_setup/tests/test_installer_pwsh_executed.py`
  - `robotis_ai_setup/tests/test_gui_install_lifecycle.py` — ONLY `PreflightAccountScopeTest`,
    `FinalizeExitContractTest` (additions), the new `DiskDeletionFenceTest`, and docstrings that restate the deleted
    claim.
- **files.mustNot:** `robotis_ai_setup/gui/**`, `robotis_ai_setup/installer/robotis_ai_setup.iss`,
  `robotis_ai_setup/installer/scripts/{install_prerequisites,verify_system,pull_images,configure_*,bind_devices,migrate_from_docker_desktop,virtualization_ready,wsl_docker_ready,uninstall_stop_containers}.ps1`,
  `robotis_ai_setup/tests/test_finalize_marker_twin_lockstep.py`, `VERSION`, `CLAUDE.md`, `docs/**`, `.github/**`.
- **Import script, exact behaviour (in this order):**
  1. `param`: `$InstallRoot` default becomes `""`; add `[switch]$ReplaceMissingDisk`. Keep every other parameter.
  2. Declare the §5 constants for this script (one-line double-quoted assignments near the top, each exactly once).
  3. SYSTEM guard first: `$runsAsSystem = $false; try { $runsAsSystem = [bool]([Security.Principal.WindowsIdentity]::GetCurrent().IsSystem) } catch { $runsAsSystem = $false }`
     (keep the `[Security.Principal.WindowsIdentity]::GetCurrent().IsSystem` text verbatim — a test needle). True →
     `Write-Host` `$SYSTEM_ACCOUNT_SKIP_DE`, `exit 0`.
  4. Existing reboot-flag guard unchanged.
  5. Resolve `$InstallRoot` when `""`: empty `$env:LOCALAPPDATA` → `Write-FAIL $NO_LOCALAPPDATA_DE`, `exit 1`; else
     `$InstallRoot = Join-Path $env:LOCALAPPDATA 'EduBotics\wsl'`. Print `Zielordner: $InstallRoot`.
  6. Rootfs resolution, shipped version, three-way registration: unchanged.
  7. Registered: `$disk = Get-EduBoticsDistroDisk` (only when the helper loaded; else `@{ Path=""; State="Unknown" }`).
     `State -eq "Missing"` → print `$DISK_MISSING_PROBLEM_DE` (`{datei}` = path), the next step, report lines
     (`DISK_ROLE=registered`, `DISK_PATH`), set the two globals; without `-ReplaceMissingDisk` → `exit 14`; with it →
     `Invoke-GuardedUnregister -Reason "missing"` (it re-reads the disk and requires `Missing`); `Done` → continue to
     the import path as for an absent registration; `Refused` → `exit 13`; `Failed` → `exit 1`. The stamp probe is
     NOT run for a missing disk.
     Otherwise the existing stamp logic runs unchanged, except that its single `wsl --unregister` is replaced by
     `Invoke-GuardedUnregister -Reason "rebuild"` (`Refused` → `exit 13`; `Failed` → the existing
     „Konnte existierenden Distro nicht entfernen." + `exit 1`). `-Force` goes through the same gate.
     `-ReplaceMissingDisk` never unregisters a disk that is `Present` or `Unknown`.
  8. `Invoke-GuardedUnregister` (the ONLY `wsl --unregister` in the file): helper missing → `Refused`. Re-read
     `Get-EduBoticsDistroDisk`; ownership = `Resolve-DiskOwnership -CurrentSid (Get-EduBoticsCurrentUserSid) -Owner (Get-EduBoticsDiskOwner $disk.Path)`
     (skip the owner read when `Missing`); `Resolve-UnregisterPermission -DiskState $disk.State -Ownership $own -Elevated (Test-EduBoticsElevated)`;
     `-Reason "missing"` additionally requires `State -eq "Missing"`. `Refuse` → print `$REBUILD_NOT_OURS_PROBLEM_DE`
     (`{datei}` = path, or the registry's `BasePath` when the path is empty), next step, report lines
     (`DISK_ROLE=registered`, `DISK_PATH`, `DISK_OWNER_KIND`, `DISK_OWNER`), set the globals, return `Refused`.
     `Allow` → `wsl --unregister $DistroName *>$null`, return `Done`/`Failed` by `$LASTEXITCODE`.
  9. Import path, after the existing reparse/not-directory checks: probe `$target = Join-Path $InstallRoot 'ext4.vhdx'`
     with `Get-Item -LiteralPath $target -Force -ErrorAction Stop` **inline (not via the helper)**: success →
     `Write-DiskInTheWay` (owner via the helper when loaded, else unknown) → `exit 13`; `ItemNotFoundException` →
     proceed; any other exception → `Write-DiskInTheWay` with unknown owner → `exit 13`.
     `Write-DiskInTheWay` picks `$DISK_OWN_PROBLEM_DE` / `$DISK_OTHER_PROBLEM_DE` (only with a non-empty owner name;
     otherwise the unknown text) / `$DISK_UNKNOWN_PROBLEM_DE`, fills `{ordner}`/`{konto}`, prints problem, next step
     (`$DISK_IN_THE_WAY_NEXTSTEP_DE`) and the report lines (`DISK_ROLE=target`), sets the globals.
  10. 20 GB check and SHA check unchanged; `wsl --import` call unchanged.
  11. Failure branch: the `Remove-Item` block is deleted. If `Get-WslFailureCode` (or, without the helper, a plain
      `-match 'ERROR_FILE_EXISTS'`) yields `ERROR_FILE_EXISTS` → re-probe the target as in step 9 (present → that
      wording; absent → unknown wording with `DISK_PATH` = target) → `exit 13`. Hypervisor class →
      `Write-HypervisorRemedy` (which now also prints `REMEDY_KIND` and `WSL_CODE`) → `exit 11`. Otherwise the
      triad (extended per §5) → `exit 1`. **Pinned lines kept verbatim:** `if ($failClass -eq "hypervisor") { exit 11 }`
      (`test_import_hypervisor_refusal_code_matches_finalize`), a bare `exit 12` line
      (`test_import_consent_refusal_code_matches_finalize`), `& wsl -d $Name --exec /bin/sh -c $STAMP_PROBE_SCRIPT`
      and the three `$STAMP_PROBE_*` assignments (`test_the_installer_and_import_probe_the_stamp_with_the_same_proof`),
      and `exit 0` as the last statement (`ScriptTerminalExitTest`).
  12. Rewrite the header: who may run it (un-elevated per account, elevated by Setup/finalize), the exit table
      0/1/11/12/13/14, the report lines, D-A1…D-A4 in two sentences each. Keep `exit 0` as the last statement.
- **Harness changes in `test_installer_pwsh_executed.py` (fixtures, never weakening an assertion):**
  - `_MOCKS_PS` `Get-Item`: accept `[switch]$Force`, `$null = $PSBoundParameters.Remove('Force')` before the
    forwarded call (CONFIRMED needed, P5).
  - `_MOCKS_PS` new `Get-Acl` function: `$env:MOCK_ACL` = `"<sid>|<name>"` returns an object whose
    `GetOwner([type])` returns `@{Value=<sid>}` and whose `.Owner` is `<name>`; `"throws"` or unset → throw.
  - `_MOCKS_PS` `Get-ChildItem` HKCU mock: an entry may be `Name|BasePath|VhdFileName`; `GetValue('DistributionName')`,
    `GetValue('BasePath')`, `GetValue('VhdFileName')` return the matching field (`""` when absent). Existing
    `"Ubuntu;EduBotics"` shapes keep working.
  - `_FAKE_WSL` `--import <name> <dir> <tar>`: normalise `\` to `/` in `<dir>`; if `<dir>/ext4.vhdx` exists →
    emit `"Die Datei ist bereits vorhanden.\r\nFehlercode: Wsl/Service/RegisterDistro/ERROR_FILE_EXISTS\r\n"`,
    exit 255, create nothing (before any `import_mode` handling); mode `ok` additionally creates
    `<dir>/ext4.vhdx` with `b"NEW-DISK"`; new mode `create_then_fail` creates it then emits the HCS text, exit 255;
    new mode `race` creates it then emits the `ERROR_FILE_EXISTS` text, exit 255. `--import` and `--unregister`
    keep appending to `calls.log`.
  - `_sandbox`: env gains `LOCALAPPDATA` (`<base>/LocalAppData`, created), `MOCK_USER_SID`
    (default `S-1-5-21-1-2-3-1001`), `MOCK_ELEVATED` (default `"1"` — Setup/finalize realism), `MOCK_IS_SYSTEM`
    (default `"0"`); new optional args `acl`, `lxss` (already exists), `install_root`. Needle substitutions on the
    COPIED scripts, each asserted found: in `wsl_distro_state.ps1` the `Get-EduBoticsCurrentUserSid` expression →
    `$env:MOCK_USER_SID`, the `Test-EduBoticsElevated` expression → `([string]$env:MOCK_ELEVATED -eq "1")`; in
    `import_edubotics_wsl.ps1` the IsSystem expression → `([string]$env:MOCK_IS_SYSTEM -eq "1")`.
  - Existing tests whose consent rebuild must still unregister —
    `test_a_stampless_distro_still_gets_its_one_final_reimport` and
    `test_a_readable_different_stamp_still_needs_consent_and_gets_it` — get an OWN disk fixture
    (`lxss="EduBotics|<base>/LocalAppData/EduBotics/wsl|ext4.vhdx"`, the file created, `acl` = the user SID);
    their assertions are unchanged.
- **The failing tests (each must fail on `66382d27` and pass after):**
  - `DiskSafetyExecutedTest.test_a_disk_at_the_target_is_never_imported_over_or_deleted` — subTests own / other
    (`S-1-5-21-1-2-3-1002|PC01\Lehrer`) / unknown (`throws`) / group (`S-1-5-32-544|VORDEFINIERT\Administratoren`),
    each through (a) `import_edubotics_wsl.ps1 -AllowDestructiveReimport` and (b) finalize default mode, the file
    `<LOCALAPPDATA>/EduBotics/wsl/ext4.vhdx` = `b"OTHER-ACCOUNTS-DATASETS"`, not registered. Assert: rc 13; no
    `--import` in `calls.log`; file bytes unchanged; output contains the expected PROBLEM constant with `{ordner}`
    filled and `EDUBOTICS_REPORT_DISK_OWNER_KIND=own|other|unknown|unknown`; for (b) the marker is
    `FAILED <iso>\n<problem>\n<next step>` and rc 13. **Today:** the file is deleted (P4 shows the mechanism).
  - `test_sequence_a_leaves_the_shared_programdata_disk_alone` — foreign bytes at
    `<ProgramData>/EduBotics/wsl/ext4.vhdx`, not registered, finalize default mode, `import_mode="ok"`,
    `images=True`: rc 0; `--import EduBotics <LOCALAPPDATA>…EduBotics\wsl …` in calls; ProgramData bytes unchanged.
    **Today:** deleted, rc 1 (P4-A, CONFIRMED).
  - `test_an_explicit_shared_install_root_refuses_too` — same foreign file, import with
    `-InstallRoot <ProgramData>\EduBotics\wsl`: rc 13, bytes unchanged. **Today:** deleted (P4-B mechanism).
  - `test_wsl_refusing_an_existing_file_is_named_not_deleted` — target empty, `import_mode="race"`: rc 13, file kept
    (`b"NEW-DISK"`). **Today:** deleted.
  - `test_a_failed_import_deletes_nothing` — `import_mode="create_then_fail"`: rc 11, file exists. **Today:** deleted.
  - `test_the_rebuild_gate` — registered, `stamp="6"`, `-AllowDestructiveReimport`, own-disk fixture varied:
    Present×{own, other, throws, group}×{elevated 1, 0} and disk-path-unknown (no `lxss`)×{1, 0}, plus one `-Force`
    cell: `--unregister EduBotics` called exactly for the `Allow` cells of §7's rule; refused cells rc 13 with
    `$REBUILD_NOT_OURS_PROBLEM_DE` and `DISK_ROLE=registered`. **Today:** every cell unregisters.
  - `test_a_missing_disk_is_named_and_rebuilt_only_with_consent` — registered, `lxss` pointing to
    `<LOCALAPPDATA>/old/ext4.vhdx` (absent), `vm_dead` = `"Fehlercode: Wsl/Service/CreateInstance/MountDisk/HCS/ERROR_FILE_NOT_FOUND\r\n"`:
    without the switch rc 14, no `--unregister`, no `--import`, `DISK_PATH=` that path, `$DISK_MISSING_PROBLEM_DE`
    filled; with `-ReplaceMissingDisk` (pass it through the wrapper like `-Destructive`) rc 0 and calls contain
    `--unregister EduBotics` before `--import EduBotics <LOCALAPPDATA>…`; with the switch and the file PRESENT
    (VM dead) → no `--unregister`, rc 11. **Today:** rc 11 in every cell (P6 shows a non-advanced script
    silently swallows an unknown switch).
  - `test_system_account_imports_nothing` — `MOCK_IS_SYSTEM=1`: rc 0, `calls.log` absent/empty, output contains
    `$SYSTEM_ACCOUNT_SKIP_DE`. **Today:** imports.
  - `test_no_localappdata_refuses` — `LOCALAPPDATA=""`: rc 1, no `--import`, `$NO_LOCALAPPDATA_DE` shown.
  - `test_the_hypervisor_report_carries_kind_and_code` — `import_mode="hcs"`, `hv="null"`:
    `EDUBOTICS_REPORT_REMEDY_KIND=Service` and `EDUBOTICS_REPORT_WSL_CODE=HCS_E_SERVICE_NOT_AVAILABLE` in stdout.
    **Today:** absent.
  - `test_a_reparse_point_target_is_refused` — `install_root` a symlinked directory (pwsh reports `ReparsePoint`,
    CONFIRMED P10): rc 1, no `--import`. (Coverage of existing code; passes once the mock accepts `-Force`.)
  - `DistroDiskHelperExecutedTest` — drives `Resolve-LxssDiskPath`, `Resolve-DiskOwnership` and
    `Resolve-UnregisterPermission` over literal tables written in the test (all 3×4×2 permission cells) and compares
    with a Python oracle in the test file.
  - `DiskDeletionFenceTest` (in `test_gui_install_lifecycle.py`, deps-free) — (1) comment-stripped (`self._code`
    style) `installer/scripts/*.ps1`, `gui/app/scripts/*.ps1` and `installer/robotis_ai_setup.iss` contain no
    `Remove-Item|DeleteFile|del |erase ` on a line that also contains `vhdx` (case-insensitive); (2) every
    `--unregister` outside comments is in this allowlist: `import_edubotics_wsl.ps1` exactly once, inside the
    body of `function Invoke-GuardedUnregister`; `migrate_from_docker_desktop.ps1` inside its `docker-desktop` loop;
    `robotis_ai_setup.iss` `[UninstallRun]` exactly once — **marked „removed by WP-A5"**; (3) no `gui/app/**/*.py`
    has an `ast.Constant` string equal to `--unregister`. Zero-file floor. **Today:** (1) fails on
    `import_edubotics_wsl.ps1`.
  - Updates: `PreflightAccountScopeTest.test_the_vhdx_path_matches_the_importers_default_install_root` → asserts the
    importer's default leaf (`Join-Path $env:LOCALAPPDATA 'EduBotics\wsl'`) and the leaf preflight probes under
    ProgramData (`"EduBotics\wsl\ext4.vhdx"`) share `EduBotics\wsl`; `test_the_per_account_verdict_requires_a_differing_marker_user`
    → both branches `Emit INFO`, the split branch names `$markerUser`, the else branch names „anderen Windows-Konto"
    and „früheren Einrichtung", both contain „bleibt unverändert", neither contains „Installer" nor „erneut ausführen";
    the class docstring drops the Remove-Item producer. `FinalizeExitContractTest` gains
    `test_import_disk_refusal_codes_pass_through_finalize` (import has literal `exit 13` and `exit 14` lines; finalize
    declares 13/14; the pass-through condition and its `Fail-WithNextAction … $importRc`) and its
    `test_failed_code_is_distinct_from_the_routed_ones` includes them.
- **testCommands:**
  - `docker run --rm -v "$PWD":/repo:ro -w /repo/robotis_ai_setup review-pwsh-own:local python3 -m unittest discover -s tests -p "test_installer_pwsh_executed.py"` (or CI)
  - `cd robotis_ai_setup && python3 -m unittest discover -s tests -p "test_gui_install_lifecycle.py"`
  - `cd robotis_ai_setup && python3 -m unittest discover -s tests -p "test_finalize_marker_twin_lockstep.py"`
  - `cd robotis_ai_setup && python3 -m unittest discover -s tests -p "test_ps_guard_scripts.py"`
  - the PS guard loop and the transliteration grep (§9 notes)
  - `cd robotis_ai_setup && python3 -m unittest discover -s tests` (no new error/failure vs. baseline)
- **acceptance:**
  - `grep -rniE "Remove-Item.*vhdx" robotis_ai_setup/installer robotis_ai_setup/gui` returns nothing.
  - `import_edubotics_wsl.ps1` contains exactly one `wsl --unregister`, inside `function Invoke-GuardedUnregister`.
  - Executed suite: all new tests pass, 0 skipped, total = 57 + the new tests.
  - Every pre-existing test of the executed file passes with at most the two fixture changes named above.
  - `finalize_install.ps1` diff touches only the lines listed in `files.may`.
  - Each §5 constant of this WP is declared exactly once in `import_edubotics_wsl.ps1`.
- **risk:** high — safety of student data, the installer, an exit-code contract, executed tests that run only in CI.
- **lenses:** correctness, delivery, regression

---

### WP-A2 — finalize's skip mode: the elevated part without touching a distro

- **summary.** Add `[switch]$SkipDistroWork` to `finalize_install.ps1`. In that mode, after the existing Phase 0 and
  the reboot verdict (unchanged, including the restart record), print the skip-mode line of §5, clear the flag with
  the same code as the full-success path, write the SUCCESS marker with the same writer, and exit 0 — no
  registration check, no import, no pull. `-AllowDestructiveReimport` is ignored in skip mode with one transcript
  line. Default mode is unchanged. Factor the flag clear and the SUCCESS marker write into one function each, used
  by both modes.
- **dependsOn:** A1
- **layers:** 1
- **files.may:** `robotis_ai_setup/installer/scripts/finalize_install.ps1`,
  `robotis_ai_setup/tests/test_installer_pwsh_executed.py`, `robotis_ai_setup/tests/test_gui_install_lifecycle.py`
  (only new tests for the skip branch's source order and the single SUCCESS writer).
- **files.mustNot:** everything else; in particular `gui/**`, `import_edubotics_wsl.ps1`, `.iss`.
- **The failing test:** `FinalizeEndToEndTest.test_skip_mode_never_touches_a_distro` — sandbox
  `flag="1", flag_age=7200, boot_age=600, hv="true", vfe="true", import_mode="ok", images=True`, the wrapper passes
  `-SkipDistroWork` (add a `[switch]$Skip` to `_WRAPPER_PS` mapping to `SkipDistroWork`): rc 0; `calls.log` ⊆
  `{"--status", "--version"}` (no `--list`, `--import`, `--unregister`, `-d …`); flag removed; marker starts
  `SUCCESS ` and carries `user=` and ` distro=`; transcript contains the skip-mode line. **Today: CONFIRMED failing
  (P6)** — today's finalize silently accepts the unknown switch and runs `--list --quiet`, `--import …`,
  `-d EduBotics -- cat …`, `docker image inspect …`.
- **More tests:** skip mode keeps the verdict first — `flag="1", flag_age=7, boot_age=1800, hv="false"` → rc 10,
  calls `{--status, --version}`, flag untouched (copy of `test_the_field_state_asks_for_a_reboot_before_touching_wsl`
  with the switch); skip mode + `dd-uninstall` custody → rc 10 and the flag restored with its original mtime;
  skip mode + `Test-RestartDidNotHelp` → rc 11 with `$RESTART_DID_NOT_HELP_*` in the marker; skip mode +
  `-AllowDestructiveReimport` → rc 0, no `--unregister`. Static (`test_gui_install_lifecycle.py`): the skip branch
  is after `$virtVerdict = Get-VirtualizationVerdict` and before the first `Get-EduBoticsDistroRegistration` call in
  the body; `Value ("SUCCESS {0}` appears exactly once.
- **testCommands:** the executed suite (container or CI); `test_gui_install_lifecycle.py`; PS guard loop.
- **acceptance:** every pre-existing `FinalizeEndToEndTest`/`RestartRecordExecutedTest`/`PrerequisitesExecutedTest`
  test passes unchanged; the skip branch exits through the same flag-clear and marker functions as the full-success
  path; header documents skip-mode exit 0 = „the elevated part is done; the distro is the GUI's".
- **risk:** high — exit-code contract and the `.reboot_required` lifecycle.
- **lenses:** correctness, delivery, regression

---

### WP-A3 — The GUI sets up its own account's environment, without admin

- **summary.** The GUI imports an absent distro for the logged-in account by running `import_edubotics_wsl.ps1`
  un-elevated, captures its output into `diagnostics_dir()\edubotics_import.log` (previous one rotated to
  `.prev.log`), shows the import's own remedy (report lines) and continues the normal checks on success. finalize is
  always called with `-SkipDistroWork`; its exit 0 continues into the account's own import; its exit 12 and an
  import's exit 12 open the GUI's own rebuild consent, whose „Ja" runs the import with `-AllowDestructiveReimport`
  (no UAC). Remove every „Installer erneut ausführen, während Sie mit Ihrem eigenen Konto angemeldet sind" advice.
  `verify_system.ps1` stops calling an absent distro a failed install.
- **dependsOn:** A1, A2
- **layers:** 2 (Windows GUI), 1 (one script: `verify_system.ps1`)
- **files.may:**
  - `robotis_ai_setup/gui/app/gui_app.py`
  - `robotis_ai_setup/gui/app/device_manager.py` — ONLY the `wsl_distro_missing` `message_de` in
    `diagnose_usb_environment` and the docstring line that mentions the installer for it
  - `robotis_ai_setup/installer/scripts/verify_system.ps1` — ONLY the „2. EduBotics distro registered" block
  - `robotis_ai_setup/tests/test_gui_install_lifecycle.py`
  - `robotis_ai_setup/tests/test_gui_distro_import.py` (new)
  - `robotis_ai_setup/tests/test_finalize_marker_twin_lockstep.py` — ONLY its module docstring's description of
    what `_marker_user` decides
  - `robotis_ai_setup/tests/test_installer_pwsh_executed.py` — ONLY a new `VerifyExecutedTest`
  - `robotis_ai_setup/tests/test_shutdown_teardown.py` — ONLY the docstring of
    `test_the_dialog_defaults_to_NOT_scanning` (it names `_prompt_finalize_install`'s data-loss dialog, which moves
    to `_prompt_rebuild_distro`)
- **files.mustNot:** `wsl_bridge.py`, `docker_manager.py`, `constants.py` (A4/A6), `import_edubotics_wsl.ps1`,
  `finalize_install.ps1`, `.iss`, `.github/**`.
- **GUI changes, exact:**
  1. Module constants: `IMPORT_EXIT_DONE = 0`, `IMPORT_EXIT_VIRT = 11`, `IMPORT_EXIT_CONSENT = 12`,
     `IMPORT_EXIT_DISK_NOT_OURS = 13`, `IMPORT_EXIT_DISK_MISSING = 14`; `IMPORT_REPORT_PREFIX = "EDUBOTICS_REPORT_"`;
     the §5 GUI constants of this WP (twins byte-equal to `import_edubotics_wsl.ps1` for the `DISK_*` except
     `DISK_MISSING_*`, and `REBUILD_NOT_OURS_*`; `VIRT_FIRMWARE_*` byte-equal to finalize).
  2. Module functions (pure, unit-tested): `_import_command(script, *, allow_destructive=False, replace_missing=False) -> list[str]`
     returning `[<SystemRoot>\System32\WindowsPowerShell\v1.0\powershell.exe (or "powershell.exe" when SystemRoot is unset), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", W]`
     with `W = "try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }; $global:LASTEXITCODE = 1; try { & '<_ps_single_quote(script)>'<switches> } catch { $global:LASTEXITCODE = 1 }; exit $LASTEXITCODE"`
     (CONFIRMED shape under pwsh, P12); `_parse_import_report(text) -> dict` (keys lower-case without the prefix,
     later wins, ignores everything else, CRLF-safe); `_import_outcome(rc, report, output) -> tuple(kind, problem, next_step, title)`:
     0 → `("done", None, None, None)`; 11 → kind from `report["remedy_kind"]` mapped to the GUI pairs
     (Disk/Firmware/Feature/Service/Unclassified), else `_distro_start_remedy_kind(*wsl_bridge.classify_wsl_failure(output))`;
     code from `report["wsl_code"]` else the classifier; the problem gets `(Fehlercode: X).` exactly like
     `_distro_start_remedy`; title `VM_START_DIALOG_TITLE_DE`; 12 → `("consent", …)`; 13 → `disk_role == "registered"`
     → `REBUILD_NOT_OURS_*` with `{datei}`; else owner kind own/other(with non-empty `disk_owner`)/other-without-name
     → unknown wording, `{ordner}` = the parent directory of `disk_path`; title `DISTRO_SETUP_DIALOG_TITLE_DE`;
     14 → `("disk_missing", IMPORT_FAILED_PROBLEM_DE, IMPORT_FAILED_NEXTSTEP_DE, DISTRO_SETUP_DIALOG_TITLE_DE)`
     (A4 refines); `None` → not-started pair; anything else → failed pair.
  3. `_resolve_import_script()` — same two candidates as `_resolve_finalize_script`, file `import_edubotics_wsl.ps1`.
  4. `_import_distro_for_this_account(self, *, allow_destructive=False, replace_missing=False)` — re-entrancy flag
     `_import_in_progress` (set before the thread, cleared in a `finally` after all post-processing); missing script
     → `messagebox.showerror("EduBotics-Umgebung fehlt", "Das Einrichtungsskript wurde nicht gefunden. Bitte den EduBotics-Installer erneut ausführen.")`;
     logs `IMPORT_STARTED_DE`, and `LEGACY_DISK_INFO_DE` when `os.stat(os.path.join(os.environ.get("PROGRAMDATA",""), "EduBotics", "wsl", "ext4.vhdx"))`
     succeeds; on a daemon thread: `docker_manager.stop_keepalive()` first when `allow_destructive or replace_missing`;
     `subprocess.run(_import_command(...), capture_output=True, encoding="utf-8", errors="replace", creationflags=0x08000000)`
     **with no timeout**; `FileNotFoundError`/`OSError` → rc `None`; write stdout+stderr to the log path (rotate the
     previous like `_finalize_worker` does), then `_transcript_excerpt` of it into the Protokoll under
     `── Einrichtungs-Protokoll ──`; route by `_import_outcome`: `done` → `self._import_completed = True`,
     `_last_setup_outcome = "done"`, re-run `_run_prerequisite_checks` on a thread; `consent` →
     `self.root.after(0, self._prompt_rebuild_distro)`; every other kind → `_last_setup_outcome` = kind
     (`virt`, `disk_blocked`, `disk_missing`, `failed`), `_last_setup_detail = (problem, next_step)`, log
     `[FEHLER] {problem} {next_step}`, status, `messagebox.showwarning(title, f"{problem}\n\n{next_step}")` on the Tk
     thread. Must not call `_run_privileged` or `_elevate_and_wait`.
  5. `_prompt_rebuild_distro(self)` — refuses silently while `_import_in_progress`; `messagebox.askyesno("EduBotics-Umgebung neu aufbauen", REBUILD_PROMPT_DE, default=messagebox.NO)`;
     Ja → `_import_distro_for_this_account(allow_destructive=True)`; Nein → `_last_setup_outcome = "consent"`,
     status „EduBotics-Umgebung nicht eingerichtet", log „Einrichtung vom Benutzer verschoben." (existing text).
  6. `_run_prerequisite_checks_body`: absent branch → if `getattr(self, "_import_completed", False)` → log
     `IMPORT_SUCCESS_BUT_MISSING_DE`, status, stop; elif `not wsl_bridge.is_wsl_available()` → `_prompt_finalize_install`
     (as today); else `self.root.after(0, self._import_distro_for_this_account)`. The rootfs gate calls
     `self._prompt_rebuild_distro` (it stays after `ensure_environment_stopped()`). Everything else unchanged.
  7. `_prompt_finalize_install(self)` — the `reason` parameter, the rootfs dialog and `allow_destructive` are
     removed; `ps_args` always ends with ` -SkipDistroWork`. Exit routing: 10 and 11 unchanged; 12 →
     `self.root.after(0, self._prompt_rebuild_distro)` (no „Installer" text); 0 → latch `_finalize_completed`,
     `_last_setup_outcome = "done"`, if `_marker_user()` is non-empty and differs casefolded from `%USERNAME%` log
     `ACCOUNT_INFO_DE` (`{admin}`, `{konto}`), then re-run `_run_prerequisite_checks` — **regardless of
     `is_distro_registered()`** (the old contradiction branch and its per-account remedy text are deleted); any other
     non-zero exit → the existing „Einrichtung fehlgeschlagen (exit N). Siehe Protokoll oben." plus, when
     `_read_failed_marker(marker_file)` returns a pair, `[FEHLER] {problem} {next_step}` and
     `_last_setup_detail`. Launch failure / cancel branches unchanged. `_marker_user` stays a nested closure with its
     existing parse (lockstep test unchanged).
  8. `_setup_unfinished_de`: outcomes `virt`, `disk_blocked`, `disk_missing` and `failed` with a detail →
     „Die Einrichtung ist nicht abgeschlossen: {p} {n}"; `consent` → the §5 text; others unchanged.
  9. `_scan_blocked_reason`: refuse with „Die Einrichtung läuft gerade — „Arme scannen“ ist erst danach möglich."
     also while `_import_in_progress`. `_scan_arms`' override tuple gains `"disk_blocked"`, `"disk_missing"`.
  10. `__init__`: `self._import_completed = False`, `self._import_in_progress = False` beside `_finalize_completed`.
  11. `_show_arm_repair`: the `wsl_distro_missing` button becomes text „EduBotics-Umgebung einrichten" with
      `command=self._import_distro_for_this_account` (no „(Administrator)"; it needs no admin); its docstring line
      changes to „wsl_distro_missing → the account's own import, un-elevated". Test: a new
      `ArmRepairButtonTest` asserts (AST over `_show_arm_repair`) that the `wsl_distro_missing` branch's button
      command is `self._import_distro_for_this_account` and no string in that branch contains „Administrator".
  12. Thread and logging rules the existing suites enforce: `_run_prerequisite_checks_body` runs on worker threads
      and must contain no `messagebox.*` call (`test_shutdown_teardown.py::TheDialogIsOnlyOnThePathThatEndsASession`
      walks its AST) — it only schedules `self._import_distro_for_this_account` / `self._prompt_rebuild_distro`
      through `self.root.after(0, …)`; every dialog is raised on the Tk thread via `root.after`; every `self._log`
      call takes ONE positional string with an inline `[FEHLER]`/`[WARNUNG]`/`[INFO]` marker, never a keyword
      (`test_gui_log_levels.py` extracts these methods against a bare `list.append`).
- **verify_system.ps1:** an `Absent` registration (not `Unresponsive`) → `Write-WARN` with the §5 sentence, no
  `$allOk = $false`; `Unresponsive` unchanged; the two-way fallback (helper missing) unchanged.
- **The failing test:** `PrerequisiteRegistrationRoutingTest.test_an_absent_distro_is_imported_for_this_account_without_elevation`
  (replaces `test_an_absent_distro_still_routes_into_finalize`): `_make(registration="absent")` with the namespace's
  `wsl_bridge` stub answering `is_wsl_available() → True` and the owner's `_import_distro_for_this_account` appending
  `"import"`: `events == ["import"]`. **Today: CONFIRMED failing (P7)** — events `['finalize']`.
- **More tests (all in the named files):**
  - routing: `…_without_a_working_wsl_still_routes_into_finalize` (`is_wsl_available → False` → `["finalize"]`);
    `…_after_a_reported_import_success_does_not_loop` (`_import_completed=True` → no event, the §5 sentence logged).
  - `PromptFinalizeInstallTest`: `test_default_path_auto_runs_without_dialog` also asserts `ps_args` ends with
    `-SkipDistroWork` and has no `-AllowDestructiveReimport`; the three `rootfs_mismatch` tests move to a new
    `RebuildPromptTest` (Ja → runner called with `allow_destructive=True`; Nein → not called, outcome `consent`,
    „verschoben" logged; askyesno default Nein); `test_exit12_with_flag_set_reports_consent_remedy` → asserts
    `_prompt_rebuild_distro` scheduled and no log line contains „Installer"; `test_exit0_but_distro_missing_reports_the_contradiction_honestly`
    → replaced by `test_exit0_continues_into_the_accounts_own_import` (re-run scheduled whether registered or not;
    no „fehlt weiterhin", no „Installer"); the marker tests (`test_marker_user_*`, `test_account_names_are_compared_case_insensitively`,
    `test_a_username_with_spaces_survives_the_distro_suffix`, `test_an_umlaut_username_round_trips_and_is_named_verbatim`,
    `test_the_same_umlaut_account_is_not_reported_as_a_mismatch`, `test_an_ansi_written_marker_falls_back_instead_of_accusing`,
    `test_missing_marker_keeps_the_old_message`, `test_an_unreadable_marker_path_cannot_raise`,
    `test_garbage_marker_keeps_the_old_message_without_raising`, `test_an_unreadable_username_env_keeps_the_old_message`,
    `test_the_per_account_branch_never_fires_on_the_success_path`) keep their inputs and their property; only the
    asserted output becomes „`ACCOUNT_INFO_DE` logged with the parsed name" (differing) or „no account line"
    (same / unparseable / missing); `test_failed_exit_with_flag_clear_reports_fehlgeschlagen` keeps its assertion and
    gains one with a FAILED marker present → its problem logged.
  - `PrerequisiteLifecycleTeardownTest`: the owner stub records `"rebuild_prompt"` for `_prompt_rebuild_distro`;
    `test_teardown_precedes_the_rootfs_gate` asserts `events[-1] == "rebuild_prompt"`.
  - `ScanRefusalAndSetupContextTest.test_the_refusal_carries_the_outcome`: `consent` → must contain „dem Neuaufbau
    zustimmen", must not contain „Installer"; new cells `disk_blocked` (with a detail) and
    `test_a_running_import_blocks`.
  - `TranscriptExcerptTest.test_all_three_call_sites_use_the_shared_excerpt` → four call sites (the import runner).
  - `OneRebootPredicateTest.test_the_gui_remedies_are_finalizes_words`: `_GUI_REMEDY_PAIRS` gains `FIRMWARE`.
  - New `ImportRemedyTwinTest` (lifecycle file): every GUI `DISK_OTHER/OWN/UNKNOWN_PROBLEM_DE`,
    `DISK_IN_THE_WAY_NEXTSTEP_DE`, `REBUILD_NOT_OURS_PROBLEM_DE`, `REBUILD_NOT_OURS_NEXTSTEP_DE` equals the
    `^\$NAME\s*=\s*"([^"]*)"` value in `import_edubotics_wsl.ps1`.
  - New `ImportExitContractTest`: `IMPORT_EXIT_*` values pinned (0/11/12/13/14) and each non-zero one appears as a
    literal `exit N` line in `import_edubotics_wsl.ps1` (11 in its existing `{ exit 11 }` form).
  - New `GuiNeverElevatesAnImportTest` (see §8).
  - New `DeviceManagerMissingDistroSentenceTest`: the AST of `diagnose_usb_environment` holds the §5 sentence for
    `wsl_distro_missing` and no string there contains „Installer".
  - `tests/test_gui_distro_import.py`: `_import_command` (apostrophe path doubled — CONFIRMED shape P12, both
    switches, `$global:LASTEXITCODE = 1` preset, ends `exit $LASTEXITCODE`, System32 path when `SystemRoot` is set);
    `_parse_import_report` (CRLF, last wins, junk ignored, a value with `=` kept whole); `_import_outcome` for 0,
    11×{each kind, no kind line → classifier, code appended}, 12, 13×{target own/other/other-without-name/unknown,
    registered}, 14, `None`, 1; the runner with a fake `subprocess.run` (log file written and rotated, excerpt
    logged, outcome+detail set, dialog on the Tk thread, re-run on 0, rebuild prompt on 12, `stop_keepalive` on the
    destructive and repair paths only, `_import_in_progress` cleared after an exception).
  - `VerifyExecutedTest.test_an_account_without_a_distro_is_not_a_failed_install` (executed): sandbox not registered,
    images absent → `verify_system.ps1` rc 0 and stdout contains the §5 sentence.
- **testCommands:** `cd robotis_ai_setup && python3 -m unittest discover -s tests -p "test_gui_install_lifecycle.py"`;
  `… -p "test_gui_distro_import.py"`; `… -p "test_finalize_marker_twin_lockstep.py"`; the executed suite; the
  transliteration grep; the whole deps-free suite.
- **acceptance:** no `gui/app/*.py` string literal contains „Installer erneut ausführen, während"; `_run_prerequisite_checks_body`
  never routes an absent distro into `_prompt_finalize_install` while `is_wsl_available()` is True; every
  `_prompt_finalize_install` launch carries `-SkipDistroWork`; the import runner has no timeout and never elevates.
- **risk:** high — the GUI's setup flow, several pinned contracts, two layers.
- **lenses:** correctness, delivery, regression

---

### WP-A4 — A missing disk and WSL's own system disk are named, and the missing one can be re-set up

- **summary.** Give `wsl_bridge` the disk-path twin of `Resolve-LxssDiskPath` and a proof-only existence probe.
  When a registered distro does not start, the GUI first asks whether its registered disk is proven missing and, if
  so, names the file and offers „Jetzt neu einrichten?" (default Nein), whose „Ja" runs the account's import with
  `-ReplaceMissingDisk`. A start failure that names WSL's own `system.vhd` gets its own sentence. The Disk remedy
  stops hard-coding `ProgramData` (finalize + GUI twin).
- **dependsOn:** A1, A3
- **layers:** 2, 1 (one finalize constant)
- **files.may:** `robotis_ai_setup/gui/app/wsl_bridge.py`, `robotis_ai_setup/gui/app/gui_app.py`,
  `robotis_ai_setup/installer/scripts/finalize_install.ps1` (ONLY `$VIRT_DISK_NEXTSTEP_DE`),
  `robotis_ai_setup/tests/test_gui_install_lifecycle.py`, `robotis_ai_setup/tests/test_gui_distro_import.py`,
  `robotis_ai_setup/tests/test_installer_pwsh_executed.py` (ONLY a `DiskPathTwinExecutedTest`).
- **files.mustNot:** `import_edubotics_wsl.ps1`, `wsl_distro_state.ps1`, `.iss`, `docker_manager.py`, `.github/**`.
- **Changes:**
  - `wsl_bridge.py`: `lxss_disk_path(base_path, vhd_file_name) -> str` (pure twin of §7, returns `""` like the PS);
    `_lxss_distro_entry(distro) -> tuple[bool, str, str]` (winreg, same enumeration as `_lxss_distro_names`,
    `QueryValueEx` of `BasePath` and `VhdFileName`, case-insensitive name match); `distro_disk_path() -> str`;
    `disk_state(path) -> str` (`""` → `"unknown"`; `os.stat` ok → `"present"`; `FileNotFoundError` → `"missing"`;
    any other `OSError` → `"unknown"` — never `os.path.exists`, which hides PermissionError);
    `distro_disk_state() -> tuple[str, str]`.
  - `gui_app.py`: `DISK_MISSING_PROBLEM_DE`/`_NEXTSTEP_DE` (twins of the import's), `DISK_MISSING_DIALOG_TITLE_DE`,
    `DISK_MISSING_QUESTION_DE`, `WSL_SYSTEM_DISK_PROBLEM_DE`/`_NEXTSTEP_DE`; `VIRT_DISK_NEXTSTEP_DE` reworded (§5).
    In `_run_prerequisite_checks_body`, at BOTH places that today call `_report_distro_cannot_start` (the immediate
    classified miss and the post-wait miss), call `wsl_bridge.distro_disk_state()` first; `"missing"` →
    `self._report_distro_disk_missing(path)` and return. `_report_distro_disk_missing(self, path)` (called on the
    worker thread) → `_last_setup_outcome = "disk_missing"`, detail = the filled pair, log `[FEHLER] …`, status
    „EduBotics-Umgebung fehlt — neu einrichten", then `self.root.after(0, <closure>)` whose closure (Tk thread) runs
    `askyesno(DISK_MISSING_DIALOG_TITLE_DE, f"{p}\n\n{n}\n\n{DISK_MISSING_QUESTION_DE}", default=messagebox.NO)`
    and on Ja calls `self._import_distro_for_this_account(replace_missing=True)`. `_report_distro_cannot_start`:
    when `re.search(r"\\system\.vhd\b", output_without_nuls, re.I)` → the WSL-system-disk pair with the code appended
    like `_distro_start_remedy` (a file NAME is not localized; comment says so), else unchanged.
    `_import_outcome` 14 → `("disk_missing", DISK_MISSING_* filled with report["disk_path"], …)` and the runner
    routes it to `_report_distro_disk_missing`.
- **The failing test:** `DistroStartProbeTest.test_a_missing_disk_is_named_and_offers_the_rebuild` — `_make(probes=[(False, "Fehler beim Anfügen des Datenträgers \"C:\\Users\\S\\AppData\\Local\\EduBotics\\wsl\\ext4.vhdx\" an WSL2: …\r\nFehlercode: Wsl/Service/CreateInstance/MountDisk/HCS/ERROR_FILE_NOT_FOUND\r\n")])`
  with a namespace `wsl_bridge` whose `distro_disk_state()` returns `("missing", "C:\\Users\\S\\AppData\\Local\\EduBotics\\wsl\\ext4.vhdx")`
  (classifier functions delegated to the real module): `_report_distro_disk_missing` called with that path,
  `_report_distro_cannot_start` not called, `_last_setup_outcome == "disk_missing"`, the detail's problem contains
  the path and „fehlt". **Today: CONFIRMED failing (P8)** — outcome `virt`, Disk remedy, no path.
- **More tests:** `test_a_present_disk_keeps_the_disk_words` and `test_an_unknown_disk_state_keeps_the_disk_words`
  (existing Disk wording, now with the reworded next step); `test_wsl_system_disk_failure_is_named` with research's
  24H2 text naming `C:\Program Files\WSL\system.vhd` (`MountVhd/HCS/0x80070032`) → the WSL-system pair with
  `(Fehlercode: 0x80070032).`; the repair dialog (Ja → runner `replace_missing=True`; Nein → nothing);
  `WslDistroDiskTest` for `disk_state` on a temp file / a missing file / a path below a regular file (`unknown`) /
  `""`; `DiskPathTwinExecutedTest` (executed) — `Resolve-LxssDiskPath` vs `wsl_bridge.lxss_disk_path` over
  `C:\a\b`, `C:\a\b\`, `\\?\C:\a\b`, `\\?\UNC\srv\sh\a`, `C:\a` + `""`, `""` + `ext4.vhdx`, a path with `ü` —
  identical results; `ImportRemedyTwinTest` gains `DISK_MISSING_*`; `test_the_gui_remedies_are_finalizes_words`
  passes with the reworded `VIRT_DISK_NEXTSTEP_DE` on both sides.
- **testCommands:** as WP-A3, plus the executed suite.
- **acceptance:** the reworded `VIRT_DISK_NEXTSTEP_DE` is byte-equal in finalize and GUI; no GUI path calls
  `os.path.exists` for the disk decision; the repair is only reachable through the askyesno „Ja".
- **risk:** high — offers an action that ends in `wsl --unregister` (guarded by A1), two layers.
- **lenses:** correctness, delivery, regression

---

### WP-A5 — The uninstaller unregisters only a disk it can prove is this account's

- **summary.** Replace `.iss` `[UninstallRun]`'s direct `wsl.exe --unregister EduBotics` with
  `powershell.exe … -File "{app}\scripts\uninstall_unregister_distro.ps1"` (same `RunOnceId`, same
  `Check: ConfirmDistroWipe`, `runhidden`). The new script dot-sources `wsl_distro_state.ps1` (missing → log and
  unregister nothing), asks the three-way registration (only `Registered` proceeds), reads the registered disk and
  its ownership, applies `Resolve-UnregisterPermission` with `-Elevated (Test-EduBoticsElevated)`, runs
  `wsl --unregister EduBotics` only on `Allow`, prints the §5 lines, appends them to
  `%ProgramData%\EduBotics\logs\install_diagnostics.log` best effort, and always exits 0. Name the account in the
  uninstall and the Step 4 consent boxes. Remove the `.iss` entry from `DiskDeletionFenceTest`'s allowlist and add
  the new script's single `wsl --unregister`.
- **dependsOn:** A1
- **layers:** 1 (installer, Pascal)
- **files.may:** `robotis_ai_setup/installer/robotis_ai_setup.iss` (ONLY the `[UninstallRun]` unregister entry, the
  `ConfirmDistroWipe` and `ShouldImportDistro` MsgBox strings, and comments beside them),
  `robotis_ai_setup/installer/scripts/uninstall_unregister_distro.ps1` (new), `robotis_ai_setup/tests/test_installer_pwsh_executed.py`
  (ONLY `UninstallGuardExecutedTest`), `robotis_ai_setup/tests/test_gui_install_lifecycle.py` (ONLY
  `DiskDeletionFenceTest`, `ScriptTerminalExitTest`'s list, a new `UninstallGuardIssTest`).
- **files.mustNot:** `AppVersion`, `[Run]`, `[Files]`, `ShouldPullImages`, `ProbeDistroStamp`, every other `.ps1`,
  `gui/**`, `.github/**`.
- **The failing test:** `UninstallGuardIssTest.test_the_uninstaller_never_runs_wsl_unregister_directly` — the
  `[UninstallRun]` section has no `Filename: "wsl.exe"` entry with `--unregister`, and has exactly one entry whose
  `Parameters` names `uninstall_unregister_distro.ps1` with `Check: ConfirmDistroWipe` and `RunOnceId: "UnregisterDistro"`.
  **Today:** fails (CONFIRMED by reading `.iss` lines 241–245). Also `DiskDeletionFenceTest` fails once its `.iss`
  allowlist entry is removed, until the `.iss` changes.
- **More tests:** `UninstallGuardExecutedTest` (sandbox, needles as A1): registered + own disk → `--unregister`
  called, rc 0; other / unknown / group (elevated) → not called, rc 0, the „wird nicht entfernt" line with the path;
  missing disk → called; not registered → nothing; unresponsive → nothing; helper file removed → nothing, rc 0.
  `UninstallGuardIssTest` also asserts both MsgBox strings contain `ExpandConstant('{username}')` and the
  „anderer Windows-Konten bleiben unverändert" sentence is in `ConfirmDistroWipe`; `ScriptTerminalExitTest` lists the
  new script; the new file starts with a UTF-8 BOM and sets `$ErrorActionPreference = "Continue"`.
- **testCommands:** the executed suite; `test_gui_install_lifecycle.py`; `test_ps_guard_scripts.py`; PS guard loop;
  the BOM check `head -c3 robotis_ai_setup/installer/scripts/uninstall_unregister_distro.ps1 | od -An -tx1` = `ef bb bf`.
- **acceptance:** Pascal compiles in the W5 prove-build (owner, §12 step 3); the uninstall on a test VM behaves as
  S0-10 records.
- **risk:** high — the installer (compile-proven only by W5), a deletion path.
- **lenses:** correctness, delivery, regression

---

### WP-A6 — Version 2.41.1

- **summary.** Bump the four version sites to `2.41.1` in one commit: `VERSION`, `installer/robotis_ai_setup.iss`
  `AppVersion=2.41.1`, `gui/app/constants.py` fallback `return "2.41.1"`, `pi_agent/constants.py` fallback
  `return "2.41.1"`. Add `robotis_ai_setup/tests/test_version_sites_lockstep.py`, which applies
  `release.yml::version-preflight`'s three source greps to the tree and requires all four to equal `VERSION`.
- **dependsOn:** A1, A2, A3, A4, A5 (full outcome) — **safe-only outcome (§12 step 4b): A1, A5**; the four edits are
  identical in both outcomes (it is still 2.41.1; there is no 2.41.1 without per-user import and a later 2.41.2 with
  it unless the owner decides so then).
- **layers:** 1, 2, Pi agent (one literal)
- **files.may:** `VERSION`, `robotis_ai_setup/installer/robotis_ai_setup.iss` (ONLY `AppVersion`),
  `robotis_ai_setup/gui/app/constants.py` (ONLY the fallback literal), `robotis_ai_setup/pi_agent/constants.py`
  (ONLY the fallback literal), `robotis_ai_setup/tests/test_version_sites_lockstep.py` (new).
- **files.mustNot:** everything else; no `ROOTFS_VERSION` change.
- **The failing check:** the scratch replay
  `docs/plans/2026-11-20-native-app-firmware/scratch/architect-A/probe_version_preflight.sh <repo> 2.41.1` —
  **today CONFIRMED FAILED** („tag v2.41.1 != VERSION 2.41.0", P9); after: `PASS`. The new lockstep test passes
  before and after (it guards against a partial bump between now and any tag).
- **testCommands:** the probe above; `cd robotis_ai_setup && python3 -m unittest discover -s tests -p "test_version_sites_lockstep.py"`;
  `cd robotis_ai_setup/pi_agent && python3 -m unittest discover -s tests -p "test_constants.py"`.
- **acceptance:** the four sites read `2.41.1`; `wsl_rootfs/ROOTFS_VERSION` still `1`.
- **risk:** medium — what ships; the release preflight is the only other guard and runs at tag time.
- **lenses:** correctness, delivery

---

### WP-A8 — CI and the release pipeline serve `release/2.41` (maintenance mode)

- **summary.** `ci.yml` runs on pushes and pull requests to `main` AND `release/**`. `release.yml`'s
  `version-preflight` decides the release mode from git ancestry and outputs `maintenance` (`'true'` or `'false'`,
  never anything else: any other outcome fails the job): a commit contained in `origin/main` → `'false'` (a normal
  release, everything exactly as today); a commit not on `main` but contained in some `origin/release/*` → `'true'`;
  a commit on neither, an unknown commit, or a checkout without `origin/main` → `::error::` and exit 1, so nothing
  runs. In maintenance mode W1 (`supabase`), W2a (`trainer-image`), W2b (`trainer-image-var`), W2 (`cloud-api`) and
  W3 (`teacher-web`) are skipped; W4 (`docker`), W5 (`installer`), W5b (`pi-agent-tarball`) and `next-step` run, and
  the summary says the cloud was not touched. Extend `test_deploy_approval_gate.py` so the job-graph simulation
  proves both modes and the decision step runs against a real git history.
- **dependsOn:** — (independent of A1–A6; built after A6 and before A7 so A7 can document it; **must be on `main`
  before the `v2.41.1` tag**, D-A12). It is the prerequisite of the first fix on `release/2.41` and of Spec C's
  C4-00.
- **layers:** CI/CD (`.github/workflows`), CI tests
- **files.may:**
  - `.github/workflows/ci.yml` — ONLY the `on:` block: `push.branches: [main, "release/**"]` and
    `pull_request.branches: [main, "release/**"]` (the `concurrency` block stays as it is; see §13 #13).
  - `.github/workflows/release.yml` — ONLY: the header comment (the two modes); `version-preflight` (its checkout
    gains `fetch-depth: 0`, its `outputs` gain `maintenance: ${{ steps.mode.outputs.maintenance }}`, one new step);
    the `if:` of `supabase`, `trainer-image`, `trainer-image-var`, `cloud-api`; `docker`'s `needs` and `if:`;
    `next-step`'s `needs` and summary text.
  - `robotis_ai_setup/tests/test_deploy_approval_gate.py`
  - `robotis_ai_setup/tests/test_ci_supply_chain_pins.py` — only if a shape it pins moves (expected: untouched;
    P23 shows it reads `version-preflight`'s channel step, which A8 leaves as it is).
- **files.mustNot:** `.github/scripts/refuse_rollback.sh` (not needed: a maintenance tag never reaches W2/W3, and a
  `main` tag keeps today's check), every other workflow (`docker-publish.yml`, `release-installer.yml`,
  `supabase-migrate.yml`, `railway-deploy-*.yml`, `publish-gui-version.yml`, `trainer-image.yml`), `.github/zizmor.yml`,
  `robotis_ai_setup/tests/test_refuse_rollback.py`, all product code, `docs/**`, `CLAUDE.md`.
- **release.yml, exact:**
  - `version-preflight` checkout:
    `with: { persist-credentials: false, fetch-depth: 0 }` — `fetch-depth: 0` is what brings
    `refs/remotes/origin/main` and `refs/remotes/origin/release/*` into the checkout; no later `git fetch` (it would
    need credentials on a private repository, the reason `refuse_rollback.sh` reads a `fetch-depth: 0` checkout too).
  - `version-preflight.outputs.maintenance: ${{ steps.mode.outputs.maintenance }}` with a comment that it is always
    `'true'` or `'false'`.
  - New step, placed after „Assert the pushed tag matches the VERSION file" and before the channel check, with no
    `if:` and no `continue-on-error`, no `${{ }}` inside `run` (zizmor; `GITHUB_SHA`/`GITHUB_OUTPUT` are runner env):
    ```yaml
      - name: Decide the release mode (main or maintenance)
        id: mode
        run: |
          set -uo pipefail
          on_branch() {  # 0 = the commit is in $1, 1 = it is not, 2 = git could not tell
            git merge-base --is-ancestor "$GITHUB_SHA" "$1"; rc=$?
            case "$rc" in 0) return 0 ;; 1) return 1 ;; *) return 2 ;; esac
          }
          if ! git rev-parse --verify --quiet "refs/remotes/origin/main^{commit}" >/dev/null; then
            echo "::error::origin/main is not in this checkout (fetch-depth: 0 is required): the release mode cannot be decided."
            exit 1
          fi
          on_branch refs/remotes/origin/main; rc=$?
          if [ "$rc" = 0 ]; then
            mode=false
          elif [ "$rc" = 2 ]; then
            echo "::error::git could not tell whether ${GITHUB_SHA} is on main."
            exit 1
          else
            mode=""
            for ref in $(git for-each-ref --format='%(refname)' refs/remotes/origin/release/); do
              on_branch "$ref"; rc=$?
              if [ "$rc" = 0 ]; then mode=true; break; fi
              if [ "$rc" = 2 ]; then echo "::error::git could not tell whether ${GITHUB_SHA} is on ${ref}."; exit 1; fi
            done
            if [ -z "$mode" ]; then
              echo "::error::${GITHUB_SHA} is on neither main nor a release/* branch: no release is built from it."
              exit 1
            fi
          fi
          echo "maintenance=${mode}" >> "$GITHUB_OUTPUT"
          echo "Release mode: maintenance=${mode}"
    ```
  - `supabase.if` — keep the two existing lines VERBATIM (the review mutations anchor on them) and wrap them:
    ```yaml
    if: >-
      ${{
        needs.version-preflight.outputs.maintenance != 'true' && (
        github.event_name != 'workflow_dispatch' ||
        !fromJSON(inputs.skip_supabase || 'false')
        )
      }}
    ```
  - `trainer-image.if` and `trainer-image-var.if`: `needs.version-preflight.outputs.maintenance != 'true'`.
  - `cloud-api.if`: insert the line `&& needs.version-preflight.outputs.maintenance != 'true'` right after
    `&& needs.version-preflight.result == 'success'`; every other line verbatim.
  - `teacher-web`: unchanged (it needs `cloud-api` to have succeeded).
  - `docker`: `needs: [version-preflight, teacher-web]` and
    ```yaml
    if: >-
      always()
      && needs.version-preflight.result == 'success'
      && (needs.teacher-web.result == 'success'
          || (needs.version-preflight.outputs.maintenance == 'true'
              && needs.teacher-web.result == 'skipped'))
    ```
  - `installer`, `pi-agent-tarball`: unchanged. `next-step`: `needs: [version-preflight, installer, pi-agent-tarball]`
    (its `if:` unchanged) and, through `env: MAINTENANCE: ${{ needs.version-preflight.outputs.maintenance }}`, one more
    summary line when it is `true`: „Maintenance release (release/*): W1, W2a, W2b, W2 and W3 were skipped — the
    database, the cloud API, the trainer image variable and the teacher web were not touched."
  - Direction of the production gates: `!= 'true'`, so an output that is somehow empty still deploys a `main`
    release (a production job is never skipped by a missing value — the rule `ProductionJobsCannotBeSkippedSilently`
    exists for), while the maintenance build path needs `== 'true'` exactly.
- **Test changes (`test_deploy_approval_gate.py`; never weaken an existing assertion):**
  - `RELEASE_ARRIVALS` and `_arrivals()`' two `release.yml` arrivals state
    `'needs.version-preflight.outputs.maintenance': 'false'` (the evaluator is strict: an unstated name is an error).
  - New arrival in `_arrivals()['release.yml']`: „a maintenance tag" —
    `{'github.event_name': 'push', 'github.ref': 'refs/tags/v2.41.2', 'needs.version-preflight.outputs.maintenance': 'true', **inputs_context(rel, {})}`;
    `_arrivals` tuples gain a fourth element `must_skip`; for this arrival
    `must_run = {'version-preflight', 'docker', 'installer', 'pi-agent-tarball', 'next-step'}` and
    `must_skip = {'supabase', 'trainer-image', 'trainer-image-var', 'cloud-api', 'teacher-web'}` (empty for every
    other arrival); `workflow_problems` reports each `must_skip` job that is not `'skipped'`.
  - The upstream-failure check considers only upstreams that RAN in that arrival
    (`for upstream in sorted(u for u in _upstream(jobs, j) if results.get(u) == 'success')`): a job that was skipped
    cannot fail. For every existing arrival every upstream runs, so their checks are unchanged.
  - `release_call_problems` keeps its two arrivals (now with the output `'false'`) and gains a check that in the
    maintenance arrival release.yml's `supabase` job is SKIPPED.
  - `test_the_review_mutations_turn_it_red` gains, each still asserting the anchor is unique: „W1 runs in
    maintenance" (remove `needs.version-preflight.outputs.maintenance != 'true' && (` and the matching `)` line from
    `supabase.if`), „W2b runs in maintenance" (remove `trainer-image-var.if`), „W2a runs in maintenance" (remove
    `trainer-image.if`), „no images in maintenance" (docker's `if:` back to `always() && needs.teacher-web.result == 'success'`),
    „docker after a failed preflight" (drop `&& needs.version-preflight.result == 'success'` from docker's `if:`). Each
    must turn `workflow_problems()` non-empty.
  - New `test_every_cloud_job_names_the_maintenance_gate`: the `if:` of `supabase`, `trainer-image`,
    `trainer-image-var` and `cloud-api` each contains `needs.version-preflight.outputs.maintenance != 'true'` (belt and
    braces: dropping it from `cloud-api` alone is not caught by the simulation, because its needs are skipped too).
  - New class `ReleaseModeIsDecidedByAncestry` (executed, `@skipUnless(git and bash)`, modelled on
    `test_refuse_rollback.py`): builds a real repository (main A–B–C; `release/2.41` from B with D; `feature` from A
    with E), clones it so `refs/remotes/origin/*` exist, extracts the „Decide the release mode" step's `run` from
    `release.yml` with `yaml.safe_load`, runs it under `bash -c` with `GITHUB_SHA` and a temp `GITHUB_OUTPUT`, and
    asserts: C → rc 0, output exactly `maintenance=false`; B → `false`; D → `true`; E → rc 1 + `::error::` and no
    output line; an unknown 40-hex SHA → rc 1; after `git update-ref -d refs/remotes/origin/main` → rc 1. Plus the
    structural checks: the step's `id` is `mode`, it has no `if:` and no `continue-on-error`, the job's checkout has
    `fetch-depth: 0` and `persist-credentials: false`, and `outputs.maintenance` is `${{ steps.mode.outputs.maintenance }}`.
  - New class `CiRunsOnReleaseBranches`: `ci.yml`'s `on.push.branches` and `on.pull_request.branches` are each
    exactly `['main', 'release/**']`, with no `paths`/`paths-ignore` filter.
- **The failing test:** `ProductionJobsCannotBeSkippedSilently.test_no_production_run_is_skipped_softened_or_redirected`
  with the maintenance arrival added (and `test_a_maintenance_tag_deploys_nothing_and_still_ships`, which asserts the
  same arrival's results directly). **Today CONFIRMED failing (P22):** with today's `release.yml` and
  `maintenance='true'` the shipped simulator runs `supabase`, `trainer-image`, `trainer-image-var`, `cloud-api` and
  `teacher-web` (all `success`), i.e. a tag on `release/2.41` would apply migrations and deploy the branch's cloud
  API and teacher web (which `refuse_rollback.sh` refuses once `main` has moved, stopping the chain before W4).
  `ReleaseModeIsDecidedByAncestry` and `CiRunsOnReleaseBranches` also fail today (no step; `branches: [main]`).
  The step's behaviour itself is PLAUSIBLE until that test runs: the architect's probe of it was blocked by the
  read-only hook (a `git init` in a temp directory counts as a git write; P25).
- **testCommands (from `robotis_ai_setup/`, a Python with PyYAML):**
  `… -m unittest discover -s tests -p "test_deploy_approval_gate.py"`;
  `… -p "test_refuse_rollback.py"`; `… -p "test_ci_supply_chain_pins.py"`; `… -p "test_publish_gui_version.py"`;
  `zizmor --offline --no-progress --min-severity low .github/workflows/` (no findings);
  `python3 -c "import yaml,sys; [yaml.safe_load(open(f)) for f in sys.argv[1:]]" ../.github/workflows/ci.yml ../.github/workflows/release.yml`.
- **acceptance:**
  - In the simulation a maintenance tag runs exactly `version-preflight`, `docker`, `installer`, `pi-agent-tarball`,
    `next-step` and skips `supabase`, `trainer-image`, `trainer-image-var`, `cloud-api`, `teacher-web` — **it deploys
    nothing to production (no migration, no Railway variable, no Railway deploy) and still builds the images, the
    installer and the Pi tarball.**
  - A `main` tag and a dispatched release run every job exactly as today; every pre-existing test of
    `test_deploy_approval_gate.py` (22 today, P23) passes, every review mutation stays red.
  - The mode step fails the run (exit 1, `::error::`) for a commit on neither branch, an unknown commit, or a checkout
    without `origin/main`; its output is never anything but `maintenance=true` / `maintenance=false`.
  - `ci.yml` triggers on `main` and `release/**`; zizmor offline reports no findings; nothing outside the listed
    lines of the two workflows changes (`git diff --stat` shows only the two workflows and the test file).
  - **Real-world proof (owner):** the `v2.41.1` tag run shows „Release mode: maintenance=false" and every surface
    ran; the first `v2.41.x` tag on `release/2.41` shows „maintenance=true", no `environment: production` approval
    is requested, `/health`'s `commit` on production is unchanged, and the release carries the `.exe` and the Pi
    tarball.
- **risk:** high — the release pipeline that writes production, the deploy-gate tests, a CI trigger.
- **lenses:** correctness, delivery, regression

---

### WP-A7 — Documentation (last)

- **summary.** Fold the new invariants into `CLAUDE.md` (present tense, symbols not lines): the „Windows GUI +
  installer" bullets for the rootfs gate (D-A4 ownership gate), the per-account distro and its default
  `%LOCALAPPDATA%\EduBotics\wsl`, „nothing deletes a disk", import exit codes 13/14 with the GUI mirrors
  `IMPORT_EXIT_*`, the report lines, finalize's skip mode and the narrowed meaning of `.reboot_required`, the
  marker's `user=` now choosing an info line, preflight check 5 as INFO, the missing-disk repair and the
  `system.vhd` sentence, `verify_system`'s absent = not yet set up, the installer still importing for the installing
  account, the double-install rule's expectation without a `ROOTFS_VERSION` bump. Rule §6 and „Deploy": the branch
  model (`release/2.41` carries WSL-product fixes, `main` the native work, the cloud stays on `main`), `ci.yml` on
  `main` and `release/**`, `release.yml`'s two modes (D-A12: how the mode is decided, what a maintenance tag skips
  and still builds, that it fails closed on a commit on neither branch), the `:latest` side effect of a maintenance
  W4, and the concurrency correction §13 #13. Correct every sentence §13 lists. Put the dated narrative (bug,
  probes, decisions, the owner's Q1–Q4 answers, S0 results once recorded) in `docs/CLAUDE-CHANGELOG.md`; living
  status (R1–R18 of §15, S0 status, RG-10 for the stillness fix) in `docs/KNOWN-ISSUES.md` (including retiring the
  „PC reverts on restart" and marker entries' stale wording where this change touches them); update
  `robotis_ai_setup/wsl_rootfs/README.md`'s manual `wsl --import` example to the per-user path.
  **Preserve the stillness fix's edits**: by the time A7 is written, `main` carries that session's `CLAUDE.md`
  (the arm check's stillness rule), `docs/CLAUDE-CHANGELOG.md` (the 2026-11-25 entry „the arm check's stillness
  window could never close on a real clock") and `docs/KNOWN-ISSUES.md` (the entry „The arm check's stillness window
  is fixed; what remains of the capture's timing is open until RG-10"); A7 starts from that `main`, edits other
  paragraphs only, and its changelog entry for 2.41.1 names both changes the release carries.
  **Safe-only variant (§12 step 4b):** document only what ships — never deleting a disk, the refusals (13/14), the
  unregister gate, the uninstall guard, the per-account default target **as the elevating account's own folder**,
  WP-A8's branch model — and say plainly in CLAUDE.md and KNOWN-ISSUES that the GUI still elevates finalize to
  import, so a standard student on a PC set up by another admin still cannot use the environment (open, parked
  A2–A4, S0-3's recorded failure). It must NOT state that the GUI imports per account without admin, name
  `-SkipDistroWork`, `IMPORT_EXIT_*`, `_import_distro_for_this_account`, the missing-disk repair dialog or the
  `system.vhd` sentence, and it keeps the old exit-0 per-account message as the current behaviour.
- **dependsOn:** A1–A6, A8 (full outcome); A1, A5, A6, A8 (safe-only outcome)
- **layers:** docs
- **files.may:** `CLAUDE.md`, `docs/CLAUDE-CHANGELOG.md`, `docs/KNOWN-ISSUES.md`, `robotis_ai_setup/wsl_rootfs/README.md`
- **files.mustNot:** any code, any test, `ROOTFS_VERSION`, `docs/deploy/**` (W6/DEPLOY runbooks are unchanged: Q4).
- **The failing check:** none (documentation). Verifier checks every §13 item is corrected, every new symbol named in
  CLAUDE.md exists (`grep`), the stillness fix's three passages above are still present byte for byte, and — in the
  safe-only variant — that none of the parked symbols is named.
- **testCommands:** `grep -n "Remove-Item … ext4.vhdx\|removal commonly fails" CLAUDE.md docs/KNOWN-ISSUES.md` returns
  only historical changelog text; `grep -n "_rest_window\|stillness window" CLAUDE.md docs/CLAUDE-CHANGELOG.md docs/KNOWN-ISSUES.md`
  still finds the stillness fix's passages; safe-only: `grep -nE "SkipDistroWork|IMPORT_EXIT_|_import_distro_for_this_account" CLAUDE.md`
  returns nothing; the whole deps-free suite (some tests read CLAUDE.md).
- **risk:** medium — it restates a data-safety rule (an under-stated rule here is how a later agent undoes it).
- **lenses:** correctness, regression

---

## 10. What this round does not do (and the trigger that makes it in scope)

| Not done | Trigger |
|---|---|
| Setup Step 4/5 importing and pulling for the ORIGINAL (pre-UAC) user (`runasoriginaluser`, `ExecAsOriginalUser`) — they still act on the installing account | Field report of disk exhaustion from over-the-shoulder installs, or Phase 7 deciding to keep the WSL installer longer. Note: `ExecAsOriginalUser` is unavailable at uninstall and `{tmp}` lives in the elevated user's profile (Inno docs, P14), so the `[Code]` probes need a shared temp location first. |
| Re-adopting an own leftover disk (`wsl --import-in-place`) or renaming it aside | A field report of S2 (own leftover) — the refusal tells IT what to do. |
| Cleaning up legacy `ProgramData\EduBotics\wsl` disks nobody uses | IT request; needs a per-account ownership survey, never automatic. |
| Detecting the post-bug double-pointing state (S22) on fielded PCs | An S0-6 result or a field log showing a registration on another account's disk. |
| Classifying WSL's compressed/encrypted-folder import error by code | S0-7 recording the exact code. |
| Stopping a student's running stack before another admin's upgrade (`CurStepChanged` acts on the installing account) | A field report of a half-updated GUI. |
| A named mutex against two concurrent imports (GUI closed mid-import and reopened) | A field log of `ERROR_ALREADY_EXISTS`/`E_ILLEGAL_STATE_CHANGE` from a second import. |
| Removing finalize's default (import) mode | The next WSL-product change to finalize — or never (WSL retires in Phase 7). |
| A maintenance W4 not re-pointing `:latest` (`docker-publish.yml` pushes `:latest` on every build, so a `release/2.41` tag moves `:latest` to its build until the next `main` push) | Anything student-facing starts pulling `:latest`, or Spec C's C4-00 stops `main`'s WSL builds (then `:latest` is only ever a maintenance build — harmless). |
| Changing `ci.yml`'s `concurrency` (it cancels in-flight runs of the same branch, `main` and `release/**` included — §13 #13) | A missing CI result on a merged commit. |
| Two update channels: W6 sets ONE `GUI_VERSION` for every Windows client, so once `main` ships a native-app release, advertising a 2.41.x from `release/2.41` competes with it | Spec C's first native release (it owns the client-side channel design). |
| Detecting UWF / Deep Freeze (KNOWN-ISSUES item) | Unchanged trigger. |

---

## 11. Ask-first items touched (CLAUDE.md „When to ask the user")

| Item | Touched? | Pre-approved? |
|---|---|---|
| `wsl --unregister EduBotics` | Product code keeps calling it (guarded); S0 runs it on test accounts | The product change is the owner's Phase 0 decision (PLAN §6). The S0 runs are owner actions on a test PC. Agents never run it. |
| `git push`, tags, W1/W2/W3/W4/W5/W6, `release/2.41` creation | Owner runbook only (§12) | Owner actions by definition. |
| Editing CI/CD config | **WP-A8 only**: `ci.yml`'s `on:` block and the listed parts of `release.yml`, with their tests | **Yes — pre-approved by the owner (Q3 = a, 2026-11-25), for exactly this edit and nothing more.** `refuse_rollback.sh`, every other workflow and `zizmor.yml` stay untouched; any further CI change is a new ask. |
| Starting W6 | Owner runbook only; W6 unchanged | Owner action (Q4 = a: it also force-updates every Pi). |
| Changing safety-critical paths (robot) | No (the stillness fix is another session's, not this spec's) | — |
| Everything else in the list | No | — |

No blocker for building A1–A8.

---

## 12. Owner runbook for 2.41.1 (the architect and the coders never push, tag, dispatch or release)

**Proof loop.** The executed PowerShell suite also runs locally in the existing image `review-pwsh-own:local`
(§9 notes; `Ran 57 … OK`, 0 skipped today); **CI's `python-tests` run of the exact commit stays the authoritative
proof** — check its log shows `test_installer_pwsh_executed` with 0 skips. Workflow tests need PyYAML
(`/opt/miniconda3/envs/lerobot/bin/python` here).

1. **The arm-check stillness fix is merged on `main`** (owner Q1 = a; another session lands it — the six files now
   uncommitted in the owner's tree: `physical_ai_server/workflow/autocal/service.py::_rest_window`, the
   frozen-stream guard in `physical_ai_server.py`, `test/test_autocal_service.py`, and its `CLAUDE.md`, changelog and
   known-issues edits). 2.41.1 is not tagged without it: without it 2.41.0's arm check can never prove rest on a real
   clock, so no kit can be calibrated.
2. **Build A1–A6 and A8 on `feat/phase0-wsl-field-fix`** and open the PR into `main` (CI runs on PRs into `main`).
   A8 may instead go in its own earlier PR; it only has to be on `main` before step 8. Nothing merges yet except,
   optionally, A8 and A1 (A1 alone is safe in every outcome). Merging deploys nothing: no Phase 0 path is in a
   W2/W3/W4 path filter (CONFIRMED by reading their `on.push.paths`); A8 changes only what a TAG does.
3. **W5 prove-build of the PR branch**: Actions → `release-installer` → Run workflow → branch
   `feat/phase0-wsl-field-fix` → `version_tag = v2.41.0`. That build bakes `IMAGE_TAG=2.41.0` (images that exist) and
   the branch's scripts and GUI, so it is also the S0 test artifact. Download `EduBotics_Setup` the same day (1-day
   retention).
4. **Spike S0** on a physical Windows 11 x64 PC or a VM with nested virtualization (GitHub's hosted Windows runners
   cannot run WSL2); S0-3 additionally on the owner's locked-down Windows PC. Take a snapshot first; S0-1 destroys
   test data. Accounts: `edu-admin` (admin), `edu-lehrer` (second admin), `edu-schueler` (standard). Record every
   step in `docs/plans/2026-11-20-native-app-firmware/S0-RESULTS.md`: account, elevated?, exact command, wsl output
   verbatim (code line), exit code, file size + LastWriteTime + `(Get-Acl).Owner` + `icacls` before/after,
   `reg query HKCU\Software\Microsoft\Windows\CurrentVersion\Lxss /s`, the GUI dialog text (screenshot).
   - **S0-1 (reproduce on 2.41.0):** `edu-admin` installs 2.41.0; `wsl -d EduBotics -- sh -c "echo S0 > /root/s0"`;
     `wsl --shutdown`; sign out. `edu-lehrer` runs the 2.41.0 installer. Expect `RegisterDistro/ERROR_FILE_EXISTS` in
     `%ProgramData%\EduBotics\logs\install_diagnostics.log`/transcripts and `C:\ProgramData\EduBotics\wsl\ext4.vhdx`
     gone; then `edu-admin`'s GUI shows „… (Fehlercode: ERROR_FILE_NOT_FOUND)". Restore the snapshot.
   - **S0-2 (fixed, same sequence with the step-3 build):** `edu-admin` installs 2.41.0, then the build (double
     install, below); `edu-lehrer` runs the build: their distro lands in
     `C:\Users\edu-lehrer\AppData\Local\EduBotics\wsl`; the ProgramData disk's size/LastWriteTime/owner unchanged;
     `edu-admin`'s `/root/s0` still present. Then, elevated as `edu-lehrer`,
     `& "C:\Program Files\EduBotics\scripts\import_edubotics_wsl.ps1" -InstallRoot "$env:ProgramData\EduBotics\wsl"`
     → exit 13 naming `edu-admin`, file intact.
   - **S0-3 (per-user import without admin):** `edu-schueler` starts the build's GUI: no UAC for the import; distro in
     `C:\Users\edu-schueler\AppData\Local\EduBotics\wsl`; record `BasePath` (with or without `\\?\`), `VhdFileName`,
     the VHDX owner (expected `edu-schueler`'s SID) and DACL; images pull; „Umgebung starten" works.
   - **S0-4/5 (missing disk):** as `edu-schueler`: `wsl --shutdown`; rename the own `ext4.vhdx` to
     `ext4.vhdx.s0-hidden`; start the GUI → the missing-disk dialog names the path; „Nein" → nothing changes
     (`wsl -l` still lists it); start again, „Ja" → re-setup succeeds; the renamed file still exists; record the start
     probe's exact error text and `wsl --unregister`'s exit code.
   - **S0-6 (who can delete what):** with a throwaway tar (`{app}\wsl_rootfs\edubotics-rootfs.tar.gz`):
     `edu-schueler` imports `S0A` into `%LOCALAPPDATA%\s0a`; `edu-lehrer` (un-elevated)
     `wsl --import-in-place S0B C:\Users\edu-schueler\AppData\Local\s0a\ext4.vhdx` (record whether WSL allows it);
     if it registered: `wsl --unregister S0B` un-elevated → the file must survive; repeat with an elevated
     `wsl --unregister` on a fresh copy → record whether it is deleted. **Release condition:** if the un-elevated
     unregister deletes the foreign file, change `Resolve-UnregisterPermission` to refuse `Unknown` un-elevated too,
     before tagging.
   - **S0-7 (compressed target):** fresh account (or `wsl --unregister EduBotics` of `edu-schueler`'s throwaway
     distro): `mkdir %LOCALAPPDATA%\EduBotics\wsl && compact /c %LOCALAPPDATA%\EduBotics\wsl`; start the GUI; record
     the import error and the German shown; nothing deleted.
   - **S0-8 (locked file in the way):** no registration; `fsutil file createnew %LOCALAPPDATA%\EduBotics\wsl\ext4.vhdx 1048576`;
     in a second PowerShell `$f=[IO.File]::Open("<path>",'Open','Read','None')`; start the GUI → exit 13 own-leftover
     dialog, file untouched; `$f.Close()`.
   - **S0-9 (SYSTEM, optional):** run the build's Setup `/VERYSILENT` as SYSTEM (a scheduled task); Step 4 logs the
     SYSTEM line and creates nothing under `C:\Windows\System32\config\systemprofile\AppData\Local\EduBotics`.
   - **S0-10 (uninstall):** as `edu-admin` uninstall, „Ja": record whether `edu-admin`'s ProgramData distro is
     unregistered (owner = `edu-admin` → yes; an owner of `Administratoren` → kept with the log line);
     `edu-schueler`'s per-user distro unaffected; the guard script ran before `{app}\scripts` vanished.
   - **S0-11 (Windows PowerShell 5.1 behaviour):** in S0-3/4/8 the `edubotics_import.log` shows readable umlauts and
     the GUI dialog matched the exit code.
   **Gate:** S0-2, S0-3, S0-4, S0-8, S0-10, S0-11 pass; S0-1, S0-6, S0-7, S0-9 recorded.

   **S0-3 decides the release's content (owner Q2 = a, decided branch).** S0-3 PASSES only when, on the owner's
   locked-down PC (and on the S0 PC), as a standard account whose WSL features an admin enabled, with no restart
   pending: (i) starting the build's GUI imports EduBotics with **no UAC prompt at any point**; (ii) `wsl -l -q` run
   as that account lists `EduBotics`; (iii) its `HKCU\…\Lxss\{GUID}\BasePath` points into that account's
   `%LOCALAPPDATA%\EduBotics\wsl`; (iv) the VHDX owner is that account's SID; (v) the images pull and „Umgebung
   starten" brings the stack up. Anything else — the import needs elevation, WSL or a policy refuses it for a
   standard user (record the exact text and code) — is a FAIL. (The owner may lift a documented PC policy and
   re-run S0-3 before deciding; the rule stays binary.)
   - **4a — S0-3 passes: the full release.** The tag commit carries A1, A2, A3, A4, A5, A6, A8, A7 (full) and the
     stillness fix. Go to step 5.
   - **4b — S0-3 fails: the safe part only.** Park A2–A4: the owner pushes them as `park/phase0-per-user-import`
     (not merged). A coder rebuilds a PR branch from `main` with the CI-validated commits of A1, A5, A6 (identical
     four edits; `dependsOn` A1, A5) and A8, then writes A7 in its **safe-only variant** (WP-A7: it must not claim
     per-user import, must name the open standard-student gap and S0-3's recorded failure). CI must be green on that
     PR. A fresh W5 prove-build of that branch (`version_tag = v2.41.0`) is the artifact for re-running **S0-2**
     (the elevated import now lands in the elevating account's own `%LOCALAPPDATA%`), **S0-8** (as a variant: the
     file in the elevating account's folder; finalize's transcript names the refusal, exit 13) and **S0-10**; S0-4
     and S0-11 do not apply (the GUI flow is unchanged). The tag commit carries A1, A5, A6, A8, A7 (safe-only) and the
     stillness fix.
5. **Dev-rig double install** (CLAUDE.md rule; no `ROOTFS_VERSION` bump, so both installs must log „Import
   übersprungen" and keep the data): on a rig with 2.41.0 installed by account X, run the step-3 (or 4b) build twice
   as X; check `/root/s0`, recordings and calibration survive and the GUI starts; in outcome 4a then log in as a
   standard account and watch the per-user import.
6. **Merge** the PR of the chosen outcome into `main` (A7 last). A2–A4 are merged only in 4a.
7. **Pre-tag check on the exact commit `<sha>` to be tagged** (all must hold):
   - the stillness fix is in it: `git grep -n "^def _rest_window" <sha> -- physical_ai_tools/physical_ai_server/physical_ai_server/workflow/autocal/service.py`
     finds the function, and `git grep -nE "def test_the_capture_loop_comes_to_rest_on_the_real_clock|def test_the_arm_check_never_reads_a_frozen_joint_stream" <sha> -- physical_ai_tools/physical_ai_server/test/test_autocal_service.py`
     finds both tests; `main`'s CI `python-tests` for `<sha>` is green (it runs the physical_ai_server suite, so
     `test/test_autocal_service.py` ran) — locally:
     `cd physical_ai_tools/physical_ai_server && /opt/miniconda3/envs/lerobot/bin/python -m pytest test/test_autocal_service.py -q -p no:cacheprovider`;
   - Phase 0 is in it: `git grep -n "function Invoke-GuardedUnregister" <sha> -- robotis_ai_setup/installer/scripts/import_edubotics_wsl.ps1`;
     in 4a also `git grep -n "SkipDistroWork" <sha> -- robotis_ai_setup/installer/scripts/finalize_install.ps1`;
     in 4b `git grep -n "_import_distro_for_this_account" <sha>` finds NOTHING;
   - A8 is in it: `git grep -n "id: mode" <sha> -- .github/workflows/release.yml` and
     `git grep -n "release/\*\*" <sha> -- .github/workflows/ci.yml`;
   - the version: `docs/plans/2026-11-20-native-app-firmware/scratch/architect-A/probe_version_preflight.sh <repo> 2.41.1`
     prints `PASS` on a checkout of `<sha>`;
   - CI's run of `<sha>` shows `test_installer_pwsh_executed` with 0 skips and `test_deploy_approval_gate` green.
8. **Tag** `v2.41.1` on `<sha>` (`git tag v2.41.1 <sha> && git push origin v2.41.1`). `release.yml` runs:
   version-preflight (tag == VERSION; **„Release mode: maintenance=false"** — A8's first real run), W1 (no migration
   file was added after `v2.41.0` — CONFIRMED by `git diff --stat dba3ac81..HEAD`, only `docs/plans/**` at spec time;
   a no-op `db push` provided `v2.41.0`'s W1 applied everything up to 060 — check the W1 log), W2a/W2b (trainer image
   rebuilt, `RUNPOD_TRAINER_IMAGE` re-pointed; lock/handler hashes unchanged), W2 cloud API and W3 teacher web at the
   tag commit (forward of production; approvals where the environment has reviewers), W4 rebuilds and pushes all 8
   images as `:2.41.1`/`:2.41` — **the server images now carry the stillness fix**, and docker-publish's smoke tests
   run in them — W5 attaches the `.exe`, W5b the Pi tarball. Nothing is advertised.
9. **Hardware test of the TAG's assets before W6** — what can be covered without a tower kit:
   - **Windows PC** (install over 2.41.0 as the same admin): „Import übersprungen", the GUI starts, the `2.41.1`
     images pull (multi-GB, R2); in 4a also a standard account's per-user import without UAC; arm scan; „Umgebung
     starten"; one recording and its upload; the Daten tab; Roboter Studio **in the simulator** (a program run, a
     preview); the Cloud pages load.
   - **The stillness fix without a kit:** on this `legacy` rig the server starts, `/calibration/status` answers and
     Roboter Studio's real-robot page reports `camera_not_tower` without an error in the Protokoll — that proves the
     changed node wiring boots, nothing more. **The arm check itself — that it now comes to rest and captures its
     poses on a real clock — is proven only by its unit tests (CI) and on the FIRST tower kit's arm check, rig gate
     RG-10** (`docs/KNOWN-ISSUES.md`). The release note says so.
   - **Orange Pi** (W6 requires the tarball tested on a real Pi): the agent updates to 2.41.1, pulls the `-opi`
     images (about 6 GB), „Umgebung starten" works, Roboter Studio in the simulator.
10. **W6**: Actions → `publish-gui-version` → `version = 2.41.1`, the two tested SHA-256s. W6 is unchanged (owner
    Q4 = a): it advertises 2.41.1 to every Windows PC **and to every Orange Pi**, whose update is forced (`PiUpdateGate`)
    and re-pulls about 6 GB per Pi; the owner accepted that for the frozen Pi fleet.
11. **Cut `release/2.41`** from the tag: `git branch release/2.41 v2.41.1 && git push origin release/2.41`
    (optionally protect it). Because A8 is in `v2.41.1`, the branch already carries `ci.yml` with `release/**` and the
    maintenance-mode `release.yml`. From then on:
    - a WSL-product fix is a PR into `release/2.41` (CI runs on it); cloud changes stay on `main` (the cloud serves
      both products); a WSL fix that also matters to code still on `main` is cherry-picked there;
    - a 2.41.x release from the branch: bump the four version sites on the branch, tag `v2.41.x` on its commit;
      `release.yml` reports „Release mode: maintenance=true", skips W1, W2a, W2b, W2, W3 (no approval is requested,
      production's `/health` commit does not change), builds and attaches images, `.exe` and Pi tarball; then the
      hardware test and W6 as above (W6 itself is dispatched from `main`, as always);
    - a tag on a commit that is on neither `main` nor a `release/*` branch fails in `version-preflight` and ships
      nothing.
12. **Migrations / rootfs / Modal:** none. Images change only through the stillness fix and the rebuild every tag
    performs.

### Rollback
- Before W6: delete nothing; simply do not run W6 (the tag advertises nothing).
- After W6: run W6 again with `2.41.0` — stops further updates. PCs already on 2.41.1 stay there (the GUI never
  downgrades); to go back install the 2.41.0 `.exe` by hand AND either pin `EDUBOTICS_IMAGE_TAG=2.41.0` or pin nothing
  (CLAUDE.md's two-step rule). Per-account distros created by 2.41.1 stay registered and work under 2.41.0; a
  student whose import had not happened falls back to 2.41.0's elevated finalize — the old deletion risk returns
  for that PC. Pis that updated stay on 2.41.1 likewise.
- A8 alone: reverting its commit on `main` restores today's `release.yml` (a `main` tag behaves identically in both);
  on `release/2.41` a revert would make a maintenance tag deploy again — never revert it there.
- There is no environment-variable switch for this change (installer scripts, GUI flow, workflow logic).
- Forward fix: a 2.41.2 tagged on `release/2.41` (maintenance mode, step 11).

---

## 13. codeTruthCorrections (documents vs. code; the code wins)

1. `preflight_system.ps1` check 5's comment, `PreflightAccountScopeTest`'s docstring and KNOWN-ISSUES' marker entry
   name a second producer of „disk on disk, not registered": „after a failed `wsl --import` the importer removes the
   partial ext4.vhdx … and that removal commonly fails". Per WSL's source a disk WSL created during a failed import is
   deleted by WSL itself (`deleteFlags` set after `CreateVhd`), so the importer's `Remove-Item` only ever deleted a
   disk WSL did NOT create — a pre-existing one, usually another account's (P4, P13). The importer's own comment
   „Clean up partial VHDX to prevent 'import over broken state' on retry" is false for the same reason.
2. The GUI's exit-0 message and preflight check 5 advise „den EduBotics-Installer erneut ausführen, während Sie mit
   Ihrem eigenen Konto angemeldet sind — das behebt beide Fälle". Run by any account that has no registration while
   another account's disk sits in the shared folder, that is exactly Sequence B, which deletes the disk (P4-B); and
   it cannot fix a standard student's account, since the installer imports for whoever elevates.
3. CLAUDE.md: „‚Is the distro registered?' has three answers … with one implementation per language". The `.iss`'s
   `IsDistroRegistered` is TWO-way (a failed `wsl --list` reads as „not registered" and makes `ShouldImportDistro`
   import); the import script's own three-way check is what stops it (CONFIRMED by reading).
4. VERIFICATION §6 C13 („no executed test covers the delete branch or `ERROR_FILE_EXISTS`") is right and understated:
   the harness's `Get-Item` mock rejects `-Force`, so the importer's pre-existing-`InstallRoot` branch exits 1 under
   the mocks before `wsl --import` (P5), and the fake `wsl` ignored the target directory.
5. `finalize_install.ps1` and `import_edubotics_wsl.ps1` are non-advanced scripts: an unknown named parameter is
   silently swallowed into `$args` (CONFIRMED P6). A newer caller passing a switch an older script lacks gets the old
   behaviour without an error — harmless only because GUI and scripts ship in one installer.
6. Research 03 §3.4 (g) „admins can delete it" — CONFIRMED and wider: `UnregisterDistribution` deletes the disk under
   `CoImpersonateClient`, so an ELEVATED `wsl --unregister` (the uninstaller's `[UninstallRun]`, Step 4's consented
   rebuild, finalize's) can delete another account's disk when its registration points there (P13; end-to-end S0-6).
7. CLAUDE.md „ci.yml: 27 validator jobs on every push/PR to `main`" — true, and its consequence for PLAN §2.5
   „Branches" was unstated: a `release/2.41` branch got no CI, and `release.yml` from it is refused by
   `refuse_rollback.sh` once `main` deploys a newer cloud API. Resolved by WP-A8 (owner Q3 = a).
8. `VIRT_DISK_NEXTSTEP_DE` (finalize + GUI) names `ProgramData` as the disk's location — false for every distro
   2.41.1 creates — and for a `MountVhd` failure on WSL's own `C:\Program Files\WSL\system.vhd` (the 24H2 corpus line
   in `test_installer_pwsh_executed.py::_MOUNTVHD_0032`) it points IT at the wrong file.
9. CLAUDE.md's double-install rule („first shows the German consent + re-imports; second logs ‚Import übersprungen'")
   holds only for a `ROOTFS_VERSION` bump; without one (2.41.1) both installs skip the import.
10. BRIEF §8's Phase 0 folder `ProgramData\EduBotics\wsl\<SID>` is superseded by `%LOCALAPPDATA%\EduBotics\wsl`
    (D-A3; any standard user can create folders under `%ProgramData%`).
11. The dispatch's local test matrix says `python3` = 3.14.5; on this machine `python3` is
    `/opt/miniconda3/bin/python3` 3.13.13 without PyYAML (9 suite errors), and `test_supabase_058` walks the
    gitignored `docs/plans/**` scratch and errors on a directory named `*.py` (P15).
12. The dispatch says the executed PowerShell tests cannot run here; a pre-existing local image
    (`review-pwsh-own:local`, pwsh 7.4.7 + Python 3.12.3) runs them: `Ran 57 … OK`, 0 skipped (P1). Nothing was
    installed or pulled.
13. CLAUDE.md „`ci.yml` cancels in-flight PR runs (never main)" and the comment above `ci.yml`'s `concurrency`
    („each main push gets its own unique group via github.ref") are false: the group is
    `ci-${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}` with `cancel-in-progress: true`,
    and every push to `main` has the same `github.ref` (`refs/heads/main`), so a newer push to `main` cancels the
    in-flight run of the older one (PLAUSIBLE from GitHub's documented concurrency semantics; not observed here). The
    same now holds for pushes to `release/**`. WP-A8 leaves the block as it is (the owner approved the `on:` block
    only); A7 corrects the sentence; §10 carries the trigger.
14. `docker-publish.yml` pushes `:latest` on EVERY build (`build-images.sh` pushes `:latest` to Docker Hub as the
    retag source, `retag` copies it to GHCR), tagged releases included — so a maintenance tag moves `:latest` to the
    `release/2.41` build until the next `main` push (CONFIRMED by reading; CLAUDE.md says nothing student-facing pulls
    `:latest`).

---

## 14. Owner answers (2026-11-25), folded in

| Q | Question (short) | Answer | Where it lives in this spec |
|---|---|---|---|
| Q1 | What does 2.41.1 carry besides Phase 0? | **(a) Ship both**: Phase 0 + the arm check's stillness-window fix (`autocal/service.py::_rest_window`, the frozen-stream guard, `test/test_autocal_service.py`, its doc edits; landed on `main` by another session, not a WP here) | Status header; D-A11; §12 steps 1, 7 (pre-tag check names `_rest_window` and its tests), 8, 9 (the arm-check part is proven only on the first kit, RG-10); WP-A7 preserves its doc edits |
| Q2 | If S0-3 shows a standard user cannot import per user? | **(a) Ship A1+A5+A6+A7 now, park A2–A4** | D-A13; §12 step 4 (the S0-3 pass criteria, outcomes 4a/4b, which commits go into the tag, the re-run S0 steps); WP-A6 and WP-A7 (safe-only variant) |
| Q3 | How does `release/2.41` ship after the cut? | **(a) CI on `release/**` + a `release.yml` maintenance mode skipping W1/W2/W2b/W3/trainer for tags not on `main`** — pre-approved ask-first item, exactly this CI edit | D-A12; WP-A8 (before the `v2.41.1` tag); §11; §12 steps 8 and 11 |
| Q4 | W6 also force-updates every Orange Pi | **(a) Accept; W6 unchanged** | §12 steps 9 and 10 |

Decided by the architect (not questions): the per-account target in `%LOCALAPPDATA%` (D-A3); finalize skip mode
instead of deleting its import mode (D-A7); keeping Setup's Steps 4/5 (D-A8); refusing own leftovers rather than
adopting them; treating a group-owned disk as unknown; the GUI-only `system.vhd` sentence; A8 landing before the
`v2.41.1` tag (D-A12); deciding the release mode by git ancestry, failing closed on a commit on neither branch, and
gating the production jobs with `!= 'true'` (WP-A8).

---

## 15. Open risks

| # | Risk | Proof / mitigation |
|---|---|---|
| R1 | A standard user's un-elevated `wsl --import` is refused on a school PC (policy, WSL version) | S0-3; Q2 |
| R2 | Every PC and every account re-downloads all images after 2.41.1 (W4 rebuilds without cache; new tags) | Accepted by shipping through the normal pipeline (PLAN §2.5); state in the release note |
| R3 | Over-the-shoulder Setup still imports and pulls for the elevating admin (extra 10+ GB) | D-A8, §10 |
| R4 | Per-user disks on small SSDs, FSLogix containers (a VHDX inside a VHD), roaming setups | KNOWN-ISSUES IT note; owner accepted the per-user cost (Q28) |
| R5 | An own leftover disk blocks the account until IT acts | S2 wording; §10 |
| R6 | `release/2.41` has no CI and no working release path once `main` diverges | Resolved by WP-A8 (Q3 = a) — as long as A8 is in `v2.41.1` (§12 step 7) |
| R7 | `v2.41.1` carries the stillness fix's server change (Q1 = a) | Its own CI tests + docker-publish smoke tests; the hardware test covers boot only (§12 step 9) |
| R8 | Windows PowerShell 5.1 differs from pwsh 7 in `-Command` exit codes or console encoding | P12 on pwsh; S0-11 |
| R9 | Older WSL builds may set the VHDX owner to `Administratoren`: then an elevated rebuild or uninstall refuses an own disk (safe direction) | S0-3/S0-10 record the owner |
| R10 | Fielded PCs already in the post-bug state (a registration on another account's disk): elevated, that account can mount and modify the other's data; not detectable un-elevated | §10 trigger |
| R11 | `.iss` Pascal is compile-checked only by W5 | §12 step 3 |
| R12 | During the rollout window a 2.41.0 GUI whose update failed („Überspringen") still runs the old finalize | Rollout is short; W6 only after the hardware test |
| R13 | Executed tests mock `Get-Acl`, the registry and identity; the real readers are proven only by S0 | S0-3, S0-6, S0-10 |
| R14 | The stillness fix's real effect (the arm check comes to rest and captures on a real clock) is unproven on hardware until the first kit | RG-10 (`docs/KNOWN-ISSUES.md`); unit tests in CI |
| R15 | WP-A8 changes the job graph of the pipeline that ships 2.41.1 itself; a defect would stop the release run | Simulation + executed step test + zizmor before the tag; the failure is loud (preflight fails, nothing deploys) |
| R16 | WP-A8's decision step is unproven here: the architect's real-git probe was blocked by the read-only hook | `ReleaseModeIsDecidedByAncestry` in CI (P25) |
| R17 | One `GUI_VERSION` channel for two products once `main` ships the native app | §10 trigger; Spec C |
| R18 | A maintenance W4 moves `:latest` to the `release/2.41` build | §13 #14; §10 trigger |

---

## 16. probesRun (all scratch under `docs/plans/2026-11-20-native-app-firmware/scratch/architect-A/`)

| # | Command | Observed |
|---|---|---|
| P0 | `git rev-parse HEAD`; `git status --porcelain` (start) | `66382d27…`; clean |
| P1 | `docker run --rm -v "$PWD":/repo:ro -w /repo/robotis_ai_setup review-pwsh-own:local bash -c 'python3 -m unittest discover -s tests -p test_installer_pwsh_executed.py'` | `Ran 57 tests in 45.667s OK` (0 skipped; pwsh 7.4.7, Python 3.12.3, arm64; image pre-existing on this machine) |
| P2 | `grep -rn -i "unregister\|vhdx\|--import\|Remove-Item" installer/ gui/app/ wsl_rootfs/` | one `*.vhdx` deletion (import failure branch); `--unregister` only in import, `.iss` `[UninstallRun]`, DD migration |
| P3 | `probe_delete_branch.py` first run | import exits 1: „A parameter cannot be found that matches parameter name 'Force'" (harness `Get-Item` mock) |
| P4 | `probe_delete_branch.py` with the mock fixed and an fs-aware fake `wsl` | A (finalize): rc 1, `ERROR_FILE_EXISTS` echoed, triad, marker „nicht eingerichtet … Speicherplatz …", foreign disk **deleted**; B (import `-Destructive`): rc 1, **deleted**; C: `Get-WslFailureClass` `""` + code `ERROR_FILE_EXISTS`, Python twin `('', 'ERROR_FILE_EXISTS')` |
| P5 | same, first run | the pre-existing-`InstallRoot` branch never reached `wsl --import` under the shipped mocks |
| P6 | `probe_finalize_skip.py` (`-SkipDistroWork` to today's finalize) | rc 0, no binding error, calls include `--import …`, `-d EduBotics -- cat …`, `docker image inspect …` |
| P7 | `cd robotis_ai_setup && python3 …/probe_gui_absent_route.py` | `events = ['finalize']` |
| P8 | `cd robotis_ai_setup && python3 …/probe_gui_missing_disk.py` | outcome `virt`; detail „WSL2 konnte eine virtuelle Festplatte nicht einbinden … (Fehlercode: ERROR_FILE_NOT_FOUND)." + ProgramData IT text; no path named |
| P9 | `probe_version_preflight.sh <repo> 2.41.1` / `2.41.0` | `FAILED` (tag ≠ VERSION) / `PASS` |
| P10 | pwsh in `review-pwsh-own:local`: symlinked dir, `Get-Item` on a missing path, `Get-Acl`, `WindowsIdentity.GetCurrent()` | `Directory, ReparsePoint`; `ItemNotFoundException`; `Get-Acl` not on Linux (`CommandNotFoundException`); `MethodInvocationException` |
| P11 | pwsh `"…{ordner}…".Replace("{ordner}", 'C:\Users\M$1ller')` | literal replacement, no regex `$1` expansion |
| P12 | pwsh `-Command` wrapper `…; $global:LASTEXITCODE = 1; try { & '/tmp/O''Brien/s.ps1' -AllowDestructiveReimport } catch { … }; exit $LASTEXITCODE` | rc 13 propagated; apostrophe path works; a throwing script → rc 1; UTF-8 umlauts in output |
| P13 | WebFetch `microsoft/WSL` master: `WslCoreFilesystem.cpp::CreateVhd`, `LxssUserSession.cpp`, `DistributionRegistration.cpp` | owner set explicitly to the user SID; protected DACL user + Administrators FILE_ALL_ACCESS; `deleteFlags` set after `CreateVhd`; path guard reads the caller's `LxssKey` (same path → `ERROR_FILE_EXISTS`); unregister deletes under `CoImpersonateClient`, skips a missing VHD, logs and continues on a failed delete; VHD path = `BasePath / VhdFileName`, `BasePath` stored as given |
| P14 | WebFetch Inno Setup docs (`[Run]` flags, `ExecAsOriginalUser`, constants) | `runasoriginaluser` [Run]-only and without effect when Setup starts elevated; `ExecAsOriginalUser` not at uninstall; `{username}` = the user running Setup/Uninstall; `{tmp}` in Setup's user temp |
| P15 | `cd robotis_ai_setup && python3 -m unittest discover -s tests` | `Ran 3218 tests in 243.892s FAILED (errors=10, skipped=92)`; 9 × no PyYAML, 1 × `test_supabase_058` on another plan's scratch dir `ver_one_…constants.py` (a directory) |
| P16 | `-p test_gui_install_lifecycle.py` / `test_finalize_marker_twin_lockstep.py` / `test_ps_guard_scripts.py` | 158 OK / 6 OK / 31 OK |
| P17 | the five `.github/scripts/ps_*.py` guards; `german_detail_lint.py` | all OK (15 files scanned); lint clean |
| P18 | read `release.yml`, `release-installer.yml`, `refuse_rollback.sh`, `ci.yml`/W2/W3/W4 `on:` blocks | ci.yml main-only; W2/W3 refuse non-descendant deploys; W5 dispatch with `version_tag` bakes `IMAGE_TAG` from it; artifact retention 1 day |
| P20 | pwsh: the fixed `Get-Item` mock (`[switch]$Force` removed from `$PSBoundParameters`, forwarded with `-Force`) on a missing and a present path | `catch [ItemNotFoundException]` matches through the mock; a present file is returned |
| P21 | `cd physical_ai_tools/physical_ai_server && python -m pytest test/test_autocal_service.py -k "rest or frozen or real_clock or jittered or period_apart or readings" -p no:cacheprovider -q` on the owner's uncommitted working tree (read-only run, no cache written) | `23 passed, 131 deselected`; the two tests the runbook's pre-tag check names (`test_the_capture_loop_comes_to_rest_on_the_real_clock`, `test_the_arm_check_never_reads_a_frozen_joint_stream`) are among them, and `service.py::_rest_window` exists in that tree |
| P22 | `cd robotis_ai_setup && <lerobot python> …/probe_maintenance_graph.py` (the gate test's own `simulate()` on today's `release.yml` with `needs.version-preflight.outputs.maintenance = 'true'` stated) | every job `success` — `supabase`, `trainer-image`, `trainer-image-var`, `cloud-api`, `teacher-web` included; `PROBLEMS: ['supabase', 'trainer-image', 'trainer-image-var', 'cloud-api', 'teacher-web']`; baseline `workflow_problems()` `[]`. This is WP-A8's failing test, run on today's code (re-run at the end: identical) |
| P23 | `<lerobot python> -m unittest discover -s tests -p test_deploy_approval_gate.py` / `test_refuse_rollback.py` / `test_ci_supply_chain_pins.py` (the system `python3` has no PyYAML) | 22 OK / 6 OK / 21 OK (re-run at the end: 22 OK) |
| P24 | `zizmor --offline --no-progress --min-severity low .github/workflows/` | „No findings to report. Good job! (8 ignored, 64 suppressed)" — the baseline WP-A8 must keep (online audits not run: no network calls from this probe) |
| P25 | a real-`git` executed probe of WP-A8's mode step (`git init` in a temp dir, two branches, `merge-base --is-ancestor`) | **blocked** by the session's read-only hook („deepwork guard (readonly): blocked [git write]"); not routed around. The step's behaviour is PLAUSIBLE (git's documented exit codes 0/1/128) until `ReleaseModeIsDecidedByAncestry` runs it in CI (R16) |
| P26 | read `ci.yml`'s `concurrency` block and `docker-publish.yml`/`build-images.sh`'s tag set | group `ci-${{ github.workflow }}-${{ github.event.pull_request.number \|\| github.ref }}` with `cancel-in-progress: true` (§13 #13); `:latest` pushed on every build, tags included (§13 #14) |
| P19 | `git status --porcelain` (end) | 6 modified tracked files that are **not** the architect's (`CLAUDE.md`, `docs/CLAUDE-CHANGELOG.md`, `docs/KNOWN-ISSUES.md`, `physical_ai_server.py`, `workflow/autocal/service.py`, `test/test_autocal_service.py`) — concurrent work in the owner's checkout (a 2026-11-25 arm-check fix). The architect wrote only under gitignored `docs/plans/`; every container mounted the repo read-only. |
