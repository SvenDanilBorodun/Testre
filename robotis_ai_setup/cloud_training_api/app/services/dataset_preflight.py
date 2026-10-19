"""Dataset preflight at ``POST /trainings/start`` (spec §4.1 step 5, §6 twin rule).

A TWIN of the Modal worker's preflight (``modal_training/training_handler.py``:
``_stat_value``, ``_validate_stats``, ``_preflight_dataset``), so a dataset the
worker would refuse after a GPU was paid for is refused here, before any credit
or GPU. Differences, all deliberate: ``token`` and ``revision`` are passed
through (a managed training reads the dataset anonymously, exactly as its pod
will, at the commit the ``v3.0`` tag points to, E17); the German sentences use
real umlauts (Rule §1; ``twin.translate({ä: ae, …}) == worker`` is asserted by
``robotis_ai_setup/tests/test_dataset_preflight_twin.py`` over one fake-hub
table); the returned dict carries the parsed ``features`` (FR-5); the success
line goes to the log instead of stdout; every refusal is a ``DatasetRefused``
(a ``ValueError``), and the six Hub/file-read failures are a ``_HubFailure``
carrying the stage and the library's exception. The worker's sentences for those
six interpolate the exception's text; the twin keeps them (the twin test) but
``preflight_dataset`` never shows them: it classifies the exception
(``classify_hub_error``) and raises ``HubUnavailable`` (a timeout, a network
error, an HTTP 429 or 5xx, anything unclassified: the route's 502) or a
``DatasetRefused`` naming the cause in German without it (400).

Plus ``resolve_v3_commit``: the ``v3.0`` tag -> commit sha (E17, TR-1).

Stdlib-only at the module top (``huggingface_hub`` is imported inside
``_hub()``), and no app import: the twin test loads this file by path.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from types import SimpleNamespace

logger = logging.getLogger(__name__)

EXPECTED_CODEBASE_VERSION = "v3.0"
MIN_JOINTS = 4
MAX_JOINTS = 20
_REQUIRED_STATS_KEYS = ("mean", "std", "min", "max", "q01", "q99")
_LANGUAGE_CONDITIONED_POLICIES = frozenset({"pi0", "pi05", "pi0_fast", "smolvla"})

class DatasetRefused(ValueError):
    """The dataset cannot be trained. ``str()`` is a German sentence for the
    student (the route answers 400 with it), written by this module and never
    containing an exception's text once it leaves ``preflight_dataset``."""


class HubUnavailable(Exception):
    """The Hub could not be asked: a timeout, a network or transport error, an
    HTTP 429 or 5xx, or a failure nobody classified. The route answers 502 with
    its retry sentence; the cause is in the server log only."""


class _HubFailure(DatasetRefused):
    """The twin's refusal of a failed Hub or file read. Its text is the WORKER's
    sentence, which interpolates the library's exception (the twin test holds
    the two equal, transliterated); ``preflight_dataset`` never lets that text
    out: it classifies ``exc`` and raises ``HubUnavailable`` or a
    ``DatasetRefused`` that names the cause in German without it."""

    def __init__(self, text: str, stage: str, exc: BaseException | None = None):
        super().__init__(text)
        self.stage = stage
        self.exc = exc


_SHA_RE = re.compile(r"[0-9a-f]{40}")
# A string LeRobot's is_valid_version accepts out of the hex alphabet: digits,
# optionally one pre-release marker (a, b, c = rc) and digits (PEP 440). LeRobot
# treats such a revision as a version TAG, not a commit (dataset_metadata.py:111-112).
_PEP440_LIKE_RE = re.compile(r"[0-9]+([abc][0-9]*)?")


def _hub():
    """The huggingface_hub names this module uses, imported at call time (the
    API's tests stub huggingface_hub; the twin test patches this function)."""
    from huggingface_hub import HfApi, hf_hub_download
    from huggingface_hub.utils import HfHubHTTPError, RepositoryNotFoundError, RevisionNotFoundError

    try:
        from huggingface_hub.utils import GatedRepoError
    except ImportError:  # pragma: no cover - every supported version has it
        GatedRepoError = RepositoryNotFoundError
    return SimpleNamespace(
        HfApi=HfApi, hf_hub_download=hf_hub_download, HfHubHTTPError=HfHubHTTPError,
        RepositoryNotFoundError=RepositoryNotFoundError, RevisionNotFoundError=RevisionNotFoundError,
        GatedRepoError=GatedRepoError,
    )


def not_found_sentence(dataset_name: str) -> str:
    return (
        f"Dataset '{dataset_name}' wurde auf HuggingFace nicht gefunden "
        f"oder ist privat (Worker hat keinen Zugriff)."
    )


def no_tag_sentence(dataset_name: str) -> str:
    return (
        f"Dataset '{dataset_name}' hat keinen "
        f"'{EXPECTED_CODEBASE_VERSION}'-Versions-Tag auf HuggingFace — "
        f"das Training lädt den Datensatz an genau diesem Tag. "
        f"Bitte den Datensatz mit der EduBotics-App erneut hochladen."
    )


# --- The student's sentences for a failed read (no exception text, ever) -----

def info_missing_sentence(dataset_name: str) -> str:
    return (
        f"Dataset '{dataset_name}' hat am '{EXPECTED_CODEBASE_VERSION}'-Tag keine "
        f"meta/info.json auf HuggingFace. Bitte den Datensatz mit der EduBotics-App "
        f"erneut hochladen."
    )


def info_invalid_sentence(dataset_name: str) -> str:
    return (
        f"Dataset '{dataset_name}' hat ein ungültiges meta/info.json (kein lesbares "
        f"JSON). Bitte den Datensatz mit der EduBotics-App erneut hochladen."
    )


def stats_unloadable_sentence(dataset_name: str) -> str:
    return (
        f"Dataset '{dataset_name}' hat keine ladbare meta/stats.json. Ohne "
        f"Normalisierungs-Statistiken kann nicht trainiert werden. Bitte mit "
        f"aktueller Recording-Software neu aufnehmen."
    )


def read_failed_sentence(dataset_name: str) -> str:
    return (
        f"Dataset '{dataset_name}' konnte auf HuggingFace nicht vollständig gelesen "
        f"werden (eine Datei fehlt oder ist ungültig). Bitte den Datensatz mit der "
        f"EduBotics-App erneut hochladen."
    )


def hub_refused_sentence(dataset_name: str, status: int) -> str:
    return (
        f"HuggingFace hat das Lesen von Dataset '{dataset_name}' abgelehnt "
        f"(HTTP-Status {int(status)}). Bitte den Namen des Datensatzes prüfen."
    )


def _stat_value(stats: dict, feature: str, key: str):
    """Read a single stat from a meta/stats.json dict, tolerant of both shapes.

    On disk LeRobot v0.5.1 serializes stats FLAT with '/'-joined keys
    (serialize_dict -> flatten_dict), e.g. "observation.state/mean". Older /
    hand-built files may use the nested {feature: {key: ...}} shape. Accept both.
    Returns None when absent.
    """
    flat = stats.get(f"{feature}/{key}")
    if flat is not None:
        return flat
    nested = stats.get(feature)
    if isinstance(nested, dict):
        return nested.get(key)
    return None


def _validate_stats(stats: dict, dataset_name: str, n_joints: int) -> None:
    """Reject a dataset whose normalization stats are missing/incomplete."""
    if not isinstance(stats, dict):
        raise DatasetRefused(
            f"Dataset '{dataset_name}' hat eine ungültige meta/stats.json "
            f"(kein JSON-Objekt). Bitte mit aktueller Recording-Software neu "
            f"aufnehmen."
        )
    for feature in ("observation.state", "action"):
        for key in _REQUIRED_STATS_KEYS:
            value = _stat_value(stats, feature, key)
            if not isinstance(value, list) or len(value) != n_joints:
                raise DatasetRefused(
                    f"Dataset '{dataset_name}' hat unvollständige "
                    f"Normalisierungs-Statistiken (meta/stats.json: '{feature}' "
                    f"'{key}' fehlt oder hat die falsche Länge). Das Modell "
                    f"würde mit falscher Normalisierung trainieren. Bitte mit "
                    f"aktueller Recording-Software neu aufnehmen."
                )


def _preflight_dataset(dataset_name: str, token, model_type: str = "",
                       revision: str = EXPECTED_CODEBASE_VERSION) -> dict:
    """Download meta/info.json at ``revision`` and validate the dataset is trainable.

    Returns the parsed meta/info.json dict (with ``features`` guaranteed present).
    Raises ValueError with a German student-facing message on failure.
    """
    hub = _hub()
    download_result: dict = {}

    def _download_worker():
        try:
            download_result["path"] = hub.hf_hub_download(
                repo_id=dataset_name,
                filename="meta/info.json",
                repo_type="dataset",
                revision=revision,
                token=token,
            )
        except BaseException as exc:  # propagate to main thread
            download_result["error"] = exc

    thread = threading.Thread(target=_download_worker, daemon=True)
    thread.start()
    thread.join(timeout=60)

    if thread.is_alive():
        raise _HubFailure(
            f"Dataset '{dataset_name}' Preflight hat das Zeitlimit (60s) "
            f"überschritten — HuggingFace Hub erreichbar? Bitte später "
            f"erneut starten.",
            "info", TimeoutError("meta/info.json download"),
        )
    err = download_result.get("error")
    if isinstance(err, hub.RepositoryNotFoundError):
        raise DatasetRefused(not_found_sentence(dataset_name))
    if isinstance(err, hub.RevisionNotFoundError):
        raise DatasetRefused(no_tag_sentence(dataset_name))
    if isinstance(err, hub.HfHubHTTPError):
        raise _HubFailure(
            f"Dataset '{dataset_name}' konnte nicht geladen werden: {err}",
            "info", err,
        )
    if err is not None:
        raise _HubFailure(
            f"Dataset '{dataset_name}' Preflight fehlgeschlagen: {err}",
            "info", err,
        )
    info_path = download_result.get("path")
    if info_path is None:
        raise DatasetRefused(
            f"Dataset '{dataset_name}' Preflight lieferte keinen Pfad zurück."
        )

    try:
        with open(info_path) as f:
            info = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise _HubFailure(
            f"Dataset '{dataset_name}' hat ein ungültiges meta/info.json: {e}",
            "info_json", e,
        )

    version = info.get("codebase_version")
    if not version:
        raise DatasetRefused(
            f"Dataset '{dataset_name}' hat kein 'codebase_version' Feld. "
            f"Bitte mit aktueller Recording-Software neu aufnehmen."
        )
    if version != EXPECTED_CODEBASE_VERSION:
        raise DatasetRefused(
            f"Dataset '{dataset_name}' hat codebase_version='{version}', "
            f"erwartet wird '{EXPECTED_CODEBASE_VERSION}'. "
            f"Bitte mit aktueller Recording-Software neu aufnehmen."
        )

    fps = info.get("fps")
    if not fps or fps <= 0:
        raise DatasetRefused(
            f"Dataset '{dataset_name}' hat keine gültige 'fps' Angabe ({fps!r})."
        )

    total_episodes = info.get("total_episodes")
    if not total_episodes or total_episodes < 1:
        raise DatasetRefused(
            f"Dataset '{dataset_name}' enthält keine Episoden "
            f"(total_episodes={total_episodes!r}). Bitte zuerst aufnehmen "
            f"und hochladen."
        )

    features = info.get("features") or {}

    def _get_joint_names(feature_key: str, label: str) -> list:
        feat = features.get(feature_key)
        if not feat:
            raise DatasetRefused(
                f"Dataset '{dataset_name}' hat kein '{feature_key}' Feature "
                f"({label}). Aufnahme ist beschädigt."
            )
        names = feat.get("names")
        if not names or not isinstance(names, list):
            raise DatasetRefused(
                f"Dataset '{dataset_name}' hat keine '{feature_key}.names' "
                f"Liste. Bitte neu aufnehmen."
            )
        if not (MIN_JOINTS <= len(names) <= MAX_JOINTS):
            raise DatasetRefused(
                f"Dataset '{dataset_name}' hat {len(names)} {label}-Gelenke — "
                f"erwartet werden {MIN_JOINTS}-{MAX_JOINTS}. Aufnahme prüfen."
            )
        return names

    state_names = _get_joint_names("observation.state", "Follower")
    action_names = _get_joint_names("action", "Action")

    if state_names != action_names:
        raise DatasetRefused(
            f"Dataset '{dataset_name}' hat unterschiedliche Gelenk-Namen "
            f"für observation.state und action. "
            f"state: {state_names}, action: {action_names}. "
            f"Aufnahme ist beschädigt — bitte neu aufnehmen."
        )

    image_keys = [k for k in features if k.startswith("observation.images.")]
    if not image_keys:
        raise DatasetRefused(
            f"Dataset '{dataset_name}' enthält keine Kamera-Features. "
            f"Mindestens eine Kamera ist erforderlich."
        )
    cameras = [k.replace("observation.images.", "") for k in image_keys]

    try:
        stats_path = hub.hf_hub_download(
            repo_id=dataset_name,
            filename="meta/stats.json",
            repo_type="dataset",
            revision=revision,
            token=token,
        )
        with open(stats_path) as f:
            stats = json.load(f)
    except (OSError, json.JSONDecodeError, hub.HfHubHTTPError,
            hub.RepositoryNotFoundError) as e:
        raise _HubFailure(
            f"Dataset '{dataset_name}' hat keine ladbare meta/stats.json "
            f"({e}). Ohne Normalisierungs-Statistiken kann nicht trainiert "
            f"werden. Bitte mit aktueller Recording-Software neu aufnehmen.",
            "stats", e,
        )
    _validate_stats(stats, dataset_name, len(state_names))

    try:
        repo_files = hub.HfApi(token=token).list_repo_files(
            dataset_name, repo_type="dataset",
            revision=revision,
        )
    except Exception as e:
        raise _HubFailure(
            f"Dataset '{dataset_name}' Dateiliste konnte nicht geladen "
            f"werden: {e}",
            "files", e,
        )

    def _layout_has(prefix: str, suffix: str) -> bool:
        return any(
            f.startswith(prefix) and f.endswith(suffix) for f in repo_files
        )

    missing_parts = []
    if not _layout_has("data/", ".parquet"):
        missing_parts.append("data/*.parquet")
    if not _layout_has("meta/episodes/", ".parquet"):
        missing_parts.append("meta/episodes/*.parquet")
    for image_key in image_keys:
        if not _layout_has(f"videos/{image_key}/", ".mp4"):
            missing_parts.append(f"videos/{image_key}/*.mp4")
    if missing_parts:
        raise DatasetRefused(
            f"Dataset '{dataset_name}' ist unvollständig hochgeladen — es "
            f"fehlen: {', '.join(missing_parts)}. Vermutlich wurde der "
            f"Upload unterbrochen. Bitte den Datensatz mit der EduBotics-App "
            f"erneut hochladen."
        )

    if (model_type or "").lower() in _LANGUAGE_CONDITIONED_POLICIES:
        total_tasks = info.get("total_tasks")
        if not total_tasks or total_tasks < 1:
            raise DatasetRefused(
                f"Dataset '{dataset_name}' enthält keine Aufgaben-Texte "
                f"(total_tasks={total_tasks!r}), die das Modell '{model_type}' "
                f"benötigt. Bitte mit einer Aufgabenanweisung neu aufnehmen "
                f"oder ein ACT-Modell wählen."
            )

    logger.info(
        "Preflight OK: dataset=%r revision=%s codebase_version=%s fps=%s joints=%s cameras=%s total_tasks=%s",
        dataset_name, revision, version, fps, len(state_names), cameras, info.get("total_tasks"),
    )
    out = dict(info)
    out["features"] = features
    return out


def preflight_dataset(dataset_name: str, token, model_type: str = "",
                      revision: str = EXPECTED_CODEBASE_VERSION) -> dict:
    """The route's entry point (spec §4.1): the twin with token and revision.

    Raises ``DatasetRefused`` (400; a German sentence of this module, never an
    exception's text) or ``HubUnavailable`` (502: the Hub could not be asked).
    The twin's own Hub-failure sentences carry the library's exception text
    (they are the worker's, held equal by the twin test), so every one of them
    is classified here and replaced; the exception's TYPE and HTTP status go to
    the server log only."""
    try:
        return _preflight_dataset(dataset_name, token, model_type, revision=revision)
    except _HubFailure as failure:
        raise _public_failure(dataset_name, failure.stage, failure.exc) from None
    except DatasetRefused:
        raise
    except Exception as exc:  # a library exception none of the twin's handlers catches
        raise _public_failure(dataset_name, "other", exc) from None


# huggingface_hub's classes, recognised by NAME (this module imports the library
# only inside _hub(), and the tests stub it). A LocalEntryNotFoundError from
# hf_hub_download means "the Hub could not be reached and nothing is cached":
# huggingface_hub 1.x raises it FROM the real cause (a 403, a 503, a ConnectError),
# so the chain is followed to that cause.
_NO_REPO_NAMES = frozenset({"RepositoryNotFoundError", "GatedRepoError", "DisabledRepoError"})
_NO_FILE_NAMES = frozenset({"RemoteEntryNotFoundError", "EntryNotFoundError"})
_TRANSPORT_NAMES = frozenset({"TransportError", "TimeoutException", "Timeout", "gaierror"})
_CHAIN_MAX = 8


def _chain(exc):
    seen = []
    while exc is not None and len(seen) < _CHAIN_MAX and all(exc is not e for e in seen):
        seen.append(exc)
        exc = exc.__cause__ if exc.__cause__ is not None else exc.__context__
    return seen


def classify_hub_error(exc) -> tuple[str, int | None]:
    """(kind, HTTP status) of a failed Hub or file read.

    ``kind``: ``"no_repo"`` (missing, private or gated: RepositoryNotFound, a 401
    or 403), ``"no_revision"`` (no such tag or commit), ``"no_file"`` (a 404 on a
    file), ``"malformed"`` (the downloaded JSON does not parse), ``"refused"``
    (another 4xx), else ``"transient"``: an HTTP 429 or 5xx, a network or
    transport error, a timeout, and every failure nobody classified (we could not
    ask, which is no proof against the dataset)."""
    for link in _chain(exc):
        names = {k.__name__ for k in type(link).__mro__}
        status = _http_status(link)
        if "LocalEntryNotFoundError" in names:
            continue
        if "RevisionNotFoundError" in names:
            return "no_revision", status
        if names & _NO_REPO_NAMES:
            return "no_repo", status
        if names & _NO_FILE_NAMES:
            return "no_file", status
        if status is not None:
            if status == 429 or status >= 500:
                return "transient", status
            if status in (401, 403):
                return "no_repo", status
            if status == 404:
                return "no_file", status
            if 400 <= status < 500:
                return "refused", status
            return "transient", status
        if isinstance(link, (json.JSONDecodeError, UnicodeDecodeError)):
            return "malformed", None
        if isinstance(link, (TimeoutError, ConnectionError)) or names & _TRANSPORT_NAMES:
            return "transient", None
    return "transient", None


def _type_chain(exc) -> str:
    return " <- ".join(type(link).__name__ for link in _chain(exc)) or "None"


def _public_failure(dataset_name: str, stage: str, exc) -> Exception:
    """The exception the route may show for a failed read at ``stage``."""
    kind, status = classify_hub_error(exc)
    logger.warning(
        "dataset preflight %r: %s at stage %s (HTTP %s) -> %s",
        dataset_name, _type_chain(exc), stage, status, kind,
    )
    if kind == "transient":
        return HubUnavailable(f"{stage}: {kind} (HTTP {status})")
    if kind == "no_repo":
        return DatasetRefused(not_found_sentence(dataset_name))
    if kind == "no_revision":
        return DatasetRefused(no_tag_sentence(dataset_name))
    if kind == "refused":
        return DatasetRefused(hub_refused_sentence(dataset_name, status))
    sentence = {
        ("info", "no_file"): info_missing_sentence,
        ("info_json", "no_file"): info_missing_sentence,
        ("info", "malformed"): info_invalid_sentence,
        ("info_json", "malformed"): info_invalid_sentence,
        ("stats", "no_file"): stats_unloadable_sentence,
        ("stats", "malformed"): stats_unloadable_sentence,
    }.get((stage, kind), read_failed_sentence)
    return DatasetRefused(sentence(dataset_name))


def _http_status(exc) -> int | None:
    resp = getattr(exc, "response", None)
    status = getattr(resp, "status_code", None)
    return status if isinstance(status, int) else None


def resolve_v3_commit(dataset_name: str) -> str | None:
    """The commit the dataset's ``v3.0`` tag points to, read anonymously (E17).

    Returns the 40-hex sha, or None when the sha would parse as a PEP 440
    version (LeRobot would treat it as a version tag; the training then runs
    unpinned, WARNING logged). Raises DatasetRefused with the twin's German
    sentence when the dataset is missing, private or gated (TR-1: a 401/403/404
    too) or has no ``v3.0`` tag; any other failure (timeout, network, 5xx, a
    malformed answer) propagates for the caller's 502 (the route shows only a
    DatasetRefused's text).
    """
    hub = _hub()
    try:
        refs = hub.HfApi(token=False).list_repo_refs(dataset_name, repo_type="dataset")
    except (hub.RepositoryNotFoundError, hub.GatedRepoError):
        raise DatasetRefused(not_found_sentence(dataset_name)) from None
    except hub.HfHubHTTPError as exc:
        if _http_status(exc) in (401, 403, 404):
            raise DatasetRefused(not_found_sentence(dataset_name)) from None
        raise
    tags = getattr(refs, "tags", None) or []
    sha = None
    for tag in tags:
        if getattr(tag, "name", None) == EXPECTED_CODEBASE_VERSION:
            sha = getattr(tag, "target_commit", None)
            break
    if sha is None:
        raise DatasetRefused(no_tag_sentence(dataset_name))
    if type(sha) is not str or not _SHA_RE.fullmatch(sha):
        raise RuntimeError("v3.0 tag has a malformed target commit")
    if _PEP440_LIKE_RE.fullmatch(sha):
        logger.warning(
            "dataset %r: the v3.0 commit parses as a version string; the training runs unpinned",
            dataset_name,
        )
        return None
    return sha
