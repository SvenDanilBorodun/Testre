// Unit tests for the pure cloud-training live-view helpers. No React / recharts.
import {
  pickSelectedJob,
  estimateEtaSeconds,
  progressPct,
  progressAgeSeconds,
  elapsedSeconds,
  formatAge,
  formatDuration,
  formatLoss,
  formatStep,
} from '../trainingLiveChartUtils';

const T0 = Date.UTC(2026, 5, 15, 12, 0, 0); // ms epoch baseline

function job(over = {}) {
  return {
    id: 1,
    status: 'running',
    requested_at: new Date(T0).toISOString(),
    total_steps: 50000,
    current_step: 10000,
    ...over,
  };
}

describe('pickSelectedJob', () => {
  test('returns null for empty input', () => {
    expect(pickSelectedJob([], null)).toBeNull();
    expect(pickSelectedJob(null, 5)).toBeNull();
  });

  test('honours an explicit selectedId when it exists', () => {
    const jobs = [job({ id: 1, status: 'succeeded' }), job({ id: 2, status: 'running' })];
    expect(pickSelectedJob(jobs, 1).id).toBe(1);
  });

  test('falls back to newest active when selectedId is missing', () => {
    const jobs = [
      job({ id: 1, status: 'succeeded', requested_at: new Date(T0).toISOString() }),
      job({ id: 2, status: 'running', requested_at: new Date(T0 + 1000).toISOString() }),
      job({ id: 3, status: 'queued', requested_at: new Date(T0 + 2000).toISOString() }),
    ];
    // newest active = queued@T0+2000
    expect(pickSelectedJob(jobs, 999).id).toBe(3);
  });

  test('falls back to most recent terminated when nothing is active', () => {
    const jobs = [
      job({ id: 1, status: 'succeeded', requested_at: new Date(T0).toISOString() }),
      job({ id: 2, status: 'failed', requested_at: new Date(T0 + 5000).toISOString() }),
    ];
    expect(pickSelectedJob(jobs, null).id).toBe(2);
  });
});

describe('progressPct', () => {
  test('clamps and handles zero total', () => {
    expect(progressPct(job({ current_step: 25000, total_steps: 50000 }))).toBe(50);
    expect(progressPct(job({ current_step: 99999, total_steps: 0 }))).toBe(0);
    expect(progressPct(job({ current_step: 60000, total_steps: 50000 }))).toBe(100);
  });
});

describe('estimateEtaSeconds', () => {
  test('uses recent throughput from history timestamps', () => {
    // 1000 steps over 100s in the recent window => 10 steps/s.
    const history = [
      { s: 9000, l: 0.2, t: T0 },
      { s: 10000, l: 0.1, t: T0 + 100000 },
    ];
    const j = job({ current_step: 10000, total_steps: 50000 });
    const eta = estimateEtaSeconds(j, history, 200);
    // remaining 40000 steps / 10 steps/s = 4000s
    expect(eta).toBeCloseTo(4000, 0);
  });

  test('falls back to whole-run average when history lacks timestamps', () => {
    const j = job({ current_step: 10000, total_steps: 50000 });
    // elapsed 100s, 10000 steps done => (40000/10000)*100 = 400s
    expect(estimateEtaSeconds(j, [], 100)).toBeCloseTo(400, 0);
  });

  test('returns null for non-running or no signal', () => {
    expect(estimateEtaSeconds(job({ status: 'succeeded' }), [], 100)).toBeNull();
    expect(estimateEtaSeconds(job({ current_step: 0 }), [], 100)).toBeNull();
    expect(estimateEtaSeconds(job({ current_step: 10, total_steps: 50000 }), [], 0)).toBeNull();
  });
});

describe('progressAgeSeconds', () => {
  test('returns seconds since last_progress_at for active jobs', () => {
    const j = job({ last_progress_at: new Date(T0).toISOString() });
    expect(progressAgeSeconds(j, T0 + 45000)).toBeCloseTo(45, 3);
  });

  test('null for terminal jobs or missing timestamp', () => {
    expect(progressAgeSeconds(job({ status: 'succeeded', last_progress_at: new Date(T0).toISOString() }), T0)).toBeNull();
    expect(progressAgeSeconds(job({ last_progress_at: null }), T0)).toBeNull();
  });
});

describe('elapsedSeconds', () => {
  test('counts to now while running, to terminated_at when finished', () => {
    expect(elapsedSeconds(job(), T0 + 60000)).toBeCloseTo(60, 3);
    const done = job({ status: 'succeeded', terminated_at: new Date(T0 + 30000).toISOString() });
    expect(elapsedSeconds(done, T0 + 99999999)).toBeCloseTo(30, 3);
  });
});

describe('formatters', () => {
  test('formatAge buckets', () => {
    expect(formatAge(2)).toBe('gerade eben');
    expect(formatAge(20)).toBe('vor 20 s');
    expect(formatAge(120)).toBe('vor 2 min');
    expect(formatAge(null)).toBeNull();
  });

  test('formatDuration h:mm:ss and mm:ss', () => {
    expect(formatDuration(65)).toBe('01:05');
    expect(formatDuration(3661)).toBe('01:01:01');
    expect(formatDuration(-1)).toBe('—');
  });

  test('formatLoss precision + exponent edges', () => {
    expect(formatLoss(0.123449)).toBe('0.1234');
    expect(formatLoss(0.12345)).toBe('0.1235');
    expect(formatLoss(0)).toBe('0');
    expect(formatLoss(0.0000001)).toBe('1.00e-7');
    expect(formatLoss(null)).toBe('—');
  });

  test('formatStep localizes and handles null', () => {
    expect(formatStep(null)).toBe('—');
    expect(typeof formatStep(12000)).toBe('string');
  });
});
