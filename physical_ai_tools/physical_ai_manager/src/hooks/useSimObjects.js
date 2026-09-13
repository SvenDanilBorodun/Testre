import { useEffect, useRef, useState } from 'react';
import ROSLIB from 'roslib';
import rosConnectionManager from '../utils/rosConnectionManager';

// Subscribe to the simulation runtime's scene topic.
//
// `/sim/objects` is a std_msgs/String carrying JSON:
//   { epoch: <int>, held: <key|null>, objects: [{key, type, tag_id, x, y, yaw}] }
//
// It exists because the twin previously ran its OWN grasp model — a distance test
// between the end-effector and the placed coordinates — and then moved meshes the
// server never learned about. After one pick-and-place the 3D pane and the 2D
// editor disagreed, and the arm followed the editor: the „Geister-Würfel".
//
// The server is now the single source of truth. `held` says which object is in the
// jaws; `objects` says where each one IS; `epoch` bumps at every run start so the
// twin can drop stale visual state.
//
// Returns `null` until the first message — which is also what an older server image
// (no such topic) leaves it as, so callers MUST keep their local fallback for that
// case rather than treating null as "nothing is held".
//
// std_msgs/String needs no interfaces rebuild, the same reason /sim/joint_states
// reuses sensor_msgs/JointState.
//
// `delayMs` (SimScene passes utils/jointStateInterpolator's INTERP_DELAY_MS) holds
// every scene back by the same delay the 3D twin draws the ARM with. The server
// publishes a scene alongside the waypoint that changed it — the capture with the
// frame the jaws closed on — and the twin shows that waypoint 100 ms after it
// arrives. Applied at once, a grasp would attach the cube to jaws still 100 ms of
// motion away, and the offset would ride along for the whole carry (visible in a
// replay that closes while moving). 0 (the default) keeps the old immediate path.
//
// A NEW EPOCH is the exception: it is delivered at once and every scene still
// waiting from the old epoch is dropped. The epoch bumps when the server resets
// its world — at a run's start and at its end — and a reset belongs to no arm
// pose. Delayed like the rest, the run-start scene arrived 100 ms after SimScene
// had already started believing the server, so the twin showed the PREVIOUS
// run's layout (keyed by index, i.e. possibly the wrong objects) for that long.

const SIM_OBJECTS_TOPIC = '/sim/objects';
// The server deduplicates identical payloads, so between events this topic is
// silent; during a carry it follows the played waypoints (~30 Hz). The throttle
// is a backstop (≤ 50 Hz) and must stay well under the joint delay: a capture held
// back by it lands that much later than the jaws it belongs to.
const SIM_OBJECTS_THROTTLE_MS = 20;
const SIM_OBJECTS_QUEUE_LENGTH = 1;

// True when `next` differs from `prev` ONLY in where the HELD object is — the
// server's ~30 Hz carry updates. Nothing reads a held object's coordinates while
// it is held: UrdfTwin parents that mesh to the gripper link and re-seats it from
// the scene only on the capture and the release, both of which change `held` and
// are therefore always delivered. Skipping these saves SimScene (and the twin's
// object layer) a re-render per carried waypoint, on the same page as Blockly.
function onlyTheHeldObjectMoved(prev, next) {
  if (!prev || prev.epoch !== next.epoch || prev.held === null
      || prev.held !== next.held || prev.objects.length !== next.objects.length) {
    return false;
  }
  for (let i = 0; i < next.objects.length; i += 1) {
    const a = prev.objects[i];
    const b = next.objects[i];
    if (a.key !== b.key || a.type !== b.type || a.tag_id !== b.tag_id) return false;
    if (b.key !== next.held && (a.x !== b.x || a.y !== b.y || a.yaw !== b.yaw)) {
      return false;
    }
  }
  return true;
}

export default function useSimObjects(rosbridgeUrl, enabled = true, { delayMs = 0 } = {}) {
  const [scene, setScene] = useState(null);
  // Keep the latest scene readable from stable callbacks without re-subscribing.
  const sceneRef = useRef(null);

  useEffect(() => {
    if (!enabled || !rosbridgeUrl) {
      // Leaving the simulator drops the scene so a later session cannot inherit a
      // stale `held` from the previous one.
      setScene(null);
      sceneRef.current = null;
      return undefined;
    }

    let cancelled = false;
    let subscription = null;
    // Scenes still waiting out `delayMs`. Cleared on teardown, so a scene from a
    // stream being left can never land after it.
    const pending = new Set();
    // Epoch of the newest scene RECEIVED (not delivered) on this subscription.
    let lastEpoch = null;
    const deliver = (next) => {
      if (cancelled) return;
      if (onlyTheHeldObjectMoved(sceneRef.current, next)) return;
      sceneRef.current = next;
      setScene(next);
    };

    const run = async () => {
      let ros;
      try {
        ros = await rosConnectionManager.getConnection(rosbridgeUrl);
      } catch (err) {
        if (!cancelled) {
          console.warn('useSimObjects: rosbridge not connectable:', err.message);
        }
        return;
      }
      if (cancelled) return;

      subscription = new ROSLIB.Topic({
        ros,
        name: SIM_OBJECTS_TOPIC,
        messageType: 'std_msgs/msg/String',
        throttle_rate: SIM_OBJECTS_THROTTLE_MS,
        queue_length: SIM_OBJECTS_QUEUE_LENGTH,
      });
      subscription.subscribe((msg) => {
        if (cancelled) return;
        let parsed;
        try {
          parsed = JSON.parse(msg && msg.data ? msg.data : '');
        } catch (_) {
          return; // a malformed payload must never break the twin
        }
        if (!parsed || typeof parsed !== 'object' || !Array.isArray(parsed.objects)) {
          return;
        }
        const next = {
          epoch: Number.isFinite(parsed.epoch) ? parsed.epoch : 0,
          held: parsed.held === null || parsed.held === undefined ? null : parsed.held,
          objects: parsed.objects.filter(
            (o) => o && typeof o === 'object'
              && Number.isFinite(o.x) && Number.isFinite(o.y),
          ),
        };
        const newEpoch = next.epoch !== lastEpoch;
        lastEpoch = next.epoch;
        if (delayMs > 0 && !newEpoch) {
          // Equal delays keep arrival order: timers of equal timeout fire FIFO.
          const id = setTimeout(() => {
            pending.delete(id);
            deliver(next);
          }, delayMs);
          pending.add(id);
        } else {
          // A reset (or the first scene, or no delay): now, and nothing from
          // before it may land on top of it later.
          pending.forEach((id) => clearTimeout(id));
          pending.clear();
          deliver(next);
        }
      });
    };

    run().catch((err) => {
      console.error('useSimObjects: error subscribing:', err);
    });

    return () => {
      cancelled = true;
      pending.forEach((id) => clearTimeout(id));
      pending.clear();
      if (subscription) {
        try { subscription.unsubscribe(); } catch (_) { /* swallow */ }
        subscription = null;
      }
    };
  }, [rosbridgeUrl, enabled, delayMs]);

  return scene;
}
