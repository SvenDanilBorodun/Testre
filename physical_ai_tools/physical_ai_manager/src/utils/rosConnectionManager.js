// Copyright 2025 ROBOTIS CO., LTD.
// Modifications copyright 2026 Sven Danil Borodun (EduBotics)
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
// Author: Kiwoong Park

import ROSLIB from 'roslib';

/**
 * The longest wait between two reconnect attempts to the LOCAL rosbridge
 * (spec-r5 §2.3-3). Local = the same-origin nginx proxy `ws(s)://<host>/rosbridge`
 * of the student PC and the Orange Pi; it comes back with the environment, so
 * the page retries every 2 s and never gives up — a page that lost its link
 * during a recording then sees the session's end within ~4 s, not ~30 s. Any
 * other URL (a developer pointing the page at a rosbridge elsewhere) keeps an
 * exponential backoff with a 30 s cap and gives up after 30 attempts.
 */
export const LOCAL_RECONNECT_MAX_DELAY_MS = 2000;
const REMOTE_RECONNECT_MAX_DELAY_MS = 30000;

/** True for the local same-origin rosbridge (path `/rosbridge`). */
export function isLocalRosbridgeUrl(url) {
  if (typeof url !== 'string' || !url) return false;
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return false;
  }
  return parsed.pathname.replace(/\/+$/, '') === '/rosbridge';
}

/** The wait before reconnect attempt `attempt` (0-based) to `url`. */
export function reconnectDelayMs(url, attempt) {
  const cap = isLocalRosbridgeUrl(url) ? LOCAL_RECONNECT_MAX_DELAY_MS : REMOTE_RECONNECT_MAX_DELAY_MS;
  return Math.min(1000 * Math.pow(2, Math.max(0, attempt)), cap);
}

/**
 * Singleton pattern for managing global ROS connection
 */
class RosConnectionManager {
  constructor() {
    this.ros = null;
    this.connecting = false;
    this.url = '';
    this.connectionPromise = null;
    this.onConnected = null;
    this.reconnectAttempts = 0;
    this.maxReconnectAttempts = 30;
    this.reconnectTimer = null;
    this.intentionalDisconnect = false;
    // Orange Pi robot login (features/piAuth): an async function that must
    // resolve before a websocket is opened. Null off a Pi.
    this.connectGate = null;
    // Bumped by every disconnect(): a gated attempt that outlived a disconnect
    // neither opens a socket nor revives the reconnect loop.
    this.disconnectGeneration = 0;
  }

  /**
   * Install the connect gate (Pi mode) or remove it (null). A gate that
   * rejects fails this attempt WITHOUT opening a socket; the reconnect loop
   * keeps trying, so the connection comes up once the robot session exists.
   * @param {(() => Promise<void>)|null} gate
   */
  setConnectGate(gate) {
    this.connectGate = typeof gate === 'function' ? gate : null;
  }

  /**
   * Set the callback function to be called when the ROS connection is established.
   * @param {function} onConnected - Callback function to execute on successful connection.
   */
  setOnConnected(onConnected) {
    if (typeof onConnected === 'function') {
      this.onConnected = onConnected;
    } else {
      console.warn('setOnConnected: provided callback is not a function');
      this.onConnected = null;
    }
  }

  /**
   * Get or create ROS connection
   * @param {string} rosbridgeUrl - rosbridge WebSocket URL
   * @returns {Promise<ROSLIB.Ros>} ROS connection object
   */
  async getConnection(rosbridgeUrl) {
    // Guard: never let a caller holding an unset URL tear down a healthy
    // connection. Components capture state.ros.rosbridgeUrl in render
    // closures; an early render (before StudentApp seeds setRosHost) captures
    // the Redux initial '' — and a retry from that stale closure used to fall
    // into the "URL changed" branch below: disconnect() killed the live
    // socket AND set intentionalDisconnect, and _scheduleReconnect's
    // `!this.url` guard then blocked every reconnect. Net effect (observed
    // live 2026-06-04): every page's live updates — collision modal, task
    // status, heartbeat — died ~10 s after load and only a manual reload
    // recovered. An empty URL can never be a real target, so fail the single
    // call instead of poisoning the singleton.
    if (!rosbridgeUrl) {
      throw new Error('rosbridge URL not configured yet');
    }

    // If URL has changed, clean up existing connection
    if (this.url !== rosbridgeUrl) {
      this.disconnect();
      this.url = rosbridgeUrl;
    }

    // If already connected, return existing connection
    if (this.ros && this.ros.isConnected) {
      return this.ros;
    }

    // If connection attempt is in progress, return same Promise
    if (this.connecting && this.connectionPromise) {
      console.log('Connection attempt in progress, waiting...');
      return this.connectionPromise;
    }

    // Create new connection
    console.log('Creating new global ROS connection to:', rosbridgeUrl);
    this.connecting = true;

    if (this.connectGate) {
      // Pi robot login: no websocket before a session. ONE gated attempt for
      // every concurrent caller (they all await this promise).
      const gate = this.connectGate;
      const generation = this.disconnectGeneration;
      this.connectionPromise = (async () => {
        try {
          await gate();
        } catch (error) {
          this.connecting = false;
          this.connectionPromise = null;
          // Keep the loop alive: no socket was opened, so no 'close' will
          // schedule the next attempt, and the first attempt after a URL change
          // still carries disconnect()'s intentional flag — without this the
          // page would never connect once the student logs in. Unless the page
          // itself disconnected meanwhile (a newer generation), and never
          // stacking a second timer on a pending one.
          if (generation === this.disconnectGeneration) {
            this.intentionalDisconnect = false;
            if (!this.reconnectTimer) this._scheduleReconnect();
          }
          throw new Error(`ROS connection waits for the robot login: ${error.message || error}`);
        }
        if (generation !== this.disconnectGeneration) {
          throw new Error('ROS connection was closed while it waited for the robot login');
        }
        return this._openSocket(rosbridgeUrl);
      })();
      return this.connectionPromise;
    }

    this.connectionPromise = this._openSocket(rosbridgeUrl);
    return this.connectionPromise;
  }

  /** Open the websocket (after any connect gate). */
  _openSocket(rosbridgeUrl) {
    return new Promise((resolve, reject) => {
      const ros = new ROSLIB.Ros({ url: rosbridgeUrl });

      const connectionTimeout = setTimeout(() => {
        this.connecting = false;
        this.connectionPromise = null;
        reject(new Error('ROS connection timeout - rosbridge server is not running'));
      }, 10000);

      ros.on('connection', () => {
        clearTimeout(connectionTimeout);
        console.log('Global ROS connection established');
        this.ros = ros;
        this.connecting = false;
        this.connectionPromise = null;
        this.reconnectAttempts = 0;
        this.intentionalDisconnect = false;

        resolve(ros);

        if (this.onConnected && typeof this.onConnected === 'function') {
          try {
            this.onConnected();
          } catch (error) {
            console.error('Error calling onConnected callback:', error);
          }
        }
      });

      ros.on('error', (error) => {
        clearTimeout(connectionTimeout);
        console.error('Global ROS connection error:', error);
        this.connecting = false;
        this.connectionPromise = null;
        this.ros = null;
        try { ros.close(); } catch {}
        reject(new Error(`ROS connection failed: ${error.message || error}`));
      });

      ros.on('close', () => {
        console.log('Global ROS connection closed');
        this.ros = null;
        this.connecting = false;
        this.connectionPromise = null;
        this._scheduleReconnect();
      });
    });
  }

  /**
   * Schedule a reconnection attempt with exponential backoff.
   */
  _scheduleReconnect() {
    if (this.intentionalDisconnect || !this.url) return;
    // The local rosbridge has no attempt cap (it returns with the environment).
    const local = isLocalRosbridgeUrl(this.url);
    if (!local && this.reconnectAttempts >= this.maxReconnectAttempts) {
      console.log(`Max reconnect attempts (${this.maxReconnectAttempts}) reached, giving up`);
      return;
    }

    const delay = reconnectDelayMs(this.url, this.reconnectAttempts);
    this.reconnectAttempts++;
    console.log(`Scheduling reconnect attempt ${this.reconnectAttempts}${local ? '' : `/${this.maxReconnectAttempts}`} `
      + `in ${delay}ms`);

    this.reconnectTimer = setTimeout(async () => {
      this.reconnectTimer = null;
      if (this.intentionalDisconnect) return;
      try {
        await this.getConnection(this.url);
      } catch (error) {
        console.warn('Reconnect attempt failed:', error.message);
      }
    }, delay);
  }

  /**
   * Disconnect ROS connection
   */
  disconnect() {
    this.intentionalDisconnect = true;
    this.disconnectGeneration += 1;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.ros) {
      console.log('Disconnecting global ROS connection');
      this.ros.close();
      this.ros = null;
    }
    this.connecting = false;
    this.connectionPromise = null;
    this.url = '';
    this.reconnectAttempts = 0;
  }

  /**
   * Reset reconnect counter (used by StartupGate retry)
   */
  resetReconnectCounter() {
    this.reconnectAttempts = 0;
    this.intentionalDisconnect = false;
  }

  /**
   * Check connection status
   */
  isConnected() {
    return this.ros && this.ros.isConnected;
  }

  /**
   * Return current connection object (only if connected)
   */
  getCurrentConnection() {
    return this.isConnected() ? this.ros : null;
  }
}

// Create singleton instance
const rosConnectionManager = new RosConnectionManager();

export default rosConnectionManager;
