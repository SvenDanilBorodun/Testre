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
import { Led, Lichtband, useReducedMotion } from './sucher';
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
import Icon from './icons/Icon';
import ContinueTrainingButton from './ContinueTrainingButton';

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
  queued: { tone: 'amber', label: 'Wird eingereiht', icon: 'hourglass' },
  running: { tone: 'accent', label: 'Läuft', icon: 'loading', spin: true },
  succeeded: { tone: 'success', label: 'Erfolgreich', icon: 'checkCircle' },
  failed: { tone: 'danger', label: 'Fehlgeschlagen', icon: 'failed' },
  canceled: { tone: 'neutral', label: 'Abgebrochen', icon: 'cancel' },
};

// The job state as a glass chip on the dark screen (Sucher).
const STATUS_LED = { amber: 'warn', accent: 'sig', success: 'ok', danger: 'rec', neutral: 'off' };

function StatusPill({ status }) {
  const ui = STATUS_UI[status] || STATUS_UI.queued;
  return (
    <span className="su-trn-chip">
      <Icon name={ui.icon} size={13} className={clsx(ui.spin && 'animate-spin', `su-trn-chip__icon--${STATUS_LED[ui.tone] || 'off'}`)} />
      {ui.label}
    </span>
  );
}

function LiveChip({ isRealtime, ageSeconds }) {
  const stale = ageSeconds != null && ageSeconds > STALE_AFTER_SECONDS;
  const ageStr = formatAge(ageSeconds);
  if (stale) {
    return (
      <span
        className="su-trn-live su-trn-live--stale"
        title="Seit einer Weile kein neuer Fortschritt — Training läuft evtl. in einer ruhigen Phase oder die Verbindung ist langsam."
      >
        <Icon name="hourglass" size={12} />
        Letztes Update {ageStr}
      </span>
    );
  }
  return (
    <span className="su-trn-live">
      <Led tone="ok" pulse="breathe" size={7} />
      {isRealtime ? 'Live' : 'Aktiv'}
      {ageStr ? <span className="su-trn-live__age">· {ageStr}</span> : null}
    </span>
  );
}

function HeroStat({ label, value, sub, tone }) {
  return (
    <div className="min-w-0">
      <div className="su-lab">{label}</div>
      <div
        className={clsx(
          'su-dot su-trn-stat truncate',
          tone === 'success' && 'su-trn-stat--ok',
          tone === 'danger' && 'su-trn-stat--rec',
        )}
      >
        {value}
      </div>
      {sub ? <div className="text-[11px] text-[var(--su-scr-ink-3)] mt-1 truncate">{sub}</div> : null}
    </div>
  );
}

function EmptyState() {
  return (
    <div className="su-trn-empty">
      <div className="flex items-center gap-2 text-[var(--su-sig-on-dark)] font-semibold text-base shrink-0">
        <Icon name="cloud" size={22} />
        Cloud-Training
      </div>
      <div className="text-sm text-[var(--su-scr-ink-2)] leading-relaxed space-y-2">
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
    <div className="su-trn-tip">
      <div>Schritt {formatStep(p.s)}</div>
      <div className="text-[var(--su-sig-on-dark)]">Loss {formatLoss(p.l)}</div>
    </div>
  );
}

function PulseDot(props) {
  const { cx, cy } = props;
  if (cx == null || cy == null) return null;
  // M13: the newest point glows (static).
  return (
    <circle
      cx={cx}
      cy={cy}
      r={6}
      fill="#FF5B1F"
      stroke="#FFFFFF"
      strokeWidth={2}
      style={{ filter: 'drop-shadow(0 0 8px rgba(255,91,31,.9))' }}
    />
  );
}

// Secondary loss chart. Shows a friendly placeholder until there are >= 2 points
// so the hero (numbers + progress) is never blocked by a missing curve.
function LossChart({ history, totalSteps, currentStep, currentLoss, isRunning, isActive }) {
  const reduced = useReducedMotion();
  const hasChart = history.length >= 2;
  const lastPoint = history.length > 0 ? history[history.length - 1] : null;

  if (!hasChart) {
    return (
      <div className="su-trn-chart su-trn-chart--empty">
        <div className="flex items-center gap-2.5 text-[var(--su-scr-ink-2)] text-sm">
          <Led tone="sig" pulse="breathe" />
          {isActive
            ? 'Die Loss-Kurve erscheint nach den ersten Trainingsschritten…'
            : 'Keine Verlaufsdaten aufgezeichnet.'}
        </div>
      </div>
    );
  }

  return (
    <div
      className="su-trn-chart"
      role="img"
      aria-label={`Loss-Kurve: Schritt ${formatStep(currentStep)} von ${formatStep(
        totalSteps,
      )}, aktueller Loss ${formatLoss(currentLoss)}`}
    >
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={history} margin={{ top: 16, right: 16, bottom: 8, left: 8 }}>
          <defs>
            <linearGradient id="lossFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#FF5B1F" stopOpacity={0.35} />
              <stop offset="100%" stopColor="#FF5B1F" stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid stroke="#34302B" />
          <XAxis
            dataKey="s"
            type="number"
            domain={['dataMin', totalSteps > 0 ? totalSteps : 'dataMax']}
            tickFormatter={compactAxis}
            stroke="#9C9488"
            tick={{ fontSize: 11, fill: '#9C9488' }}
            tickLine={false}
            axisLine={false}
          />
          <YAxis
            dataKey="l"
            stroke="#9C9488"
            tick={{ fontSize: 11, fill: '#9C9488' }}
            tickLine={false}
            axisLine={false}
            width={48}
            tickFormatter={(v) => formatLoss(v)}
          />
          <Tooltip
            content={<ChartTooltip />}
            cursor={{ stroke: '#FF5B1F', strokeWidth: 1, strokeDasharray: '2 4' }}
          />
          <Area
            type="monotone"
            dataKey="l"
            stroke="#FF5B1F"
            strokeWidth={2.5}
            fill="url(#lossFill)"
            isAnimationActive={!isActive && !reduced}
            animationDuration={400}
            animationEasing="ease-out"
            dot={false}
            activeDot={{ r: 4, fill: '#FF5B1F', stroke: 'white', strokeWidth: 2 }}
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

export default function TrainingLiveChart({ jobs, isRealtime, remaining, onContinued }) {
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

  // While no GPU server is training yet there is nothing to fill — show an
  // indeterminate "starting" bar instead of the old bare "Warte auf GPU-Worker"
  // spinner. A managed (RunPod) row knows its total_steps from the start, so
  // `queued` counts as starting too: no attempt has written `running` yet, or
  // the training was requeued after a lost GPU (RunPod spec §4.11).
  const starting = isActive && (job.status === 'queued' || totalSteps <= 0);

  // The API's German line for a training between GPU servers or being
  // published (`trainings.status_note`, migration 049). Active jobs only: a
  // finished job's last note would describe a state that is over — a finished,
  // capped row's note (S23/S29) is rendered by ContinueTrainingButton instead.
  const statusNote = isActive && typeof job.status_note === 'string' && job.status_note.trim()
    ? job.status_note
    : null;

  // A capped parent shows its real percentage, never „100 %" (owner E32, review m2).
  const heroPct = isSucceeded && !Number.isInteger(job.capped_step) ? 100 : pct;

  // Middle hero stat: remaining time while running, else final progress %.
  const middle = isRunning
    ? { label: 'Verbleibt (≈)', value: eta == null ? '—' : formatDuration(eta) }
    : { label: 'Fortschritt', value: `${Math.round(heroPct)}%` };

  return (
    <div className="su-trn-live-chart">
      {/* Header: status + model/dataset + live age + deselect */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3 min-w-0">
          <StatusPill status={job.status} />
          <div className="min-w-0 flex items-baseline gap-x-3 gap-y-0.5 flex-wrap">
            <div className="flex items-center gap-2 min-w-0">
              {isSucceeded ? (
                <a
                  href={`https://huggingface.co/${job.model_name}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="su-trn-repo su-trn-repo--link truncate inline-flex items-center gap-1"
                  title={job.model_name}
                >
                  {job.model_name}
                  <Icon name="externalLink" size={14} className="shrink-0" />
                </a>
              ) : (
                <span className="su-trn-repo truncate" title={job.model_name}>
                  {job.model_name}
                </span>
              )}
              <span className="su-lab shrink-0">{job.model_type}</span>
            </div>
            <div className="text-[13px] text-[var(--su-scr-ink-2)] truncate" title={job.dataset_name}>
              Datensatz · {job.dataset_name}
            </div>
          </div>
        </div>
        <div className="flex items-center gap-3 shrink-0">
          {isActive && <LiveChip isRealtime={isRealtime} ageSeconds={ageSeconds} />}
          {selectedId === job.id && (
            <button
              type="button"
              onClick={() => dispatch(setSelectedTrainingId(null))}
              className="text-[12px] text-[var(--su-scr-ink-2)] hover:text-[var(--su-scr-ink)] underline"
              title="Auswahl aufheben — automatisch das neueste Training zeigen"
            >
              Auswahl aufheben
            </button>
          )}
        </div>
      </div>

      <div className="su-trn-body">
        {/* HERO: progress is the headline */}
        <div className="su-trn-hero">
          <div className="su-lab">
            {starting ? 'Status' : 'Fortschritt'}
          </div>
          {starting ? (
            <div className="flex items-center gap-2 mt-2 text-[var(--su-scr-ink)] font-semibold text-lg">
              <Icon name="loading" size={18} className="animate-spin text-[var(--su-sig-on-dark)]" />
              GPU-Worker startet…
            </div>
          ) : (
            <div className="su-dot su-trn-pct">
              {Math.round(heroPct)}
              <span>%</span>
            </div>
          )}

          <div className="su-lab mt-4">Schritt</div>
          <div className="su-dot su-trn-stepnum">
            {formatStep(currentStep)}
            {totalSteps > 0 && <span> / {formatStep(totalSteps)}</span>}
          </div>

          {/* Progress — the light band (static); indeterminate while starting */}
          {starting ? (
            <div className="su-trn-band su-trn-band--starting" />
          ) : (
            <Lichtband
              className={clsx('su-trn-band', `su-trn-band--${isFailed ? 'failed' : isCanceled ? 'canceled' : isSucceeded ? 'done' : 'run'}`)}
              count={30}
              frac={heroPct / 100}
              color={isFailed ? '#FF3B30' : isCanceled ? '#8A867E' : isSucceeded ? '#3DBE5A' : '#FF5B1F'}
              label="Fortschritt"
            />
          )}

          {/* Hero stats */}
          <div className="grid grid-cols-3 gap-3 md:gap-5 mt-5">
            <HeroStat
              label="Loss"
              value={formatLoss(currentLoss)}
              tone={isSucceeded ? 'success' : undefined}
            />
            <HeroStat label={middle.label} value={middle.value} />
            <HeroStat label="Vergangen" value={formatDuration(elapsed)} />
          </div>

          {isSucceeded && (
            <ContinueTrainingButton job={job} jobs={jobs} remaining={remaining} onContinued={onContinued} />
          )}

          {statusNote && (
            <p className="su-trn-note" role="status">
              {statusNote}
            </p>
          )}
        </div>

        {/* Secondary: loss curve */}
        <div className="min-w-0">
          <div className="flex items-center gap-2 mb-2 su-lab">
            <Icon name="zap" size={12} className="text-[var(--su-sig-on-dark)]" />
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
      </div>

      {/* Error tail */}
      {isFailed && job.error_message && (
        <details className="su-trn-error text-xs">
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
