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

const SIM_OBJECTS_TOPIC = '/sim/objects';
// The scene changes only on a capture / carry step / release and the server already
// deduplicates identical payloads, so this throttle is a backstop, not the mechanism.
const SIM_OBJECTS_THROTTLE_MS = 50;
const SIM_OBJECTS_QUEUE_LENGTH = 1;

export default function useSimObjects(rosbridgeUrl, enabled = true) {
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
        sceneRef.current = next;
        setScene(next);
      });
    };

    run().catch((err) => {
      console.error('useSimObjects: error subscribing:', err);
    });

    return () => {
      cancelled = true;
      if (subscription) {
        try { subscription.unsubscribe(); } catch (_) { /* swallow */ }
        subscription = null;
      }
    };
  }, [rosbridgeUrl, enabled]);

  return scene;
}
