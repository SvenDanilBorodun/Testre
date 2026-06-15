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
  MdBolt,
} from 'react-icons/md';
import { Pill } from './EbUI';
import { setSelectedTrainingId } from '../features/training/trainingSlice';
import {
  ACTIVE_STATUSES,
  STALE_AFTER_SECONDS,
  pickSelectedJob,
  formatDuration,
  formatStep,
  formatLoss,
  compactAxis,
  elapsedSeconds,
  progressAgeSeconds,
  formatAge,
  progressPct,
  estimateEtaSeconds,
} from './trainingLiveChartUtils';

// Re-exported so existing importers (TrainingPage + the gate test) keep working
// after the pure helpers moved into trainingLiveChartUtils.
export { pickSelectedJob };

// ---------- live clock ----------

// Ticks once per second while the job is active so the elapsed timer and the
// "Aktualisiert vor Xs" age chip stay current without a re-render storm.
function useNow(active) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return undefined;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [active]);
  return now;
}

// ---------- small presentational bits ----------

const STATUS_UI = {
  queued: { tone: 'amber', label: 'Wird eingereiht', Icon: MdHourglassEmpty },
  running: { tone: 'accent', label: 'Läuft', Icon: MdRefresh, spin: true },
  succeeded: { tone: 'success', label: 'Erfolgreich', Icon: MdCheckCircle },
  failed: { tone: 'danger', label: 'Fehlgeschlagen', Icon: MdError },
  canceled: { tone: 'neutral', label: 'Abgebrochen', Icon: MdCancel },
};

function StatusPill({ status }) {
  const ui = STATUS_UI[status] || STATUS_UI.queued;
  const Icon = ui.Icon;
  return (
    <Pill tone={ui.tone} dot>
      <span className="inline-flex items-center gap-1.5">
        <Icon size={12} className={ui.spin ? 'animate-spin' : ''} />
        {ui.label}
      </span>
    </Pill>
  );
}

// "Live · vor 3s" while healthy, an amber "wirkt langsam" hint when the worker
// has gone quiet for a while. Only rendered for active jobs.
function LiveChip({ isRealtime, ageSeconds }) {
  const stale = ageSeconds != null && ageSeconds > STALE_AFTER_SECONDS;
  const ageStr = formatAge(ageSeconds);
  if (stale) {
    return (
      <span
        className="inline-flex items-center gap-1.5 text-[11px] font-medium px-2.5 py-1 rounded-full bg-[var(--amber-wash)] text-[color:var(--amber)]"
        title="Seit einer Weile kein neuer Fortschritt — Training läuft evtl. in einer ruhigen Phase oder die Verbindung ist langsam."
      >
        <MdHourglassEmpty size={12} />
        Letztes Update {ageStr}
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1.5 text-[11px] font-medium px-2.5 py-1 rounded-full bg-[var(--accent-wash)] text-[var(--accent-ink)]">
      <span className="w-1.5 h-1.5 rounded-full bg-[var(--accent)] animate-pulse" />
      {isRealtime ? 'Live' : 'Aktiv'}
      {ageStr ? <span className="opacity-70">· {ageStr}</span> : null}
    </span>
  );
}

// Big metric in the hero strip.
function HeroStat({ label, value, sub, tone }) {
  return (
    <div className="min-w-0">
      <div className="text-[10px] font-semibold uppercase tracking-wider text-[var(--ink-3)]">
        {label}
      </div>
      <div
        className={clsx(
          'font-mono font-semibold leading-none mt-1.5 text-[22px] md:text-[26px] truncate',
          tone === 'success' && 'text-[color:var(--success)]',
          tone === 'danger' && 'text-[color:var(--danger)]',
          !tone && 'text-[var(--ink)]',
        )}
      >
        {value}
      </div>
      {sub ? <div className="text-[11px] text-[var(--ink-3)] mt-1 truncate">{sub}</div> : null}
    </div>
  );
}

function EmptyState() {
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

function ChartTooltip({ active, payload }) {
  if (!active || !payload?.length) return null;
  const p = payload[0].payload;
  return (
    <div className="rounded-[var(--radius-sm)] bg-white border border-[var(--line)] shadow-soft px-3 py-2 text-xs">
      <div className="font-mono text-[var(--ink)]">Schritt {formatStep(p.s)}</div>
      <div className="font-mono text-[var(--accent-ink)]">Loss {formatLoss(p.l)}</div>
    </div>
  );
}

function PulseDot(props) {
  const { cx, cy } = props;
  if (cx == null || cy == null) return null;
  return (
    <g>
      <circle cx={cx} cy={cy} r={7} fill="var(--accent)" opacity={0.25} className="animate-ping" />
      <circle cx={cx} cy={cy} r={4} fill="var(--accent)" />
    </g>
  );
}

// Secondary loss chart. Shows a friendly placeholder until there are >= 2 points
// so the hero (numbers + progress) is never blocked by a missing curve.
function LossChart({ history, totalSteps, currentStep, currentLoss, isRunning, isActive }) {
  const hasChart = history.length >= 2;
  const lastPoint = history.length > 0 ? history[history.length - 1] : null;

  if (!hasChart) {
    return (
      <div className="h-[150px] rounded-[var(--radius)] chart-grid flex items-center justify-center">
        <div className="flex items-center gap-2.5 text-[var(--ink-3)] text-sm">
          <span className="w-2.5 h-2.5 rounded-full bg-[var(--accent)] animate-pulse" />
          {isActive
            ? 'Die Loss-Kurve erscheint nach den ersten Trainingsschritten…'
            : 'Keine Verlaufsdaten aufgezeichnet.'}
        </div>
      </div>
    );
  }

  return (
    <div
      className="h-[200px] rounded-[var(--radius)] chart-grid overflow-hidden"
      role="img"
      aria-label={`Loss-Kurve: Schritt ${formatStep(currentStep)} von ${formatStep(
        totalSteps,
      )}, aktueller Loss ${formatLoss(currentLoss)}`}
    >
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={history} margin={{ top: 16, right: 16, bottom: 8, left: 8 }}>
          <defs>
            <linearGradient id="lossFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="var(--accent)" stopOpacity={0.32} />
              <stop offset="100%" stopColor="var(--accent)" stopOpacity={0.02} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="2 6" stroke="rgba(14,20,21,0.08)" vertical={false} />
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
          <Tooltip
            content={<ChartTooltip />}
            cursor={{ stroke: 'var(--accent)', strokeWidth: 1, strokeDasharray: '2 4' }}
          />
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
            <ReferenceDot x={lastPoint.s} y={lastPoint.l} shape={<PulseDot />} ifOverflow="extendDomain" />
          )}
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

// ---------- main component ----------

export default function TrainingLiveChart({ jobs, isRealtime }) {
  const dispatch = useDispatch();
  const selectedId = useSelector((s) => s.training.selectedTrainingId);
  const job = useMemo(() => pickSelectedJob(jobs, selectedId), [jobs, selectedId]);

  const isActive = job ? ACTIVE_STATUSES.has(job.status) : false;
  const now = useNow(isActive);

  if (!job) return <EmptyState />;

  const history = Array.isArray(job.loss_history) ? job.loss_history : [];
  const totalSteps = job.total_steps || 0;
  const currentStep = job.current_step || 0;
  const lastPoint = history.length > 0 ? history[history.length - 1] : null;
  const currentLoss = job.current_loss ?? lastPoint?.l ?? null;

  const isRunning = job.status === 'running';
  const isSucceeded = job.status === 'succeeded';
  const isFailed = job.status === 'failed';
  const isCanceled = job.status === 'canceled';

  const elapsed = elapsedSeconds(job, now);
  const ageSeconds = progressAgeSeconds(job, now);
  const eta = estimateEtaSeconds(job, history, elapsed);
  const pct = progressPct(job);

  // While running with no step info yet (Modal cold-start + dataset download),
  // there is nothing to fill — show an indeterminate "starting" bar instead of
  // the old bare "Warte auf GPU-Worker" spinner.
  const starting = isActive && totalSteps <= 0;

  const barTone = isFailed
    ? 'var(--danger)'
    : isCanceled
      ? 'var(--ink-4)'
      : isSucceeded
        ? 'var(--success)'
        : 'var(--accent)';

  const heroPct = isSucceeded ? 100 : pct;

  // Middle hero stat: remaining time while running, else final progress %.
  const middle = isRunning
    ? { label: 'Verbleibt (≈)', value: eta == null ? '—' : formatDuration(eta) }
    : { label: 'Fortschritt', value: `${Math.round(heroPct)}%` };

  return (
    <div className="space-y-4">
      {/* Header: status + model/dataset + live age + deselect */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3 min-w-0">
          <StatusPill status={job.status} />
          <div className="min-w-0">
            <div className="flex items-center gap-2 text-sm min-w-0">
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
              <span className="text-[var(--ink-3)] text-[11px] font-mono shrink-0">{job.model_type}</span>
            </div>
            <div className="text-[11px] text-[var(--ink-3)] truncate" title={job.dataset_name}>
              Datensatz · {job.dataset_name}
            </div>
          </div>
        </div>
        <div className="flex items-center gap-3 shrink-0">
          {isActive && <LiveChip isRealtime={isRealtime} ageSeconds={ageSeconds} />}
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
      </div>

      {/* HERO: progress is the headline */}
      <div className="rounded-[var(--radius)] border border-[var(--line)] bg-white p-4 md:p-5">
        <div className="flex items-end justify-between gap-4 mb-3">
          <div className="min-w-0">
            <div className="text-[10px] font-semibold uppercase tracking-wider text-[var(--ink-3)]">
              {starting ? 'Status' : 'Fortschritt'}
            </div>
            {starting ? (
              <div className="flex items-center gap-2 mt-1.5 text-[var(--ink)] font-semibold text-lg">
                <MdRefresh size={18} className="animate-spin text-[var(--accent)]" />
                GPU-Worker startet…
              </div>
            ) : (
              <div className="font-mono font-semibold leading-none mt-1 text-[40px] md:text-[48px] text-[var(--ink)]">
                {Math.round(heroPct)}
                <span className="text-[var(--ink-3)] text-[24px] md:text-[28px]">%</span>
              </div>
            )}
          </div>
          <div className="text-right shrink-0">
            <div className="text-[10px] font-semibold uppercase tracking-wider text-[var(--ink-3)]">
              Schritt
            </div>
            <div className="font-mono text-[var(--ink)] mt-1 text-sm md:text-base">
              <span className="font-semibold">{formatStep(currentStep)}</span>
              {totalSteps > 0 && (
                <span className="text-[var(--ink-3)]"> / {formatStep(totalSteps)}</span>
              )}
            </div>
          </div>
        </div>

        {/* Progress bar — determinate when steps are known, indeterminate while starting */}
        <div className="w-full h-3 bg-[var(--bg-sunk)] rounded-full overflow-hidden">
          {starting ? (
            <div className="h-full w-1/3 rounded-full bg-[var(--accent)] animate-pulse" />
          ) : (
            <div
              className="h-full rounded-full transition-[width] duration-500"
              style={{ width: `${heroPct}%`, background: barTone }}
            />
          )}
        </div>

        {/* Hero stats */}
        <div className="grid grid-cols-3 gap-3 md:gap-5 mt-4">
          <HeroStat
            label="Loss"
            value={formatLoss(currentLoss)}
            tone={isSucceeded ? 'success' : undefined}
          />
          <HeroStat label={middle.label} value={middle.value} />
          <HeroStat label="Vergangen" value={formatDuration(elapsed)} />
        </div>
      </div>

      {/* Secondary: loss curve */}
      <div>
        <div className="flex items-center gap-2 mb-2 text-[11px] font-semibold uppercase tracking-wider text-[var(--ink-3)]">
          <MdBolt size={12} className="text-[var(--accent)]" />
          Loss-Verlauf
        </div>
        <LossChart
          history={history}
          totalSteps={totalSteps}
          currentStep={currentStep}
          currentLoss={currentLoss}
          isRunning={isRunning}
          isActive={isActive}
        />
      </div>

      {/* Error tail */}
      {isFailed && job.error_message && (
        <details className="text-xs text-[color:var(--danger)] bg-[var(--danger-wash)] border border-[color:var(--danger)]/30 rounded-[var(--radius-sm)] p-3">
          <summary className="cursor-pointer font-medium">Fehlermeldung anzeigen</summary>
          <pre className="mt-2 whitespace-pre-wrap break-all text-[11px] font-mono leading-snug">
            {job.error_message}
          </pre>
          <div className="mt-2 italic opacity-80">Das Trainingsguthaben wurde erstattet.</div>
        </details>
      )}
    </div>
  );
}
