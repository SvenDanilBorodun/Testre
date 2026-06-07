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
// READ-ONLY: this component only SUBSCRIBES to /joint_states and mirrors the
// real arm onto a three.js URDF model. It never publishes to any topic — Rule
// §2 (no new software control surface onto the arm) is untouched.
//
// This module is loaded as a LAZY chunk (React.lazy + dynamic import from
// RecordPage), so three.js (~600 KB) and urdf-loader never land in the entry
// bundle the white-screen CI greps. It mounts only while the „3D-Ansicht"
// panel is open, and fully tears down its WebGL context on unmount/collapse.

import React, { useEffect, useRef, useState } from 'react';
import { useSelector } from 'react-redux';
import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls';
import { STLLoader } from 'three/examples/jsm/loaders/STLLoader';
import URDFLoader from 'urdf-loader';
import ROSLIB from 'roslib';
import rosConnectionManager from '../utils/rosConnectionManager';

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

// Match ImageGridCell's monitor-view budget: 10 Hz over rosbridge, newest frame
// only. /joint_states at 100 Hz on the wire would be wasteful for a visual twin.
const JOINT_THROTTLE_MS = 100;
const JOINT_QUEUE_LENGTH = 1;

// The follower URDF + its STL meshes are bundled under public/omx-urdf/ (copied
// there by tools/export_omx_urdf_for_web.py). Fetched relative to PUBLIC_URL so
// it works offline with NO CDN (classroom requirement) — same idiom as
// Workshop/tutorialIndex.js.
const URDF_URL = `${process.env.PUBLIC_URL || ''}/omx-urdf/omx_f.urdf`;

// Arm link colour (the URDF materials are a flat dark grey; we override for a
// cleaner, better-lit look in the viewer).
const LINK_COLOR = 0xbfc4cc;

export default function UrdfTwin() {
  const rosbridgeUrl = useSelector((state) => state.ros.rosbridgeUrl);

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

  useEffect(() => {
    const mount = mountRef.current;
    if (!mount) return undefined;

    let disposed = false;
    let animationId = null;
    let needsRender = true; // render-on-demand: only paint when something moved

    const width = mount.clientWidth || 480;
    const height = mount.clientHeight || 360;

    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x1a1d23);

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
      URDF_URL,
      (robot) => {
        if (disposed) {
          // Effect already cleaned up while the URDF was in flight — dispose
          // the freshly built robot so its geometries don't leak.
          disposeObject(robot);
          return;
        }
        // URDF +Z is "up"; rotate so the arm stands upright in the viewer.
        robot.rotation.x = -Math.PI / 2;
        scene.add(robot);
        robotRef.current = robot;
        frameRobot(camera, controls, robot);
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
      if (animationId !== null) window.cancelAnimationFrame(animationId);
      controls.dispose();
      // Dispose every geometry/material reachable from the scene (three leaks
      // GPU memory otherwise), then the shared link material + renderer.
      scene.traverse((obj) => disposeObject(obj));
      material.dispose();
      renderer.dispose();
      if (renderer.domElement && renderer.domElement.parentNode === mount) {
        mount.removeChild(renderer.domElement);
      }
    };
  }, []);

  // ---- rosbridge /joint_states subscription (ImageGridCell idiom) ----
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
          console.warn('UrdfTwin: rosbridge not connectable for /joint_states:', err.message);
        }
        return;
      }
      if (cancelled) return;

      subscription = new ROSLIB.Topic({
        ros,
        name: '/joint_states',
        messageType: 'sensor_msgs/msg/JointState',
        throttle_rate: JOINT_THROTTLE_MS,
        queue_length: JOINT_QUEUE_LENGTH,
      });
      subscription.subscribe((msg) => {
        if (cancelled) return;
        applyJointState(robotRef.current, msg);
        requestRenderRef.current();
        if (!cancelled) setHasJointData(true);
      });
    };
    run().catch((err) => {
      console.error('UrdfTwin: error subscribing to /joint_states:', err);
    });

    return () => {
      cancelled = true;
      if (subscription) {
        try { subscription.unsubscribe(); } catch (_) { /* swallow */ }
        subscription = null;
      }
    };
  }, [rosbridgeUrl]);

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
function applyJointState(robot, msg) {
  if (!robot || !msg || !Array.isArray(msg.name) || !Array.isArray(msg.position)) {
    return;
  }
  const { name, position } = msg;
  const n = Math.min(name.length, position.length);
  for (let i = 0; i < n; i += 1) {
    const jointName = name[i];
    if (FOLLOWER_JOINT_SET.has(jointName)) {
      const value = position[i];
      if (typeof value === 'number' && Number.isFinite(value)) {
        robot.setJointValue(jointName, value);
      }
    }
  }
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
