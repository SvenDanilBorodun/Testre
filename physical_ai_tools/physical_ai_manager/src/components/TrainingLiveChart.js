import React, { useEffect, useMemo, useState } from 'react';
import { useSelector, useDispatch } from 'react-redux';
import {
  Area,
  AreaChart,
  CartesianGrid,
  ReferenceDot,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import clsx from 'clsx';
import {
  MdCloud,
  MdOpenInNew,
  MdCheckCircle,
  MdError,
  MdCancel,
  MdHourglassEmpty,
  MdRefresh,
} from 'react-icons/md';
import { Pill } from './EbUI';
import { setSelectedTrainingId } from '../features/training/trainingSlice';

// ---------- helpers ----------

const ACTIVE_STATUSES = new Set(['queued', 'running']);
const TERMINAL_STATUSES = new Set(['succeeded', 'failed', 'canceled']);

const ETA_MIN_STEPS = 200;

export function pickSelectedJob(jobs, selectedId) {
  if (!jobs || jobs.length === 0) return null;
  if (selectedId != null) {
    const pinned = jobs.find((j) => j.id === selectedId);
    if (pinned) return pinned;
  }
  const active = jobs
    .filter((j) => ACTIVE_STATUSES.has(j.status))
    .sort((a, b) => new Date(b.requested_at) - new Date(a.requested_at));
  if (active.length > 0) return active[0];
  const terminated = jobs
    .filter((j) => TERMINAL_STATUSES.has(j.status))
    .sort((a, b) => new Date(b.requested_at) - new Date(a.requested_at));
  return terminated[0] || jobs[0];
}

function formatDuration(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return '—';
  const s = Math.round(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const pad = (n) => String(n).padStart(2, '0');
  if (h > 0) return `${pad(h)}:${pad(m)}:${pad(sec)}`;
  return `${pad(m)}:${pad(sec)}`;
}

function formatStep(step) {
  if (step == null) return '—';
  return Number(step).toLocaleString('de-DE');
}

function formatLoss(loss) {
  if (loss == null || !Number.isFinite(loss)) return '—';
  const abs = Math.abs(loss);
  if (abs === 0) return '0';
  if (abs < 0.001 || abs >= 1000) return loss.toExponential(2);
  return loss.toFixed(4);
}

function compactAxis(n) {
  const abs = Math.abs(n);
  if (abs >= 1_000_000) return `${(n / 1_000_000).toFixed(abs >= 10_000_000 ? 0 : 1)}M`;
  if (abs >= 1_000) return `${(n / 1_000).toFixed(abs >= 10_000 ? 0 : 1)}K`;
  return String(n);
}

function useElapsedSeconds(job) {
  const [now, setNow] = useState(() => Date.now());
  const isActive = job && ACTIVE_STATUSES.has(job.status);
  useEffect(() => {
    if (!isActive) return undefined;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [isActive]);
  if (!job?.requested_at) return 0;
  const start = new Date(job.requested_at).getTime();
  const end = job.terminated_at ? new Date(job.terminated_at).getTime() : now;
  return Math.max(0, (end - start) / 1000);
}

// ---------- subcomponents ----------

const STATUS_UI = {
  queued: { tone: 'amber', label: 'Wird eingereiht', Icon: MdHourglassEmpty },
  running: { tone: 'accent', label: 'Live', Icon: MdRefresh, spin: true },
  succeeded: { tone: 'success', label: 'Erfolgreich', Icon: MdCheckCircle },
  failed: { tone: 'danger', label: 'Fehlgeschlagen', Icon: MdError },
  canceled: { tone: 'neutral', label: 'Abgebrochen', Icon: MdCancel },
};

function StatusPill({ status, isRealtime }) {
  const ui = STATUS_UI[status] || STATUS_UI.queued;
  const Icon = ui.Icon;
  return (
    <Pill tone={ui.tone} dot>
      <span className="inline-flex items-center gap-1.5">
        <Icon size={12} className={ui.spin ? 'animate-spin' : ''} />
        {ui.label}
        {status === 'running' && isRealtime && (
          <span className="text-[10px] opacity-70 ml-0.5">· Live</span>
        )}
      </span>
    </Pill>
  );
}

function Stat({ label, value, mono = true, strong = false }) {
  return (
    <div className="min-w-0">
      <div className="text-[10px] font-semibold uppercase tracking-wider text-[var(--ink-3)]">
        {label}
      </div>
      <div
        className={clsx(
          'truncate',
          mono ? 'font-mono' : '',
          strong ? 'text-base font-semibold text-[var(--ink)]' : 'text-sm text-[var(--ink)]',
        )}
      >
        {value}
      </div>
    </div>
  );
}

function EmptyState() {
  // Preserves the exact German copy students saw before, wrapped in the
  // friendlier chart-panel layout.
  return (
    <div className="flex flex-col md:flex-row gap-4 items-start p-4 md:p-6 rounded-[var(--radius)] bg-[var(--accent-wash)] border border-[color:var(--accent)]/20">
      <div className="flex items-center gap-2 text-[var(--accent-ink)] font-semibold text-base shrink-0">
        <MdCloud size={22} />
        Cloud-Training
      </div>
      <div className="text-sm text-[var(--accent-ink)] leading-relaxed space-y-2">
        <p>
          Das Training läuft auf einer Cloud-GPU. Wenn du auf „Training starten"
          klickst, wird der Auftrag an die Cloud gesendet. Der Fortschritt
          erscheint hier live — inklusive Loss-Kurve, Schritten und verbleibender
          Zeit.
        </p>
        <p>
          Abgeschlossene Modelle werden automatisch auf HuggingFace Hub
          hochgeladen und können anschließend für die Inferenz verwendet werden.
        </p>
      </div>
    </div>
  );
}

function WaitingForWorker() {
  return (
    <div className="h-[280px] rounded-[var(--radius)] chart-grid flex items-center justify-center">
      <div className="flex items-center gap-3 text-[var(--ink-3)]">
        <span className="w-2.5 h-2.5 rounded-full bg-[var(--accent)] animate-pulse" />
        <span className="text-sm">Warte auf GPU-Worker…</span>
      </div>
    </div>
  );
}

function ChartTooltip({ active, payload }) {
  if (!active || !payload?.length) return null;
  const p = payload[0].payload;
  return (
    <div className="rounded-[var(--radius-sm)] bg-white border border-[var(--line)] shadow-soft px-3 py-2 text-xs">
      <div className="font-mono text-[var(--ink)]">
        Schritt {formatStep(p.s)}
      </div>
      <div className="font-mono text-[var(--accent-ink)]">
        Loss {formatLoss(p.l)}
      </div>
    </div>
  );
}

function PulseDot(props) {
  // Animated pulse at the most recent point while the run is live.
  const { cx, cy } = props;
  if (cx == null || cy == null) return null;
  return (
    <g>
      <circle
        cx={cx}
        cy={cy}
        r={7}
        fill="var(--accent)"
        opacity={0.25}
        className="animate-ping"
      />
      <circle cx={cx} cy={cy} r={4} fill="var(--accent)" />
    </g>
  );
}

// ---------- main component ----------

export default function TrainingLiveChart({ jobs, isRealtime }) {
  const dispatch = useDispatch();
  const selectedId = useSelector((s) => s.training.selectedTrainingId);
  const job = useMemo(() => pickSelectedJob(jobs, selectedId), [jobs, selectedId]);

  const elapsed = useElapsedSeconds(job);

  if (!job) return <EmptyState />;

  const history = Array.isArray(job.loss_history) ? job.loss_history : [];
  const totalSteps = job.total_steps || 0;
  const currentStep = job.current_step || 0;
  const pct = totalSteps > 0 ? Math.min(100, (currentStep / totalSteps) * 100) : 0;
  const isActive = ACTIVE_STATUSES.has(job.status);
  const isRunning = job.status === 'running';
  const isSucceeded = job.status === 'succeeded';
  const isFailed = job.status === 'failed';
  const isCanceled = job.status === 'canceled';

  const eta =
    isRunning && currentStep >= ETA_MIN_STEPS && totalSteps > 0 && elapsed > 0
      ? ((totalSteps - currentStep) / currentStep) * elapsed
      : null;

  const lastPoint = history.length > 0 ? history[history.length - 1] : null;
  const hasChart = history.length >= 2;

  return (
    <div className="space-y-4">
      {/* Header: status pill + model/dataset + deselect */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3 min-w-0">
          <StatusPill status={job.status} isRealtime={isRealtime} />
          <div className="min-w-0">
            <div className="flex items-center gap-2 text-sm text-[var(--ink)] min-w-0">
              {isSucceeded ? (
                <a
                  href={`https://huggingface.co/${job.model_name}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="font-semibold text-[var(--accent-ink)] hover:underline truncate inline-flex items-center gap-1"
                  title={job.model_name}
                >
                  {job.model_name}
                  <MdOpenInNew size={14} className="shrink-0" />
                </a>
              ) : (
                <span className="font-semibold text-[var(--ink)] truncate" title={job.model_name}>
                  {job.model_name}
                </span>
              )}
              <span className="text-[var(--ink-3)] text-[11px] font-mono shrink-0">
                {job.model_type}
              </span>
            </div>
            <div className="text-[11px] text-[var(--ink-3)] truncate" title={job.dataset_name}>
              Datensatz · {job.dataset_name}
            </div>
          </div>
        </div>
        {selectedId === job.id && (
          <button
            onClick={() => dispatch(setSelectedTrainingId(null))}
            className="text-[11px] text-[var(--ink-3)] hover:text-[var(--ink)] underline"
            title="Auswahl aufheben — automatisch das neueste Training zeigen"
          >
            Auswahl aufheben
          </button>
        )}
      </div>

      {/* Stats strip */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 md:gap-5">
        <Stat
          label="Schritt"
          value={
            <>
              <span className="font-semibold">{formatStep(currentStep)}</span>
              {totalSteps > 0 && (
                <span className="text-[var(--ink-3)]"> / {formatStep(totalSteps)}</span>
              )}
            </>
          }
          strong
        />
        <Stat label="Loss" value={formatLoss(job.current_loss ?? lastPoint?.l)} strong />
        <Stat label="Vergangen" value={formatDuration(elapsed)} />
        <Stat
          label={isRunning ? 'Verbleibt (≈)' : 'Fortschritt'}
          value={
            isRunning
              ? eta == null
                ? '—'
                : `${formatDuration(eta)}`
              : `${Math.round(pct)}%`
          }
        />
      </div>

      {/* Chart area */}
      {!hasChart && job.status === 'queued' ? (
        <WaitingForWorker />
      ) : !hasChart ? (
        <WaitingForWorker />
      ) : (
        <div
          className="h-[280px] rounded-[var(--radius)] chart-grid overflow-hidden"
          role="img"
          aria-label={`Loss-Kurve: Schritt ${formatStep(currentStep)} von ${formatStep(
            totalSteps,
          )}, aktueller Loss ${formatLoss(job.current_loss)}`}
        >
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart
              data={history}
              margin={{ top: 16, right: 16, bottom: 8, left: 8 }}
            >
              <defs>
                <linearGradient id="lossFill" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="var(--accent)" stopOpacity={0.32} />
                  <stop offset="100%" stopColor="var(--accent)" stopOpacity={0.02} />
                </linearGradient>
              </defs>
              <CartesianGrid
                strokeDasharray="2 6"
                stroke="rgba(14,20,21,0.08)"
                vertical={false}
              />
              <XAxis
                dataKey="s"
                type="number"
                domain={['dataMin', totalSteps > 0 ? totalSteps : 'dataMax']}
                tickFormatter={compactAxis}
                stroke="var(--ink-3)"
                tick={{ fontSize: 11, fill: 'var(--ink-3)' }}
                tickLine={false}
                axisLine={false}
              />
              <YAxis
                dataKey="l"
                stroke="var(--ink-3)"
                tick={{ fontSize: 11, fill: 'var(--ink-3)' }}
                tickLine={false}
                axisLine={false}
                width={48}
                tickFormatter={(v) => formatLoss(v)}
              />
              <Tooltip content={<ChartTooltip />} cursor={{ stroke: 'var(--accent)', strokeWidth: 1, strokeDasharray: '2 4' }} />
              <Area
                type="monotone"
                dataKey="l"
                stroke="var(--accent)"
                strokeWidth={2}
                fill="url(#lossFill)"
                isAnimationActive={!isActive}
                animationDuration={400}
                dot={false}
                activeDot={{ r: 4, fill: 'var(--accent)', stroke: 'white', strokeWidth: 2 }}
              />
              {isRunning && lastPoint && (
                <ReferenceDot
                  x={lastPoint.s}
                  y={lastPoint.l}
                  shape={<PulseDot />}
                  ifOverflow="extendDomain"
                />
              )}
            </AreaChart>
          </ResponsiveContainer>
        </div>
      )}

      {/* Progress bar */}
      {totalSteps > 0 && (
        <div>
          <div className="w-full h-2 bg-[var(--bg-sunk)] rounded-full overflow-hidden">
            <div
              className="h-full rounded-full transition-all duration-500"
              style={{
                width: `${pct}%`,
                background: isFailed
                  ? 'var(--danger)'
                  : isCanceled
                    ? 'var(--ink-4)'
                    : 'var(--accent)',
              }}
            />
          </div>
          <div className="mt-1 flex items-center justify-between text-[11px] font-mono text-[var(--ink-3)]">
            <span>
              {formatStep(currentStep)} / {formatStep(totalSteps)} Schritte
            </span>
            <span>{Math.round(pct)}%</span>
          </div>
        </div>
      )}

      {/* Error tail */}
      {job.status === 'failed' && job.error_message && (
        <details className="text-xs text-[color:var(--danger)] bg-[var(--danger-wash)] border border-[color:var(--danger)]/30 rounded-[var(--radius-sm)] p-3">
          <summary className="cursor-pointer font-medium">
            Fehlermeldung anzeigen
          </summary>
          <pre className="mt-2 whitespace-pre-wrap break-all text-[11px] font-mono leading-snug">
            {job.error_message}
          </pre>
          <div className="mt-2 italic opacity-80">
            Das Trainingsguthaben wurde erstattet.
          </div>
        </details>
      )}
    </div>
  );
}
