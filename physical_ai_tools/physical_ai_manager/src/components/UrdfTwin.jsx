// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
//
// Live 3D "digital twin" of the OMX FOLLOWER arm (leLab-comparison PR-7).
//
// READ-ONLY w.r.t. the real arm: this component only SUBSCRIBES to a joint-state
// topic and mirrors the arm onto a three.js URDF model. It never publishes to
// any topic — Rule §2 (no new software control surface onto the arm) is
// untouched.
//
// This module is loaded as a LAZY chunk (React.lazy + dynamic import from
// RecordPage AND from Workshop/SimScene), so three.js (~600 KB) and urdf-loader
// never land in the entry bundle the white-screen CI greps. It mounts only while
// the panel is open, and fully tears down its WebGL context on unmount/collapse.
//
// Props (ALL optional — the default-prop call `<UrdfTwin/>` is byte-for-byte the
// pre-Phase-3 behaviour, so RecordPage is untouched):
//   * jointTopic   — the JointState topic to mirror (default the bare global
//                    /joint_states; SimScene passes the sim-only /sim/joint_states).
//   * objects      — Phase-3 sim objects [{type, tag_id, x, y, yaw}] rendered as
//                    primitive meshes on a virtual table. Empty → nothing built.
//   * showTable    — draw a flat table plane at the base z=0 plane.
//   * heldObjectId — tag_id of the object currently grasped; its mesh is
//                    re-parented to the end-effector link so it follows the
//                    gripper, and dropped back to the table on release.
//   * onEndEffector — callback({x,y,z,gripper}) fired per joint update with the
//                    end-effector world position in BASE (ROS) frame + the
//                    gripper angle, so SimScene can run the grasp-attach geometry
//                    outside this thin renderer. Default null → never invoked.
//   * zones        — Phase-4 no-go ("Sperrzone") keep-out boxes
//                    [{min:[x,y,z], max:[x,y,z]}] (base-frame metres) rendered as
//                    translucent-red boxes + red wireframes. Empty (the default)
//                    → the zone effect early-returns and builds ZERO meshes, so
//                    RecordPage's `<UrdfTwin/>` is byte-for-byte unchanged.
//   * showPath     — Phase-5 "Bahn anzeigen": trace the end-effector world
//                    position as a cyan polyline. Default false → the path layer
//                    builds ZERO three primitives (no Line/BufferGeometry), so
//                    RecordPage + the jsdom three mock are untouched.
//   * pathClearToken — Phase-5 "Bahn löschen": bump this integer to empty the
//                    accumulated path buffer (setDrawRange(0,0)). Default 0.
//   * showFrames   — Phase-5 "Achsen": parent a small RGB AxesHelper to the base
//                    (link0) and TCP (end_effector_link) links. Default false →
//                    no AxesHelper is constructed (RecordPage + mock untouched).
//   * catalogDims  — Sim-stage catalog render map { [type]: {height_m, width_m,
//                    color} } (color '#rrggbb'). Each sim object is rendered as a
//                    box of width_m × height_m × width_m in the catalog colour,
//                    resting on the table. Default {} → every object falls back to
//                    the fixed SIM_OBJECT_FALLBACK_SIZE_M amber cube (RecordPage +
//                    any type absent from the map are unchanged). The map arrives
//                    ASYNC (a service fetch) after a hydrated scene has already
//                    built its meshes, so it is a dep of the object-diff effect
//                    (a mesh whose resolved dims/type changed is disposed+rebuilt).
//   * showShadows  — Sim-stage soft contact shadows: renderer shadow map + a
//                    shadow-casting key light + robot/object castShadow + the sim
//                    table as receiver. Default false → NO renderer flags are set
//                    and nothing casts (RecordPage + jsdom mock untouched).
//   * showReach    — Sim-stage translucent reach-annulus ring on the 3D table
//                    (the SAME 0.10/0.28 bounds as the 2D editor, imported from
//                    ./Workshop/simConstants — not the solver's wrist-centre span).
//                    Default false → the ring layer builds ZERO primitives.

import React, { useMemo, useEffect, useRef, useState } from 'react';
import { useSelector } from 'react-redux';
import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls';
import { STLLoader } from 'three/examples/jsm/loaders/STLLoader';
import URDFLoader from 'urdf-loader';
import ROSLIB from 'roslib';
import rosConnectionManager from '../utils/rosConnectionManager';
import {
  reachAnnulus,
  SIM_OBJECT_FALLBACK_SIZE_M,
  SIM_OBJECT_COLOR_HEX,
  SIM_OBJECT_HELD_COLOR_HEX,
} from './Workshop/simConstants';
import { armGeometry } from '../utils/armProfile';

// The 6 follower joints the URDF exposes as drivable revolute joints, in the
// wire order from omx_f_config.yaml::joint_order.follower. We map by NAME from
// each JointState message, so the order here is only documentation — unknown
// names in a message are ignored, and a missing name simply isn't updated.
const FOLLOWER_JOINTS = [
  'joint1',
  'joint2',
  'joint3',
  'joint4',
  'joint5',
  'gripper_joint_1',
];
const FOLLOWER_JOINT_SET = new Set(FOLLOWER_JOINTS);

// Per-URDF-asset render config (edu6 §4.5). The capability manifest's
// urdf_asset_id picks the row; joint names come from the manifest itself
// (armGeometry) so /joint_states, the sim publisher and this twin agree.
// gripperJoint is special-cased in emitEndEffector (the grasp-attach gripper
// channel); eeLink is the frame a held mesh re-parents to. The edu6 gripper
// FINGERS are <mimic> joints urdf-loader animates from end_gear_joint itself.
const URDF_ASSETS = {
  omx_f: {
    url: `${process.env.PUBLIC_URL || ''}/omx-urdf/omx_f.urdf`,
    eeLink: 'end_effector_link',
    baseLink: 'link0',
  },
  edu6: {
    url: `${process.env.PUBLIC_URL || ''}/edu6-urdf/edu6.urdf`,
    eeLink: 'End_effector',
    baseLink: 'base_link',
  },
};

// Match ImageGridCell's monitor-view budget: 10 Hz over rosbridge, newest frame
// only. /joint_states at 100 Hz on the wire would be wasteful for a visual twin.
const JOINT_THROTTLE_MS = 100;
const JOINT_QUEUE_LENGTH = 1;

// The URDFs + their STL meshes are bundled under public/<asset>-urdf/ (the OMX
// copied there by tools/export_omx_urdf_for_web.py; edu6 by the plan's PR-3
// asset drop). Fetched relative to PUBLIC_URL so it works offline with NO CDN
// (classroom requirement) — see URDF_ASSETS above for the per-profile rows.

// Arm link colour (the URDF materials are a flat dark grey; we override for a
// cleaner, better-lit look in the viewer).
const LINK_COLOR = 0xbfc4cc;

// ── Sim-object render constants ──────────────────────────────────────────────
// Objects are sized/coloured PER TYPE from the `catalogDims` prop (the sim-stage
// catalog seam); a type absent from the map falls back to a fixed amber cube. The
// fallback size + rest/held colours are shared with the 2D editor via
// ./Workshop/simConstants (SIM_OBJECT_FALLBACK_SIZE_M / *_COLOR_HEX) so the two
// panes never disagree. Held meshes turn green while carried, restore their own
// per-type rest colour on release.
const SIM_TABLE_SIZE_M = 0.7;
const SIM_TABLE_COLOR = 0x2a2e34;
// Phase-4 no-go ("Sperrzone") box rendering: a translucent red volume + a
// brighter red wireframe, distinct from the amber sim objects.
const ZONE_COLOR = 0xef4444;
const ZONE_EDGE_COLOR = 0xf87171;
const ZONE_OPACITY = 0.22;
// The URDF link the gripper closes around — the verified grasp frame (a fixed
// child of link5 via the URDF `end_effector_joint`). A held mesh is re-parented
// here so it follows the gripper exactly.
const EE_LINK_NAME = 'end_effector_link';

// ── Phase-5 (3D-twin upgrade) path-trail + frame-triad constants ─────────────
// "Bahn anzeigen": a cyan polyline tracing the end-effector world position over
// time. Points are appended into a PREALLOCATED Float32Array (no per-frame GC);
// the draw range grows as the arm moves. Capped so a long session can't grow the
// buffer unbounded — once full, the trail simply freezes.
const PATH_COLOR = 0x22d3ee; // cyan-400
const PATH_MAX_POINTS = 4000;
const PATH_MIN_MOVE_M = 0.001; // append only when the TCP moved ≥ ~1 mm
// Base/TCP coordinate-frame triads ("Achsen"): a small RGB AxesHelper parented to
// link0 (base) and end_effector_link (TCP). Parented under the robot so they
// inherit its -π/2 viewer rotation automatically — no manual frame math.
const BASE_LINK_NAME = 'link0';
const FRAME_AXIS_SIZE_M = 0.07;

// ── Sim-stage reach-annulus ring ("showReach") ───────────────────────────────
// A translucent green ring on the table marking the graspable annulus, using the
// SAME 0.10/0.28 bounds as the 2D editor (imported above). Green matches the 2D
// SVG annulus stroke (#22c55e). Sits just above the table (y=0.001) to avoid
// z-fighting; rendered only while showReach is true.
const REACH_RING_COLOR = 0x22c55e;
const REACH_RING_Y = 0.001;
// ── Sim-stage soft-shadow tuning ("showShadows") ─────────────────────────────
// Modest shadow map so the sim stays smooth on integrated GPUs.
const SHADOW_MAP_SIZE = 1024;

export default function UrdfTwin({
  jointTopic = '/joint_states',
  objects = [],
  showTable = false,
  heldObjectId = null,
  onEndEffector = null,
  zones = [],
  showPath = false,
  pathClearToken = 0,
  showFrames = false,
  catalogDims = {},
  showShadows = false,
  showReach = false,
}) {
  const rosbridgeUrl = useSelector((state) => state.ros.rosbridgeUrl);
  // Profile geometry (edu6 §4.5): which URDF asset to load, which joint names
  // to apply, which link is the grasp frame, and the reach-ring radii. A null/
  // OMX manifest resolves to the pre-edu6 values exactly.
  const caps = useSelector((state) => (state.tasks && state.tasks.taskStatus ? state.tasks.taskStatus.capabilities : null));
  const armGeo = useMemo(() => armGeometry(caps), [caps]);
  const asset = URDF_ASSETS[armGeo.urdfAssetId] || URDF_ASSETS.omx_f;
  const jointSet = useMemo(() => new Set(armGeo.jointNames), [armGeo]);
  const annulus = useMemo(() => reachAnnulus(caps), [caps]);
  const jointSetRef = useRef(jointSet);
  useEffect(() => { jointSetRef.current = jointSet; }, [jointSet]);
  const gripperJointRef = useRef(armGeo.gripperJointName);
  useEffect(() => { gripperJointRef.current = armGeo.gripperJointName; }, [armGeo]);

  const mountRef = useRef(null);
  // German hint chip until the first /joint_states message lands. The twin
  // still renders the static URDF rest pose immediately (works with NO arm
  // powered), so this is a "is the data flowing yet?" indicator, not a blocker.
  const [hasJointData, setHasJointData] = useState(false);
  const [loadError, setLoadError] = useState(false);

  // ---- three.js scene lifecycle (independent of the rosbridge effect) ----
  // Kept in a ref so the rosbridge effect can call robot.setJointValue without
  // re-running when the scene rebuilds.
  const robotRef = useRef(null);
  const requestRenderRef = useRef(() => {});

  // Phase-3 sim layer refs. All stay empty/unused for the default-prop call, so
  // RecordPage (no objects / no table / no held / no onEndEffector) constructs
  // ZERO of the extra three.js primitives — the jsdom three mock never sees
  // Group/BoxGeometry/PlaneGeometry either.
  const sceneRef = useRef(null);
  const objectsGroupRef = useRef(null);
  const objectMeshMapRef = useRef(new Map()); // tag_id -> THREE.Mesh
  const tableMeshRef = useRef(null);
  const prevHeldIdRef = useRef(null);
  // Phase-4 no-go zone layer (stays null/unused for the default zones=[] call).
  const zonesGroupRef = useRef(null);
  // Latest onEndEffector callback, read by the (stable) subscription closure.
  const onEndEffectorRef = useRef(onEndEffector);
  useEffect(() => {
    onEndEffectorRef.current = onEndEffector;
  }, [onEndEffector]);

  // ---- Phase-5 (3D-twin upgrade) optional-layer refs ----
  // All stay null/unused for the default-prop call (showPath/showFrames false), so
  // RecordPage constructs ZERO of the path/axes primitives and the jsdom three
  // mock (which lacks Line/BufferGeometry/AxesHelper) is never exercised.
  const pathGroupRef = useRef(null);
  const pathLineRef = useRef(null);
  const pathPositionsRef = useRef(null); // Float32Array(PATH_MAX_POINTS * 3)
  const pathStateRef = useRef({ count: 0, last: null }); // last: THREE.Vector3
  const pathTmpRef = useRef(null); // reusable Vector3 for getWorldPosition
  const axesBaseRef = useRef(null);
  const axesTcpRef = useRef(null);
  // Sim-stage reach-annulus ring (stays null/unused for the default showReach=false
  // call — the ring effect early-returns and constructs no primitive).
  const reachRingRef = useRef(null);
  // Latest toggle values read by the (stable) subscription closure + the URDF
  // load-success path + the [] -deps mount effect (none re-runs on a prop change).
  const showPathRef = useRef(showPath);
  const showFramesRef = useRef(showFrames);
  const showShadowsRef = useRef(showShadows);
  // Live catalogDims for the release snap/recolor: the held effect is keyed on
  // [heldObjectId] ONLY (adding the prop as a dep would re-run the grab/release
  // logic on every catalog arrival), so it reads the freshest map through this
  // ref instead of a stale closure.
  const catalogDimsRef = useRef(catalogDims);
  useEffect(() => {
    catalogDimsRef.current = catalogDims;
  }, [catalogDims]);
  useEffect(() => {
    showPathRef.current = showPath;
  }, [showPath]);
  useEffect(() => {
    showFramesRef.current = showFrames;
  }, [showFrames]);
  useEffect(() => {
    showShadowsRef.current = showShadows;
  }, [showShadows]);

  useEffect(() => {
    const mount = mountRef.current;
    if (!mount) return undefined;

    // Capture the (stable, never-reassigned) sim-object map for use in the
    // cleanup, so the lint's "ref may have changed by cleanup" guard is happy.
    const objectMeshMap = objectMeshMapRef.current;

    let disposed = false;
    let animationId = null;
    let needsRender = true; // render-on-demand: only paint when something moved

    const width = mount.clientWidth || 480;
    const height = mount.clientHeight || 360;

    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x1a1d23);
    sceneRef.current = scene;

    const camera = new THREE.PerspectiveCamera(45, width / height, 0.01, 100);
    camera.position.set(0.45, 0.35, 0.45);

    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.setSize(width, height);
    mount.appendChild(renderer.domElement);

    // Soft lighting: a hemisphere fill + one key directional light.
    const hemi = new THREE.HemisphereLight(0xffffff, 0x33373d, 1.1);
    scene.add(hemi);
    const key = new THREE.DirectionalLight(0xffffff, 1.4);
    key.position.set(1, 2, 1.5);
    scene.add(key);

    // Sim-stage soft contact shadows (showShadows). RecordPage's default
    // showShadows=false sets NO renderer flags and configures no shadow camera —
    // the twin is byte-for-byte unchanged. Read through the ref so the [] -deps
    // mount effect never lists the prop (a toggle would otherwise rebuild the
    // whole scene). The sim table is the receiver (see the objects/table layer);
    // robot + object meshes cast (URDF load path + buildSimObjectMesh).
    if (showShadowsRef.current) {
      renderer.shadowMap.enabled = true;
      renderer.shadowMap.type = THREE.PCFSoftShadowMap;
      key.castShadow = true;
      if (key.shadow) {
        if (key.shadow.mapSize && typeof key.shadow.mapSize.set === 'function') {
          key.shadow.mapSize.set(SHADOW_MAP_SIZE, SHADOW_MAP_SIZE);
        }
        const sc = key.shadow.camera;
        if (sc) {
          sc.near = 0.05;
          sc.far = 6;
          sc.left = -0.5;
          sc.right = 0.5;
          sc.top = 0.5;
          sc.bottom = -0.5;
          if (typeof sc.updateProjectionMatrix === 'function') {
            sc.updateProjectionMatrix();
          }
        }
        key.shadow.bias = -0.0005;
      }
    }

    // Ground grid for spatial reference (1 m, 20 divisions).
    const grid = new THREE.GridHelper(1, 20, 0x3a3f47, 0x2a2e34);
    scene.add(grid);

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.12;
    controls.target.set(0, 0.12, 0);
    controls.addEventListener('change', () => { needsRender = true; });

    const requestRender = () => { needsRender = true; };
    requestRenderRef.current = requestRender;

    // Mesh loader: urdf-loader hands us each resolved STL path; we load it with
    // three's STLLoader and wrap the geometry in a lit Mesh (urdf-loader does
    // not ship a mesh loader of its own — STL support is supplied here).
    const stlLoader = new STLLoader();
    const material = new THREE.MeshStandardMaterial({
      color: LINK_COLOR,
      metalness: 0.25,
      roughness: 0.6,
    });

    const loader = new URDFLoader();
    loader.loadMeshCb = (path, _manager, done) => {
      stlLoader.load(
        path,
        (geometry) => {
          const mesh = new THREE.Mesh(geometry, material);
          done(mesh);
        },
        undefined,
        (err) => done(null, err),
      );
    };

    loader.load(
      asset.url,
      (robot) => {
        if (disposed) {
          // Effect already cleaned up while the URDF was in flight — dispose
          // the freshly built robot so its geometries don't leak.
          disposeObject(robot);
          return;
        }
        // Per-asset link names for every module-level helper (reparent,
        // triads, path trail, emitEndEffector) — the OMX constants stay the
        // fallback so a stashless robot behaves exactly as before. (A THREE
        // Object3D always has userData; the jsdom test mock may not.)
        if (!robot.userData) robot.userData = {};
        robot.userData.eeLinkName = asset.eeLink;
        robot.userData.baseLinkName = asset.baseLink;
        // URDF +Z is "up"; rotate so the arm stands upright in the viewer.
        robot.rotation.x = -Math.PI / 2;
        scene.add(robot);
        robotRef.current = robot;
        frameRobot(camera, controls, robot);
        // Sim-stage shadows: mark every robot mesh as a shadow caster once the
        // URDF is in (the mount effect already enabled the renderer shadow map +
        // key light). Guarded by the ref so RecordPage's default call skips it.
        if (showShadowsRef.current && typeof robot.traverse === 'function') {
          robot.traverse((obj) => {
            if (obj && obj.isMesh) obj.castShadow = true;
          });
        }
        // Phase-5: if the „Achsen" toggle was already on before the URDF
        // finished loading, attach the base/TCP triads now (the frames effect
        // bailed earlier because robotRef was still null).
        if (showFramesRef.current) {
          buildFrameTriads(robot, axesBaseRef, axesTcpRef);
        }
        needsRender = true;
      },
      undefined,
      () => {
        if (!disposed) setLoadError(true);
      },
    );

    const onResize = () => {
      if (disposed || !mount) return;
      const w = mount.clientWidth || width;
      const h = mount.clientHeight || height;
      renderer.setSize(w, h);
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
      needsRender = true;
    };
    window.addEventListener('resize', onResize);
    // Also re-fit on CONTAINER resize (the dock/panel divider drag, or the whole
    // dock being widened) — a window-resize listener alone leaves the canvas at
    // its old size when only the panel changes width/height.
    let resizeObserver = null;
    if (typeof ResizeObserver !== 'undefined') {
      resizeObserver = new ResizeObserver(() => onResize());
      resizeObserver.observe(mount);
    }

    const animate = () => {
      animationId = window.requestAnimationFrame(animate);
      // OrbitControls damping keeps the scene "alive" for a few frames after a
      // drag — controlsUpdated is true while damping settles, so we keep
      // rendering until it stops, then idle.
      const controlsUpdated = controls.update();
      if (needsRender || controlsUpdated) {
        renderer.render(scene, camera);
        needsRender = false;
      }
    };
    animate();

    return () => {
      disposed = true;
      requestRenderRef.current = () => {};
      robotRef.current = null;
      window.removeEventListener('resize', onResize);
      if (resizeObserver) resizeObserver.disconnect();
      if (animationId !== null) window.cancelAnimationFrame(animationId);
      controls.dispose();
      // Dispose every geometry/material reachable from the scene (three leaks
      // GPU memory otherwise), then the shared link material + renderer. This
      // also reaches the sim-object meshes (under objectsGroup → scene) and a
      // held mesh (under the robot → scene), so no separate dispose is needed.
      scene.traverse((obj) => disposeObject(obj));
      material.dispose();
      renderer.dispose();
      if (renderer.domElement && renderer.domElement.parentNode === mount) {
        mount.removeChild(renderer.domElement);
      }
      // Drop the sim-layer bookkeeping (the meshes themselves were disposed by
      // the traverse above).
      sceneRef.current = null;
      objectsGroupRef.current = null;
      objectMeshMap.clear();
      tableMeshRef.current = null;
      prevHeldIdRef.current = null;
      zonesGroupRef.current = null;
      // The reach ring (under scene) was disposed by the traverse above; drop the ref.
      reachRingRef.current = null;
      // Phase-5: the path line (under pathGroup → scene) and the frame triads
      // (under robot links → scene) were already disposed by the traverse above;
      // just drop the bookkeeping refs + the accumulator state.
      pathGroupRef.current = null;
      pathLineRef.current = null;
      pathPositionsRef.current = null;
      pathStateRef.current = { count: 0, last: null };
      pathTmpRef.current = null;
      axesBaseRef.current = null;
      axesTcpRef.current = null;
    };
    // asset: a profile change (the capability manifest naming a different
    // urdf_asset_id) rebuilds the whole viewer with the right URDF — rare
    // (caps normally settle before the twin first opens) and the cleanup
    // above already disposes everything.
  }, [asset]);

  // ---- Phase-3: sim objects + table layer (built/diffed on demand) ----------
  // Lazily creates a THREE.Group the first time there is something to show, then
  // builds/updates/removes primitive meshes to match `objects` and toggles the
  // table plane. Held meshes are skipped here (the held effect owns their
  // transform). For the default `<UrdfTwin/>` call (objects=[], showTable=false)
  // this returns before constructing any primitive — RecordPage pays nothing.
  useEffect(() => {
    const scene = sceneRef.current;
    if (!scene) return;
    const map = objectMeshMapRef.current;
    // Nothing to build and nothing built yet → no-op (keeps Group/geometry
    // constructors out of the default path + the jsdom three mock).
    if (objects.length === 0 && !showTable && !objectsGroupRef.current) return;

    let group = objectsGroupRef.current;
    if (!group) {
      group = new THREE.Group();
      scene.add(group);
      objectsGroupRef.current = group;
    }

    // Table plane at the base z=0 plane (viewer y=0 after the robot's
    // -π/2 X-rotation, which the object layer mirrors).
    if (showTable && !tableMeshRef.current) {
      const tableGeo = new THREE.PlaneGeometry(SIM_TABLE_SIZE_M, SIM_TABLE_SIZE_M);
      const tableMat = new THREE.MeshStandardMaterial({
        color: SIM_TABLE_COLOR,
        metalness: 0.05,
        roughness: 0.95,
        side: THREE.DoubleSide,
      });
      const table = new THREE.Mesh(tableGeo, tableMat);
      table.rotation.x = -Math.PI / 2;
      table.position.set(0, 0, 0);
      // Sim-stage shadows: the opaque sim table is the shadow receiver (cleaner
      // than a separate ShadowMaterial plane — it is already the visible ground).
      if (showShadowsRef.current) table.receiveShadow = true;
      group.add(table);
      tableMeshRef.current = table;
    } else if (!showTable && tableMeshRef.current) {
      const table = tableMeshRef.current;
      if (table.parent) table.parent.remove(table);
      disposeObject(table);
      tableMeshRef.current = null;
    }

    // Diff the object meshes by tag_id. Each object is sized/coloured from the
    // catalog by TYPE; the resolved dims + type are stashed on mesh.userData so
    // the snap/recolor helpers read per-mesh dims and the diff can detect a stale
    // mesh (catalogDims arrives async after a hydrated scene builds meshes, and a
    // tag_id can be reused across workflows with a different type — both must
    // dispose+rebuild). A currently-held mesh is left untouched (never yank a
    // carried mesh); it reconciles on the NEXT objects/catalogDims change, not on
    // release itself — so a mesh built before the catalog fetch resolved and then
    // grasped mid-fetch keeps its fallback look until the next scene edit
    // (cosmetic, near-unreachable: the local service resolves in ~100 ms while a
    // grasp takes seconds to reach).
    const seen = new Set();
    objects.forEach((o) => {
      const id = simObjectKey(o);
      if (id === null) return;
      seen.add(id);
      const dims = resolveDims(catalogDims, o.type);
      let mesh = map.get(id);
      if (mesh && id !== heldObjectId && !simDimsMatch(mesh.userData, o.type, dims)) {
        if (mesh.parent) mesh.parent.remove(mesh);
        disposeObject(mesh);
        map.delete(id);
        mesh = null;
      }
      if (!mesh) {
        mesh = buildSimObjectMesh(dims, o.type, id, showShadowsRef.current);
        group.add(mesh);
        map.set(id, mesh);
      }
      // A held mesh follows the gripper (held effect owns it) — don't yank it
      // back to the table while the objects list is edited mid-carry.
      if (id !== heldObjectId) positionSimObject(mesh, o);
    });
    // Remove meshes whose object is gone.
    Array.from(map.keys()).forEach((id) => {
      if (seen.has(id)) return;
      const mesh = map.get(id);
      if (mesh) {
        if (mesh.parent) mesh.parent.remove(mesh);
        disposeObject(mesh);
      }
      map.delete(id);
    });

    requestRenderRef.current();
    // catalogDims IS a dep (its async arrival must re-size existing meshes).
    // heldObjectId is read (to skip a carried mesh) but intentionally NOT a dep:
    // the held effect re-parents on its own; re-running this on every grab would
    // be wasteful. One-shot/diff effect idiom.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [objects, showTable, catalogDims]);

  // ---- Phase-3: grasp re-parenting (mesh follows the gripper) ---------------
  // On grab, re-parent the matching mesh under the end-effector link (attach()
  // preserves world transform). On release, re-attach to the scene and snap it
  // straight down onto the table. No-op for the default null heldObjectId.
  useEffect(() => {
    const robot = robotRef.current;
    const scene = sceneRef.current;
    const map = objectMeshMapRef.current;
    const prev = prevHeldIdRef.current;

    if (prev !== null && prev !== heldObjectId) {
      const prevMesh = map.get(prev);
      if (prevMesh && scene && typeof scene.attach === 'function') {
        scene.attach(prevMesh);
        // Snap/recolor from the LIVE catalogDims when the map describes this
        // type: the mesh's build-time userData.simDims is frozen, and a catalog
        // fetch resolving MID-CARRY (a held mesh is deliberately skipped by the
        // objects-diff rebuild) would otherwise snap and recolor with stale
        // fallback values. userData is NOT overwritten — the geometry was built
        // with the old dims, and the objects-diff effect must still see the
        // mismatch to dispose+rebuild the mesh on its next run.
        const type = prevMesh.userData ? prevMesh.userData.simType : null;
        const liveMap = catalogDimsRef.current;
        const live = liveMap && type && liveMap[type]
          ? resolveDims(liveMap, type) : null;
        snapMeshToTable(prevMesh, live);
        // Restore the mesh's OWN per-type rest colour (not a global amber — a
        // catalog-coloured object would otherwise turn amber after being carried).
        setMeshColor(prevMesh, live ? live.color : restColorOf(prevMesh));
        requestRenderRef.current();
      }
    }
    if (heldObjectId !== null) {
      const mesh = map.get(heldObjectId);
      const link = robot && robot.links
        ? (robot.links[robot.userData?.eeLinkName || EE_LINK_NAME]
           || robot.links[EE_LINK_NAME]) : null;
      if (mesh && link && typeof link.attach === 'function') {
        link.attach(mesh);
        setMeshColor(mesh, SIM_OBJECT_HELD_COLOR_HEX);
        requestRenderRef.current();
      }
    }
    prevHeldIdRef.current = heldObjectId;
  }, [heldObjectId]);

  // ---- Phase-4: no-go zone layer (rebuilt on change) ------------------------
  // Mirrors the objects-diff effect, but zones have no stable id (keyed by index
  // in the editor), so a full rebuild on every change is simpler than a diff and
  // cheap (zones ≤ 16). For the default zones=[] call this returns BEFORE
  // constructing any THREE primitive — RecordPage's `<UrdfTwin/>` pays nothing and
  // the jsdom three mock (which lacks Group/BoxGeometry/EdgesGeometry/LineSegments)
  // is never exercised.
  useEffect(() => {
    const scene = sceneRef.current;
    if (!scene) return;
    const list = Array.isArray(zones) ? zones : [];
    if (list.length === 0 && !zonesGroupRef.current) return;

    let group = zonesGroupRef.current;
    if (!group) {
      group = new THREE.Group();
      scene.add(group);
      zonesGroupRef.current = group;
    }

    // Dispose + remove every existing zone child, then build fresh. Each child is
    // a flat mesh or LineSegments, so a one-level disposeObject per child frees
    // its geometry + material.
    for (let i = group.children.length - 1; i >= 0; i -= 1) {
      const child = group.children[i];
      group.remove(child);
      disposeObject(child);
    }

    list.forEach((z) => {
      const built = buildZoneObjects(z);
      if (built) built.forEach((obj) => group.add(obj));
    });

    requestRenderRef.current();
  }, [zones]);

  // ---- Phase-5: end-effector path trail ("Bahn anzeigen") -------------------
  // Lazily builds a cyan THREE.Line over a PREALLOCATED Float32Array the first
  // time the trail is enabled, then just toggles its visibility. Points are
  // appended in the joint-state callback (guarded by showPathRef). For the
  // default showPath=false call this returns BEFORE constructing any THREE
  // primitive — RecordPage's `<UrdfTwin/>` and the jsdom three mock (no Line/
  // BufferGeometry/LineBasicMaterial) are never exercised.
  useEffect(() => {
    const scene = sceneRef.current;
    if (!scene) return;
    if (!showPath && !pathGroupRef.current) return;

    let group = pathGroupRef.current;
    if (!group) {
      group = new THREE.Group();
      const positions = new Float32Array(PATH_MAX_POINTS * 3);
      const geo = new THREE.BufferGeometry();
      geo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
      geo.setDrawRange(0, 0);
      const mat = new THREE.LineBasicMaterial({ color: PATH_COLOR });
      const line = new THREE.Line(geo, mat);
      // The preallocated (mostly-zero) buffer makes the auto bounding sphere
      // wrong until it fills, so skip culling — the trail must never be hidden
      // when the camera frames a sub-region of the scene.
      line.frustumCulled = false;
      group.add(line);
      scene.add(group);
      pathGroupRef.current = group;
      pathLineRef.current = line;
      pathPositionsRef.current = positions;
      pathStateRef.current.count = 0;
      pathStateRef.current.last = null;
    }
    group.visible = showPath;
    requestRenderRef.current();
  }, [showPath]);

  // ---- Phase-5: clear the path trail ("Bahn löschen") -----------------------
  // A bumped pathClearToken empties the preallocated buffer's draw range and
  // resets the accumulator. Runs once on mount with the default token 0 (the line
  // isn't built yet → refs-only no-op, constructs nothing).
  useEffect(() => {
    const line = pathLineRef.current;
    pathStateRef.current.count = 0;
    pathStateRef.current.last = null;
    if (line && line.geometry) {
      line.geometry.setDrawRange(0, 0);
      if (line.geometry.attributes && line.geometry.attributes.position) {
        line.geometry.attributes.position.needsUpdate = true;
      }
    }
    requestRenderRef.current();
  }, [pathClearToken]);

  // ---- Phase-5: base/TCP coordinate-frame triads ("Achsen") -----------------
  // Attaches/removes an AxesHelper on link0 (base) + end_effector_link (TCP).
  // When the robot hasn't loaded yet this returns; the URDF load-success path
  // re-attaches if the toggle is already on (showFramesRef). For the default
  // showFrames=false call no AxesHelper is constructed — RecordPage + the jsdom
  // three mock (no AxesHelper) are untouched.
  useEffect(() => {
    const robot = robotRef.current;
    if (!robot || !robot.links) return;
    if (showFrames) buildFrameTriads(robot, axesBaseRef, axesTcpRef);
    else disposeFrameTriads(axesBaseRef, axesTcpRef);
    requestRenderRef.current();
  }, [showFrames]);

  // ---- Sim-stage: reach-annulus ring ("showReach") --------------------------
  // Lazily builds a translucent green ring on the table (the 0.10/0.28 graspable
  // annulus, shared with the 2D editor) the first time it is enabled, then just
  // toggles its visibility. For the default showReach=false call this returns
  // BEFORE constructing any THREE primitive — RecordPage's `<UrdfTwin/>` and the
  // jsdom three mock (no RingGeometry/MeshBasicMaterial) are never exercised.
  useEffect(() => {
    const scene = sceneRef.current;
    if (!scene) return;
    if (!showReach && !reachRingRef.current) return;

    let ring = reachRingRef.current;
    // Profile change (annulus radii) → rebuild the ring geometry.
    if (ring && ring.userData && ring.userData.annulusKey
        !== `${annulus.inner}:${annulus.outer}`) {
      sceneRef.current.remove(ring);
      if (ring.geometry) ring.geometry.dispose();
      if (ring.material) ring.material.dispose();
      reachRingRef.current = null;
      ring = null;
    }
    if (!ring) {
      const geo = new THREE.RingGeometry(annulus.inner, annulus.outer, 64);
      const mat = new THREE.MeshBasicMaterial({
        color: REACH_RING_COLOR,
        transparent: true,
        opacity: 0.15,
        side: THREE.DoubleSide,
        depthWrite: false,
      });
      ring = new THREE.Mesh(geo, mat);
      ring.userData.annulusKey = `${annulus.inner}:${annulus.outer}`;
      ring.rotation.x = -Math.PI / 2; // flat on the table (viewer y=0 plane)
      ring.position.set(0, REACH_RING_Y, 0);
      scene.add(ring);
      reachRingRef.current = ring;
    }
    ring.visible = showReach;
    requestRenderRef.current();
  }, [showReach, annulus]);

  // ---- rosbridge joint-state subscription (ImageGridCell idiom) ----
  // `jointTopic` defaults to the bare global /joint_states (RecordPage); SimScene
  // passes the sim-only /sim/joint_states so the virtual arm never collides with
  // the real joint_state_broadcaster.
  useEffect(() => {
    if (!rosbridgeUrl) return undefined;

    let cancelled = false;
    let subscription = null;

    const run = async () => {
      let ros;
      try {
        ros = await rosConnectionManager.getConnection(rosbridgeUrl);
      } catch (err) {
        if (!cancelled) {
          console.warn(`UrdfTwin: rosbridge not connectable for ${jointTopic}:`, err.message);
        }
        return;
      }
      if (cancelled) return;

      subscription = new ROSLIB.Topic({
        ros,
        name: jointTopic,
        messageType: 'sensor_msgs/msg/JointState',
        throttle_rate: JOINT_THROTTLE_MS,
        queue_length: JOINT_QUEUE_LENGTH,
      });
      subscription.subscribe((msg) => {
        if (cancelled) return;
        const robot = robotRef.current;
        applyJointState(robot, msg, jointSetRef.current);
        requestRenderRef.current();
        if (!cancelled) setHasJointData(true);
        // Phase-5: accumulate the end-effector world position into the path trail
        // — only while „Bahn anzeigen" is on (showPathRef). RecordPage's default
        // showPath=false skips this entirely (no getWorldPosition, no allocation).
        if (showPathRef.current && robot) {
          if (!pathTmpRef.current) pathTmpRef.current = new THREE.Vector3();
          const added = appendPathPoint(
            robot,
            pathLineRef.current,
            pathPositionsRef.current,
            pathStateRef.current,
            pathTmpRef.current,
          );
          if (added) requestRenderRef.current();
        }
        // Surface the end-effector pose + gripper for the sim grasp geometry —
        // only when a consumer is wired (RecordPage passes none → zero cost,
        // and the robot.links/getWorldPosition reads never run).
        const cb = onEndEffectorRef.current;
        if (cb && robot) emitEndEffector(robot, msg, cb, gripperJointRef.current);
      });
    };
    run().catch((err) => {
      console.error(`UrdfTwin: error subscribing to ${jointTopic}:`, err);
    });

    return () => {
      cancelled = true;
      if (subscription) {
        try { subscription.unsubscribe(); } catch (_) { /* swallow */ }
        subscription = null;
      }
    };
  }, [rosbridgeUrl, jointTopic]);

  return (
    <div className="relative w-full h-full rounded-[var(--radius-lg)] overflow-hidden bg-[#1a1d23]">
      <div ref={mountRef} className="absolute inset-0" />

      {/* Header chip */}
      <div className="absolute top-2 left-2 z-10 h-7 px-2.5 rounded-full bg-white/[0.08] border border-white/15 backdrop-blur-md flex items-center gap-1.5 text-[11px] text-white/80 font-mono">
        <span
          className="w-1.5 h-1.5 rounded-full"
          style={{ background: hasJointData ? 'var(--accent)' : '#9aa0a6' }}
        />
        Follower-Modell
      </div>

      {/* German "waiting for joint data" hint until the first message. */}
      {!hasJointData && !loadError && (
        <div className="absolute bottom-2 left-2 right-2 z-10 flex justify-center">
          <div className="px-3 py-1.5 rounded-full bg-black/55 border border-white/15 backdrop-blur-md text-[12px] text-white/85">
            Wartet auf Gelenkdaten …
          </div>
        </div>
      )}

      {/* Asset load failure (bundled asset missing/corrupt — should not happen). */}
      {loadError && (
        <div className="absolute inset-0 z-10 flex items-center justify-center p-4">
          <div className="px-3 py-2 rounded-xl bg-black/65 border border-white/15 text-[12px] text-white/85 text-center">
            3D-Modell konnte nicht geladen werden.
          </div>
        </div>
      )}
    </div>
  );
}

// Map a sensor_msgs/JointState message onto the robot. Only the 6 known
// follower joints are applied; any other name (e.g. gripper_joint_2, which the
// URDF mimics automatically) is ignored. Safe when robot is null (the URDF may
// still be loading) and when name/position arrays are missing/mismatched.
function applyJointState(robot, msg, jointSet = FOLLOWER_JOINT_SET) {
  if (!robot || !msg || !Array.isArray(msg.name) || !Array.isArray(msg.position)) {
    return;
  }
  const { name, position } = msg;
  const n = Math.min(name.length, position.length);
  for (let i = 0; i < n; i += 1) {
    const jointName = name[i];
    if (jointSet.has(jointName)) {
      const value = position[i];
      if (typeof value === 'number' && Number.isFinite(value)) {
        robot.setJointValue(jointName, value);
      }
    }
  }
}

// Stable id for a sim object: its (globally unique) tag_id. Returns null for a
// malformed entry so the diff skips it.
function simObjectKey(o) {
  return o && o.tag_id !== undefined && o.tag_id !== null ? o.tag_id : null;
}

// Resolve a sim object's render dims from the catalog map by TYPE, with fallbacks
// to the fixed amber cube for any type the catalog does not describe (a missing/
// late catalog fetch, or an unknown type). Returns a plain {height, width, color}
// stashed on the mesh's userData so positionSimObject / snapMeshToTable / the
// release-recolor read the per-object dims without re-consulting the map.
function resolveDims(catalogDims, type) {
  const d = catalogDims && type ? catalogDims[type] : null;
  const height = d && typeof d.height_m === 'number' && d.height_m > 0
    ? d.height_m : SIM_OBJECT_FALLBACK_SIZE_M;
  const width = d && typeof d.width_m === 'number' && d.width_m > 0
    ? d.width_m : SIM_OBJECT_FALLBACK_SIZE_M;
  const color = d && typeof d.color === 'string' && d.color
    ? d.color : SIM_OBJECT_COLOR_HEX;
  return { height, width, color };
}

// True when an existing mesh's stashed type + dims still match the freshly
// resolved catalog dims — a mismatch (type reused across workflows, or catalogDims
// arriving/changing) forces a dispose+rebuild so size/colour never go stale.
function simDimsMatch(userData, type, dims) {
  if (!userData || userData.simType !== type) return false;
  const prev = userData.simDims;
  if (!prev) return false;
  return prev.height === dims.height
    && prev.width === dims.width
    && prev.color === dims.color;
}

// Build one sim-object box (width × height × width) in the catalog colour, with
// its type + resolved dims stashed on userData. Casts a shadow when the sim-stage
// shadow layer is on.
function buildSimObjectMesh(dims, type, id, castShadow) {
  const geo = new THREE.BoxGeometry(dims.width, dims.height, dims.width);
  const mat = new THREE.MeshStandardMaterial({
    color: dims.color,
    metalness: 0.2,
    roughness: 0.6,
  });
  const mesh = new THREE.Mesh(geo, mat);
  mesh.userData.simId = id;
  mesh.userData.simType = type;
  mesh.userData.simDims = dims;
  if (castShadow) mesh.castShadow = true;
  return mesh;
}

// The per-type rest colour a released mesh restores (its own catalog colour, or
// the amber fallback for an object built without catalog dims).
function restColorOf(mesh) {
  const dims = mesh && mesh.userData && mesh.userData.simDims;
  return dims && typeof dims.color === 'string' && dims.color
    ? dims.color : SIM_OBJECT_COLOR_HEX;
}

// Place a resting sim-object mesh from its base-frame (x, y, yaw). The robot is
// added with rotation.x = -π/2 (URDF +Z up → viewer +Y up); the object layer
// mirrors that mapping: base (x, y, z) → viewer (x, z, -y). A box of edge
// height_m (its own resolved dims) rests with its bottom on the table (base z=0)
// at half-height. A base-z yaw maps to a viewer-y rotation (base +Z → viewer +Y).
function positionSimObject(mesh, o) {
  const dims = mesh && mesh.userData ? mesh.userData.simDims : null;
  const height = dims && typeof dims.height === 'number' && dims.height > 0
    ? dims.height : SIM_OBJECT_FALLBACK_SIZE_M;
  const half = height / 2;
  const x = typeof o.x === 'number' ? o.x : 0;
  const y = typeof o.y === 'number' ? o.y : 0;
  const yaw = typeof o.yaw === 'number' ? o.yaw : 0;
  mesh.position.set(x, half, -y);
  mesh.rotation.set(0, yaw, 0);
}

// Build a translucent-red box + a red wireframe for one no-go ("Sperrzone") box.
// Base (ROS) frame → viewer frame: the robot is added with rotation.x = -π/2
// (URDF +Z up → viewer +Y up), and the zone layer mirrors that mapping, so base
// (x, y, z) → viewer (x, z, -y). For a box [x0,y0,z0]..[x1,y1,z1] the viewer
// center is ((x0+x1)/2, (z0+z1)/2, -(y0+y1)/2) and the size is (x1-x0, z1-z0,
// y1-y0). Returns [boxMesh, edges] or null for a malformed/degenerate zone (the
// server treats ctx.zones defensively too).
function buildZoneObjects(z) {
  if (!z || !Array.isArray(z.min) || !Array.isArray(z.max)) return null;
  const [x0, y0, z0] = z.min;
  const [x1, y1, z1] = z.max;
  const finite = [x0, y0, z0, x1, y1, z1].every(
    (v) => typeof v === 'number' && Number.isFinite(v),
  );
  if (!finite) return null;
  const sx = Math.abs(x1 - x0);
  const sy = Math.abs(z1 - z0); // viewer +Y is the zone HEIGHT (base +Z)
  const sz = Math.abs(y1 - y0);
  if (sx <= 0 || sy <= 0 || sz <= 0) return null;

  const cx = (x0 + x1) / 2;
  const cy = (z0 + z1) / 2;
  const cz = -(y0 + y1) / 2;

  const geo = new THREE.BoxGeometry(sx, sy, sz);
  const mat = new THREE.MeshStandardMaterial({
    color: ZONE_COLOR,
    transparent: true,
    opacity: ZONE_OPACITY,
    metalness: 0.0,
    roughness: 0.9,
    depthWrite: false,
  });
  const mesh = new THREE.Mesh(geo, mat);
  mesh.position.set(cx, cy, cz);

  // EdgesGeometry derives its own geometry from the box; both the box geo (via
  // the mesh) and the edges geo (via the LineSegments) are disposed on rebuild.
  const edgesGeo = new THREE.EdgesGeometry(geo);
  const edgesMat = new THREE.LineBasicMaterial({ color: ZONE_EDGE_COLOR });
  const edges = new THREE.LineSegments(edgesGeo, edgesMat);
  edges.position.set(cx, cy, cz);

  return [mesh, edges];
}

// On release, the mesh is re-attached to the scene with its carried world
// transform; snap it straight down so it rests on the table (viewer y=half of its
// OWN height), keeping its current x/z and yaw. `liveDims` (optional) carries the
// freshly resolved catalog dims when the live map knows the type — it wins over
// the mesh's frozen build-time userData (which can be stale for a mesh grasped
// before the async catalog fetch resolved).
function snapMeshToTable(mesh, liveDims = null) {
  if (mesh && mesh.position && typeof mesh.position.y === 'number') {
    const dims = liveDims || (mesh.userData && mesh.userData.simDims);
    const height = dims && typeof dims.height === 'number' && dims.height > 0
      ? dims.height : SIM_OBJECT_FALLBACK_SIZE_M;
    mesh.position.y = height / 2;
  }
}

// Recolour a sim-object mesh (amber resting / green grasped). Tolerant of a
// missing material.color so a primitive without a standard material is a no-op.
function setMeshColor(mesh, color) {
  const mat = mesh && mesh.material;
  if (mat && mat.color && typeof mat.color.set === 'function') {
    mat.color.set(color);
  }
}

// Read the end-effector link's world position, convert it back to the base (ROS)
// frame (undo the robot's -π/2 X-rotation: viewer (X,Y,Z) → base (X, -Z, Y)),
// extract the gripper angle from the message, and hand both to the consumer.
function emitEndEffector(robot, msg, cb, gripperJoint = 'gripper_joint_1') {
  const link = robot && robot.links
    ? (robot.links[robot.userData?.eeLinkName || EE_LINK_NAME]
       || robot.links[EE_LINK_NAME]) : null;
  if (!link || typeof link.getWorldPosition !== 'function') return;
  const world = link.getWorldPosition(new THREE.Vector3());
  let gripper = null;
  if (msg && Array.isArray(msg.name) && Array.isArray(msg.position)) {
    const gi = msg.name.indexOf(gripperJoint);
    if (gi >= 0 && gi < msg.position.length) {
      const v = msg.position[gi];
      if (typeof v === 'number' && Number.isFinite(v)) gripper = v;
    }
  }
  cb({ x: world.x, y: -world.z, z: world.y, gripper });
}

// Frame the camera + orbit target on the robot's bounding box so the whole arm
// is visible regardless of mesh extents.
function frameRobot(camera, controls, robot) {
  const box = new THREE.Box3().setFromObject(robot);
  if (box.isEmpty()) return;
  const center = box.getCenter(new THREE.Vector3());
  const size = box.getSize(new THREE.Vector3());
  const maxDim = Math.max(size.x, size.y, size.z) || 0.3;
  controls.target.copy(center);
  const dist = maxDim * 2.0;
  camera.position.set(center.x + dist, center.y + dist * 0.7, center.z + dist);
  camera.near = Math.max(maxDim / 100, 0.001);
  camera.far = maxDim * 100;
  camera.updateProjectionMatrix();
  controls.update();
}

// Phase-5: append the end-effector world position to the preallocated path
// buffer when the arm has moved at least PATH_MIN_MOVE_M since the last recorded
// point. The stored coordinates are VIEWER-frame (the line is added straight to
// the scene, so it draws where the TCP visibly is — no base-frame conversion).
// Returns true when a point was added so the caller can request a render. Caps at
// PATH_MAX_POINTS — once full, further points are dropped (the trail freezes).
function appendPathPoint(robot, line, positions, state, tmp) {
  const link = robot && robot.links
    ? (robot.links[robot.userData?.eeLinkName || EE_LINK_NAME]
       || robot.links[EE_LINK_NAME]) : null;
  if (!link || typeof link.getWorldPosition !== 'function' || !line || !positions) {
    return false;
  }
  const world = link.getWorldPosition(tmp);
  if (!world || typeof world.x !== 'number') return false;
  if (state.last) {
    const dx = world.x - state.last.x;
    const dy = world.y - state.last.y;
    const dz = world.z - state.last.z;
    if (dx * dx + dy * dy + dz * dz < PATH_MIN_MOVE_M * PATH_MIN_MOVE_M) {
      return false;
    }
  }
  if (state.count >= PATH_MAX_POINTS) return false;
  const i = state.count * 3;
  positions[i] = world.x;
  positions[i + 1] = world.y;
  positions[i + 2] = world.z;
  state.count += 1;
  if (!state.last) state.last = new THREE.Vector3();
  state.last.set(world.x, world.y, world.z);
  const geo = line.geometry;
  if (geo) {
    geo.setDrawRange(0, state.count);
    if (geo.attributes && geo.attributes.position) {
      geo.attributes.position.needsUpdate = true;
    }
  }
  return true;
}

// Phase-5: parent a small RGB AxesHelper to the base (link0) and TCP
// (end_effector_link) links so each frame's orientation is shown. Idempotent (a
// triad already attached for a link is left in place) and safe when the robot/
// links aren't ready (returns without building). Parented UNDER the robot, so the
// triads inherit its -π/2 viewer rotation automatically — no manual frame math.
function buildFrameTriads(robot, baseRef, tcpRef) {
  if (!robot || !robot.links) return false;
  let built = false;
  const base = robot.links[robot.userData?.baseLinkName || BASE_LINK_NAME]
    || robot.links[BASE_LINK_NAME];
  if (base && typeof base.add === 'function' && !baseRef.current) {
    const ax = new THREE.AxesHelper(FRAME_AXIS_SIZE_M);
    base.add(ax);
    baseRef.current = ax;
    built = true;
  }
  const tcp = robot.links[robot.userData?.eeLinkName || EE_LINK_NAME]
    || robot.links[EE_LINK_NAME];
  if (tcp && typeof tcp.add === 'function' && !tcpRef.current) {
    const ax = new THREE.AxesHelper(FRAME_AXIS_SIZE_M);
    tcp.add(ax);
    tcpRef.current = ax;
    built = true;
  }
  return built;
}

// Phase-5: remove + dispose both frame triads (no-op for an unattached ref).
// AxesHelper is a LineSegments with its own geometry + LineBasicMaterial, both
// freed by disposeObject.
function disposeFrameTriads(baseRef, tcpRef) {
  [baseRef, tcpRef].forEach((ref) => {
    const ax = ref.current;
    if (ax) {
      if (ax.parent) ax.parent.remove(ax);
      disposeObject(ax);
      ref.current = null;
    }
  });
}

// Recursively dispose three geometries/materials under an object (GPU memory).
function disposeObject(obj) {
  if (!obj) return;
  if (obj.geometry && typeof obj.geometry.dispose === 'function') {
    obj.geometry.dispose();
  }
  const mat = obj.material;
  if (mat) {
    if (Array.isArray(mat)) {
      mat.forEach((m) => m && typeof m.dispose === 'function' && m.dispose());
    } else if (typeof mat.dispose === 'function') {
      mat.dispose();
    }
  }
}
