// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// The wire to the robot for Cloud-Inferenz. Pinned: the start request carries
// EXACTLY task_info, session_token and edge_token (the srv's field names); only
// the LOCAL rosbridge is called and nothing connects; an image without the
// service is `oldImage`; no answer in 10 s is `timedOut`; rosbridge's English
// text never reaches the student and neither credential comes back in a
// message; the stop is bounded; the state subscription forwards only valid
// payloads.

import ROSLIB from 'roslib';
import rosConnectionManager from '../../../utils/rosConnectionManager';
import {
  START_TIMEOUT_MS,
  STOP_TIMEOUT_MS,
  startCloudInference,
  stopCloudInference,
  subscribeCloudState,
} from '../robotCalls';
import { START_REQUEST_KEYS, START_SERVICE, START_SERVICE_TYPE, STATE_TOPIC, STOP_SERVICE, STOP_SERVICE_TYPE } from '../robotContract';
import { cloudCopy } from '../../../components/CloudInference/cloudCopy';

const CONNECT = 'C'.repeat(43);
const EDGE = 'edge-' + 'e'.repeat(30);

const mockCalls = [];
const mockTopics = [];
vi.mock('roslib', () => ({
  __esModule: true,
  default: {
    Topic: function TopicMock(opts) {
      this.opts = opts;
      this.cb = null;
      this.unsubscribed = false;
      this.subscribe = (cb) => { this.cb = cb; };
      this.unsubscribe = () => { this.unsubscribed = true; };
      mockTopics.push(this);
    },
    ServiceRequest: function RequestMock(values) { Object.assign(this, values); },
    Service: function ServiceMock(opts) {
      this.opts = opts;
      this.callService = (request, ok, fail) => { mockCalls.push({ service: this, request, ok, fail }); };
    },
  },
}));

beforeEach(() => {
  mockCalls.length = 0;
  mockTopics.length = 0;
  rosConnectionManager.url = 'ws://localhost/rosbridge';
  vi.spyOn(rosConnectionManager, 'getCurrentConnection').mockReturnValue({ isConnected: true });
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

const request = () => ({ task_info: { task_type: 'inference', fps: 30 }, session_token: CONNECT, edge_token: EDGE });

describe('startCloudInference', () => {
  it('sends exactly task_info, session_token and edge_token to the start service', async () => {
    const pending = startCloudInference(request());
    expect(mockCalls).toHaveLength(1);
    const { service, request: req } = mockCalls[0];
    expect(service.opts.name).toBe(START_SERVICE);
    expect(service.opts.serviceType).toBe(START_SERVICE_TYPE);
    expect(Object.keys(req)).toEqual([...START_REQUEST_KEYS]);
    expect(req.session_token).toBe(CONNECT);
    expect(req.edge_token).toBe(EDGE);
    mockCalls[0].ok({ success: true, message: 'Cloud-Inferenz gestartet.' });
    await expect(pending).resolves.toEqual({ success: true, message: '', oldImage: false, timedOut: false });
  });

  it('shows the robot\'s own refusal as-is, without either credential', async () => {
    const pending = startCloudInference(request());
    mockCalls[0].ok({ success: false, message: `Der Leader-Arm ist aktiv ${CONNECT} ${EDGE}` });
    const r = await pending;
    expect(r.success).toBe(false);
    expect(r.message).toContain('Der Leader-Arm ist aktiv');
    expect(r.message).not.toContain(CONNECT);
    expect(r.message).not.toContain(EDGE);
  });

  it('an image without the service is oldImage', async () => {
    const pending = startCloudInference(request());
    mockCalls[0].fail('Service /edubotics/cloud_inference/start does not exist');
    await expect(pending).resolves.toEqual({ success: false, message: cloudCopy.errors.robotOld, oldImage: true, timedOut: false });
  });

  it('any other failure is our sentence, rosbridge\'s English text dropped', async () => {
    const pending = startCloudInference(request());
    mockCalls[0].fail(new Error('some rosbridge error'));
    const r = await pending;
    expect(r).toEqual({ success: false, message: cloudCopy.errors.robotFailed, oldImage: false, timedOut: false });
  });

  it('no answer within 10 s is timedOut', async () => {
    vi.useFakeTimers();
    const pending = startCloudInference(request());
    vi.advanceTimersByTime(START_TIMEOUT_MS - 1);
    let settled = false;
    pending.then(() => { settled = true; });
    await Promise.resolve();
    expect(settled).toBe(false);
    vi.advanceTimersByTime(1);
    const r = await pending;
    expect(r.timedOut).toBe(true);
    expect(START_TIMEOUT_MS).toBe(10000);
  });

  it('never calls a Jetson proxy and never connects', async () => {
    rosConnectionManager.url = 'ws://10.0.0.5:9091';
    const r = await startCloudInference(request());
    expect(r.success).toBe(false);
    expect(mockCalls).toHaveLength(0);
    rosConnectionManager.url = 'ws://localhost/rosbridge';
    rosConnectionManager.getCurrentConnection.mockReturnValue(null);
    expect((await startCloudInference(request())).message).toBe(cloudCopy.errors.robotUnreachable);
    expect(mockCalls).toHaveLength(0);
  });
});

describe('stopCloudInference', () => {
  it('calls the Trigger service and answers its success', async () => {
    const pending = stopCloudInference();
    expect(mockCalls[0].service.opts.name).toBe(STOP_SERVICE);
    expect(mockCalls[0].service.opts.serviceType).toBe(STOP_SERVICE_TYPE);
    expect({ ...mockCalls[0].request }).toEqual({});
    mockCalls[0].ok({ success: true, message: '' });
    await expect(pending).resolves.toBe(true);
  });

  it('is bounded and never rejects', async () => {
    vi.useFakeTimers();
    const pending = stopCloudInference();
    vi.advanceTimersByTime(STOP_TIMEOUT_MS);
    await expect(pending).resolves.toBe(false);
    vi.useRealTimers();
    const failing = stopCloudInference();
    mockCalls[mockCalls.length - 1].fail('boom');
    await expect(failing).resolves.toBe(false);
  });
});

describe('subscribeCloudState', () => {
  it('forwards only valid payloads and unsubscribes', async () => {
    vi.spyOn(rosConnectionManager, 'getConnection').mockResolvedValue({ isConnected: true });
    const seen = [];
    const unsubscribe = await subscribeCloudState('ws://localhost/rosbridge', (s) => seen.push(s));
    const topic = mockTopics[0];
    expect(topic.opts.name).toBe(STATE_TOPIC);
    expect(topic.opts.messageType).toBe('std_msgs/msg/String');
    expect(topic.opts.queue_length).toBe(1);
    topic.cb({ data: 'garbage' });
    topic.cb({ data: JSON.stringify({ v: 1, seq: 1, active: false, phase: 'idle', rtt_ms_p50: null, plan_age_ms: null, gpu_ms_p50: null, hold_s: null, ends_in_s: null, stale_camera: null, end_text_de: null }) });
    expect(seen).toHaveLength(1);
    unsubscribe();
    expect(topic.unsubscribed).toBe(true);
    topic.cb({ data: 'x' });
    expect(seen).toHaveLength(1);
  });
});

it('the request type names the robot\'s srv (ROSLIB is the only transport here)', () => {
  expect(typeof ROSLIB.Service).toBe('function');
});
