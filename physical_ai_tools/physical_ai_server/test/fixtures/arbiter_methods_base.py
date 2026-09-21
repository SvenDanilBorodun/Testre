# GENERATED FIXTURE — the four PhysicalAIServer arbiter methods VERBATIM from base
# 7efaeb4d60da40dcb68922393723d238cf5ec34d (the manual-mode arbiter / on_workflow
# claim), kept at their original indentation under a stand-in class so multi-line
# string literals survive byte-for-byte. test_node_init_ordering.py parses this
# with the RUNNING interpreter and asserts ast.dump(live) == ast.dump(base) per
# method — version-independent (the SAME interpreter dumps both sides), unlike a
# pinned ast.dump/ast.unparse hash, which differs across the 3.11 CI / 3.12 local
# pytest interpreters. Never executed (never imported/compileall'd); only parsed.
# Regenerate from base ONLY when base itself legitimately changes.

class _Base:
    def _assert_no_other_active(self, requested_mode: str) -> tuple[bool, str]:
        """Reject a Roboter Studio request when another mode owns the arm.

        Returns (ok, german_message). `requested_mode` is one of
        'calibration', 'workflow', 'recording', 'inference', 'training',
        'manual', 'capture', 'leader_teach'. It selects which relaxations apply:
        'manual' coexists with an open Handbetrieb session; 'capture' (a read-only
        FK snapshot) coexists with Handbetrieb AND a leader-arm take; 'leader_teach'
        coexists with nothing, itself included.
        """
        # A collision recovery owns the arm until the student finishes the two-step
        # home→resume flow (which, mid-recording, seamlessly resumes that recording). Block
        # any new operation until then — defense-in-depth behind the non-dismissible
        # CollisionModal, and it keeps the collision watchdog from fighting a new session's
        # /task/status. Cleared implicitly: _collision_active goes False at resync-complete.
        if getattr(self, '_collision_active', False):
            return False, ('Eine Kollision wird gerade behoben — bitte zuerst die Schritte '
                           'im Hinweisfenster abschließen.')
        if self.on_recording:
            return False, 'Aufnahme läuft gerade — bitte zuerst stoppen.'
        if self.on_inference:
            return False, 'Inferenz läuft gerade — bitte zuerst stoppen.'
        if self.is_training:
            return False, 'Training läuft gerade — bitte abwarten oder abbrechen.'
        if requested_mode != 'calibration' and self.on_calibration:
            return False, 'Kalibrierung läuft gerade — bitte zuerst beenden.'
        if requested_mode != 'workflow' and self.on_workflow:
            return False, 'Ein Workflow läuft gerade — bitte zuerst stoppen.'
        # Batch 2b — Handbetrieb (manual jog / hand-guide / record / replay) owns
        # the arm during a manual session. A 'manual' request RELAXES here so
        # capture/jog/record/replay coexist within the one session; every OTHER
        # mode refuses so a recording / inference can't claim the arm mid
        # hand-guide (the follower is LIMP) or mid driven jog/replay.
        # 'capture' is a read-only FK snapshot that drives nothing, so it coexists with
        # an open Handbetrieb session exactly like 'manual' does …
        if requested_mode not in ('manual', 'capture') and getattr(self, 'on_manual', False):
            return False, 'Handbetrieb ist aktiv — bitte zuerst den Handbetrieb beenden.'
        # D8 — a leader take owns the recording slot while the follower stays
        # teleoperated. Only a capture may coexist (a Position/Ziel captured mid-take).
        if requested_mode != 'capture' and getattr(self, 'on_leader_teach', False):
            return False, 'Eine Leader-Aufnahme läuft gerade — bitte zuerst beenden.'
        return True, ''

    def workflow_start_callback(self, request, response):
        # F1 — atomic mode arbitration: hold _mode_lock around the ownership check
        # AND the on_workflow claim so two tabs can't both pass the gate and end up
        # with two writers on /leader/joint_trajectory. Claim on_workflow BEFORE
        # any driver spawns (in _launch_workflow); the finally releases it when no
        # workflow actually started. _mode_lock is RELEASED before _launch_workflow
        # so it never nests _manual_lock/motion_lock (no lock-ordering deadlock).
        with self._mode_lock:
            ok, msg = self._assert_no_other_active('workflow')
            if not ok:
                response.success = False
                response.message = msg
                response.unreachable_block_ids = []
                response.unreachable_messages = []
                return response
            # HIGH-1 — mutual exclusion across BOTH manager instances. The mode
            # mutex above deliberately skips the on_workflow check for workflow
            # requests (the single-manager design relied on WorkflowManager.start's
            # own is_running guard). With a separate sim manager, a cross-kind
            # start (a real start during a sim run, or vice versa, e.g. a second
            # browser tab) would pass the mutex AND the target manager's is_running
            # (a DIFFERENT instance) — running both, one driving the real follower.
            # Refuse a start while ANY workflow (sim or real) is already live.
            #
            # ALSO gate on `on_workflow` (the teardown-race fix): `is_running`
            # flips False at the daemon's FIRST finally line (`_stop_event.set()`),
            # but `on_workflow` is cleared LAST (after the hat-thread joins, up to
            # ~1 s each). In that window `_active_workflow_manager()` is None yet a
            # workflow is still tearing down — without this a new start would slip
            # in and the finishing daemon would then clear `on_workflow=False`
            # mid-run, letting a concurrent recording/inference claim the arm.
            # `on_workflow` stays True through teardown, so this closes the window
            # without blocking a legit restart (clean finish → already False).
            if self._active_workflow_manager() is not None or getattr(
                    self, 'on_workflow', False):
                response.success = False
                response.message = 'Es läuft bereits ein Workflow. Bitte zuerst stoppen.'
                response.unreachable_block_ids = []
                response.unreachable_messages = []
                return response
            # Claim ownership under the lock BEFORE any driver spawns; a concurrent
            # start now sees on_workflow True and refuses at the gate above.
            self.on_workflow = True
        started = False
        try:
            started = self._launch_workflow(request, response)
        finally:
            # No running workflow took the claim (refused routing / failed start /
            # an exception) → release it so the arm isn't wedged.
            if not started:
                self.on_workflow = False
        return response

    def _launch_workflow(self, request, response) -> bool:
        """Route + start a workflow (sim or real). Returns True iff a workflow
        actually started (the running daemon then owns on_workflow); False on any
        refusal/failure. Populates the response on every path. Split out of
        workflow_start_callback so the F1 on_workflow claim (set by the caller
        under _mode_lock) can be released in a finally without re-indenting the
        whole routing body."""
        # Roboter Studio Phase-3 sim: the run flag rides a `sim` sibling key
        # INSIDE workflow_json (Interpreter.from_json reads only data['blocks'],
        # so the sibling is silently ignored by the interpreter). When enabled we
        # route to the virtual-arm runtime and BYPASS the leader-contention gate
        # below — a sim run never publishes to /leader/joint_trajectory, so a live
        # leader can't contend with it.
        # MED-2 — defensive parse: a crafted non-dict `sim` (string/int/list) or a
        # non-list `objects` must fail cleanly to the real path, never crash the
        # service callback (which would leave the response unpopulated → client
        # hang). Only the top-level json.loads is wrapped; the field reads must be
        # isinstance-guarded.
        sim_cfg = None
        try:
            parsed = json.loads(request.workflow_json)
            sim_cfg = parsed.get('sim') if isinstance(parsed, dict) else None
        except (ValueError, TypeError):
            sim_cfg = None
        sim_enabled = isinstance(sim_cfg, dict) and bool(sim_cfg.get('enabled'))

        if sim_enabled:
            raw_objs = sim_cfg.get('objects')
            self._sim_objects = list(raw_objs) if isinstance(raw_objs, list) else []
            self._warn_sim_objects_beyond_tag_capacity(self._sim_objects)
            manager = self._get_or_create_sim_workflow_manager()
            if manager is None:
                response.success = False
                response.message = 'Simulations-Runtime kann nicht initialisiert werden.'
                response.unreachable_block_ids = []
                response.unreachable_messages = []
                return False
            # Refresh the virtual scene on the cached SimArm so the held
            # simulation sees the objects placed for THIS run.
            try:
                if self._sim_arm is not None:
                    # Also re-seeds the arm to HOME and resets the world (the arm
                    # owns the world reset so the two can never drift apart).
                    self._sim_arm.set_objects(self._sim_objects)
                elif self._sim_world is not None:
                    self._sim_world.reset(self._sim_objects)
                self._publish_sim_objects(force=True)
            except Exception as e:  # noqa: BLE001 — scene refresh is best-effort
                self.get_logger().warning(f'sim scene refresh failed: {e}')
            # B2: RunControls sends breakpoints BEFORE /workflow/start (the BP-r1
            # contract — else early-block breakpoints are skipped); with no run
            # active they land on the REAL manager via set_breakpoints' idle
            # fallback. Copy them onto the sim manager so breakpoints work in sim.
            try:
                if self.workflow_manager is not None:
                    manager.set_breakpoints(
                        list(getattr(self.workflow_manager, '_breakpoints', ()) or ()))
            except Exception as e:  # noqa: BLE001 — best-effort
                self.get_logger().warning(f'sim breakpoint seed failed: {e}')
            # ...and the pinned destinations, for exactly the same reason. The
            # sim manager is built with `load_destinations=lambda: {}` and owns a
            # SEPARATE _persisted_destinations, while `set_destination` is only
            # ever called on the REAL manager (mark_destination_callback and
            # capture_pose_callback). So a destination the teacher pinned by
            # clicking the scene camera existed on the arm and not in the
            # simulator: measured on all 3 profiles, „bewege zu P" finished on
            # the real manager and failed in sim with „Unbekanntes Ziel: „P".
            # Bitte das Ziel zuerst in der Szenen-Kamera anklicken (pinnen)." —
            # advice the student had already followed.
            #
            # PER-ENTRY, never one `try` around the whole `for`: a raise on entry
            # 3 of 10 would silently drop 4..10 and log once — structurally the
            # `all(install(n) for n in drifted)` short-circuit CLAUDE.md calls
            # out for the Pi agent's `_install_each`. Best-effort must mean
            # "skip the entry that failed", not "stop".
            #
            # `plane_tracked` rides ALONG, and that is what keeps a REAL-WORLD
            # height out of the VIRTUAL table. The sim's `load_calibration`
            # supplies exactly `{'z_table': 0.0}` — z = 0 IS the table there — so
            # a plane-tracked pin re-resolves to 0.0 in sim while a „Position
            # merken" FK capture keeps its measured height, which is meaningful
            # in sim too (the sim arm shares the base frame). Seeding the rig's
            # measured z blindly could put a pin below −WORKSPACE_FLOOR_MARGIN_M
            # and have the SIMULATOR refuse „bewege zu P" with „Zielpunkt liegt
            # unter der Tischebene." — the mirror image of the bug this whole
            # change fixes, in the surface that is supposed to be the safe place
            # to fail.
            try:
                _pins = (self.workflow_manager.get_destinations() or {}
                         ) if self.workflow_manager is not None else {}
            except Exception as e:  # noqa: BLE001 — best-effort
                _pins = {}
                self.get_logger().warning(f'sim destination read failed: {e}')
            for _name, _d in _pins.items():
                try:
                    manager.set_destination(
                        _name, _d.get('x', 0.0), _d.get('y', 0.0),
                        # `.get('z', 0.0)` is NOT the forbidden z_table=0.0
                        # fallback: that rule guards the REAL calibration path,
                        # where a missing z means the table was never MEASURED.
                        # Here z = 0 is the sim's DEFINED table, and every entry
                        # `set_destination` wrote carries all three keys anyway.
                        _d.get('z', 0.0),
                        plane_tracked=bool(_d.get('plane_tracked')))
                except Exception as e:  # noqa: BLE001 — best-effort, per entry
                    self.get_logger().warning(
                        f'sim destination seed failed for {_name}: {e}')
        else:
            # HIGH-6 — refuse a follower-only Roboter-Studio workflow while a leader
            # arm is live (a both-arms session). Roboter Studio's trajectory writer is
            # the SOLE intended writer on /leader/joint_trajectory; with a leader
            # broadcaster also publishing there the two contend and the follower jumps
            # between the two command streams. The GUI flips the arm container into
            # follower-only (EDUBOTICS_FOLLOWER_ONLY=1) BEFORE offering Roboter Studio;
            # this is the server-side backstop, keyed SOLELY on /leader/joint_states
            # freshness (leader_appears_active — the env var is deliberately NOT read
            # here; it goes stale when the toggle recreates only the arm container).
            # NOTE (2026-09-07, activation gate): that predicate is now STRICTLY
            # CONSERVATIVE — a live leader no longer implies the trajectory
            # broadcaster is loaded, so this can refuse while there is provably no
            # contention. Refusing too readily is the safe direction, so the gate is
            # left as-is; see collision_monitor.leader_appears_active.
            # getattr-guarded so a server built
            # without the collision monitor still starts workflows normally.
            leader_check = getattr(self, 'leader_appears_active', None)
            if callable(leader_check):
                try:
                    leader_active = leader_check()
                except Exception as e:  # noqa: BLE001 — guard must not block a normal start
                    self.get_logger().warning(f'leader_appears_active check failed: {e}')
                    leader_active = False
                if leader_active:
                    response.success = False
                    response.message = (
                        'Roboter Studio kann nicht starten, solange der Leader-Arm '
                        'aktiv ist. Bitte zuerst in den Follower-Modus wechseln '
                        '(Leader-Schalter im Roboter-Studio-Tab) und erneut starten.'
                    )
                    response.unreachable_block_ids = []
                    response.unreachable_messages = []
                    return False
            manager = self._get_or_create_workflow_manager()
            if manager is None:
                response.success = False
                response.message = 'Workflow-Runtime kann nicht initialisiert werden.'
                response.unreachable_block_ids = []
                response.unreachable_messages = []
                return False
            # A program on a LIMP follower used to finish GREEN with no motion:
            # the Feetech driver drops every trajectory while torque is off (a
            # rate-limited log line only), and nothing before a run asserted it.
            # Same defensive torque-on every jog already does; on the OMX it
            # carries the hold-in-place, so it cannot snap. No hand-guided arm
            # can be stiffened here — _assert_no_other_active('workflow') has
            # already refused any open manual session.
            torque_on = getattr(self, '_set_follower_torque', None)
            if callable(torque_on) and not torque_on(True):
                response.success = False
                response.message = (
                    'Programm nicht gestartet — der Arm konnte nicht verriegelt '
                    'werden und würde sich nicht bewegen.' + self._last_torque_reason())
                response.unreachable_block_ids = []
                response.unreachable_messages = []
                return False
        success, message, unreachable = manager.start(
            request.workflow_json,
            request.workflow_id,
        )
        # on_workflow was claimed under _mode_lock in workflow_start_callback; the
        # caller's finally releases it when this returns False. On success the
        # running daemon owns it (the on_finished callback clears it on exit).
        response.success = success
        response.message = message
        # IK pre-check warnings — the React side renders setWarningText()
        # on each block id; the safety envelope still gates motion at
        # runtime so these are advisory.
        response.unreachable_block_ids = [
            str(u.get('block_id', '')) for u in (unreachable or [])
        ]
        response.unreachable_messages = [
            str(u.get('message', '')) for u in (unreachable or [])
        ]
        return bool(success)

    def _on_workflow_finished(self, terminal_phase: str) -> None:
        """Fired by WorkflowManager._run on every exit path. Releases
        the on_workflow mutex so the next mode (Aufnahme / Inferenz /
        Training / Kalibrierung) can claim the arm without the student
        having to press Stop on a workflow that's already done.

        Runs on the daemon thread, not the ROS executor thread — keep
        it tiny and side-effect-only. Phase is one of 'finished',
        'stopped', 'error'.
        """
        try:
            self.get_logger().info(
                f'Workflow daemon exited (phase={terminal_phase}); '
                f'releasing on_workflow.'
            )
        except Exception:
            pass
        self.on_workflow = False
