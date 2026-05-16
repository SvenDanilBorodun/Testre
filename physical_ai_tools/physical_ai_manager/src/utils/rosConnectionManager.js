// Copyright 2025 EduBotics
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
    // Jetson auth: when the URL points at a Jetson rosbridge proxy
    // (port 9091), the proxy expects the first WS frame to be
    // {op: "auth", token: "<JWT>"}. We send that on the 'connection'
    // event BEFORE any user-facing subscribe/advertise. The JWT is
    // refreshed by useJetsonConnection on every reconnect via
    // setAuthToken so a Supabase token rotation mid-session doesn't
    // strand the WS.
    this.authToken = null;
  }

  /**
   * Set / clear the JWT to send as the first frame on connection.
   * Called by useJetsonConnection on connect (with the current Supabase
   * access token) and on disconnect (with null).
   * @param {string|null} token
   */
  setAuthToken(token) {
    this.authToken = token || null;
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

    this.connectionPromise = new Promise((resolve, reject) => {
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

        // Jetson auth-op: if a token is set, send it as the FIRST frame
        // before any subscribe/advertise the user-side `onConnected`
        // callback might queue. ROSLIB.Ros exposes the raw .socket; we
        // use it directly because ROSLIB doesn't have an 'auth' op of
        // its own. The Jetson proxy holds back upstream traffic until
        // this frame arrives and verifies.
        if (this.authToken && ros.socket && ros.socket.readyState === 1) {
          try {
            ros.socket.send(JSON.stringify({
              op: 'auth',
              token: this.authToken,
            }));
          } catch (error) {
            console.warn('Failed to send JWT auth frame:', error);
          }
        }

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

    return this.connectionPromise;
  }

  /**
   * Schedule a reconnection attempt with exponential backoff.
   */
  _scheduleReconnect() {
    if (this.intentionalDisconnect || !this.url) return;
    if (this.reconnectAttempts >= this.maxReconnectAttempts) {
      console.log(`Max reconnect attempts (${this.maxReconnectAttempts}) reached, giving up`);
      return;
    }

    const delay = Math.min(1000 * Math.pow(2, this.reconnectAttempts), 30000);
    this.reconnectAttempts++;
    console.log(`Scheduling reconnect attempt ${this.reconnectAttempts}/${this.maxReconnectAttempts} in ${delay}ms`);

    this.reconnectTimer = setTimeout(async () => {
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
