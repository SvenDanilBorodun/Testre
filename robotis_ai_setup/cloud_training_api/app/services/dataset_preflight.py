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
line goes to the log instead of stdout.

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
        raise ValueError(
            f"Dataset '{dataset_name}' hat eine ungültige meta/stats.json "
            f"(kein JSON-Objekt). Bitte mit aktueller Recording-Software neu "
            f"aufnehmen."
        )
    for feature in ("observation.state", "action"):
        for key in _REQUIRED_STATS_KEYS:
            value = _stat_value(stats, feature, key)
            if not isinstance(value, list) or len(value) != n_joints:
                raise ValueError(
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
        raise ValueError(
            f"Dataset '{dataset_name}' Preflight hat das Zeitlimit (60s) "
            f"überschritten — HuggingFace Hub erreichbar? Bitte später "
            f"erneut starten."
        )
    err = download_result.get("error")
    if isinstance(err, hub.RepositoryNotFoundError):
        raise ValueError(not_found_sentence(dataset_name))
    if isinstance(err, hub.RevisionNotFoundError):
        raise ValueError(no_tag_sentence(dataset_name))
    if isinstance(err, hub.HfHubHTTPError):
        raise ValueError(
            f"Dataset '{dataset_name}' konnte nicht geladen werden: {err}"
        )
    if err is not None:
        raise ValueError(
            f"Dataset '{dataset_name}' Preflight fehlgeschlagen: {err}"
        )
    info_path = download_result.get("path")
    if info_path is None:
        raise ValueError(
            f"Dataset '{dataset_name}' Preflight lieferte keinen Pfad zurück."
        )

    try:
        with open(info_path) as f:
            info = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(
            f"Dataset '{dataset_name}' hat ein ungültiges meta/info.json: {e}"
        )

    version = info.get("codebase_version")
    if not version:
        raise ValueError(
            f"Dataset '{dataset_name}' hat kein 'codebase_version' Feld. "
            f"Bitte mit aktueller Recording-Software neu aufnehmen."
        )
    if version != EXPECTED_CODEBASE_VERSION:
        raise ValueError(
            f"Dataset '{dataset_name}' hat codebase_version='{version}', "
            f"erwartet wird '{EXPECTED_CODEBASE_VERSION}'. "
            f"Bitte mit aktueller Recording-Software neu aufnehmen."
        )

    fps = info.get("fps")
    if not fps or fps <= 0:
        raise ValueError(
            f"Dataset '{dataset_name}' hat keine gültige 'fps' Angabe ({fps!r})."
        )

    total_episodes = info.get("total_episodes")
    if not total_episodes or total_episodes < 1:
        raise ValueError(
            f"Dataset '{dataset_name}' enthält keine Episoden "
            f"(total_episodes={total_episodes!r}). Bitte zuerst aufnehmen "
            f"und hochladen."
        )

    features = info.get("features") or {}

    def _get_joint_names(feature_key: str, label: str) -> list:
        feat = features.get(feature_key)
        if not feat:
            raise ValueError(
                f"Dataset '{dataset_name}' hat kein '{feature_key}' Feature "
                f"({label}). Aufnahme ist beschädigt."
            )
        names = feat.get("names")
        if not names or not isinstance(names, list):
            raise ValueError(
                f"Dataset '{dataset_name}' hat keine '{feature_key}.names' "
                f"Liste. Bitte neu aufnehmen."
            )
        if not (MIN_JOINTS <= len(names) <= MAX_JOINTS):
            raise ValueError(
                f"Dataset '{dataset_name}' hat {len(names)} {label}-Gelenke — "
                f"erwartet werden {MIN_JOINTS}-{MAX_JOINTS}. Aufnahme prüfen."
            )
        return names

    state_names = _get_joint_names("observation.state", "Follower")
    action_names = _get_joint_names("action", "Action")

    if state_names != action_names:
        raise ValueError(
            f"Dataset '{dataset_name}' hat unterschiedliche Gelenk-Namen "
            f"für observation.state und action. "
            f"state: {state_names}, action: {action_names}. "
            f"Aufnahme ist beschädigt — bitte neu aufnehmen."
        )

    image_keys = [k for k in features if k.startswith("observation.images.")]
    if not image_keys:
        raise ValueError(
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
        raise ValueError(
            f"Dataset '{dataset_name}' hat keine ladbare meta/stats.json "
            f"({e}). Ohne Normalisierungs-Statistiken kann nicht trainiert "
            f"werden. Bitte mit aktueller Recording-Software neu aufnehmen."
        )
    _validate_stats(stats, dataset_name, len(state_names))

    try:
        repo_files = hub.HfApi(token=token).list_repo_files(
            dataset_name, repo_type="dataset",
            revision=revision,
        )
    except Exception as e:
        raise ValueError(
            f"Dataset '{dataset_name}' Dateiliste konnte nicht geladen "
            f"werden: {e}"
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
        raise ValueError(
            f"Dataset '{dataset_name}' ist unvollständig hochgeladen — es "
            f"fehlen: {', '.join(missing_parts)}. Vermutlich wurde der "
            f"Upload unterbrochen. Bitte den Datensatz mit der EduBotics-App "
            f"erneut hochladen."
        )

    if (model_type or "").lower() in _LANGUAGE_CONDITIONED_POLICIES:
        total_tasks = info.get("total_tasks")
        if not total_tasks or total_tasks < 1:
            raise ValueError(
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
    """The route's entry point (spec §4.1): the twin with token and revision."""
    return _preflight_dataset(dataset_name, token, model_type, revision=revision)


def _http_status(exc) -> int | None:
    resp = getattr(exc, "response", None)
    status = getattr(resp, "status_code", None)
    return status if isinstance(status, int) else None


def resolve_v3_commit(dataset_name: str) -> str | None:
    """The commit the dataset's ``v3.0`` tag points to, read anonymously (E17).

    Returns the 40-hex sha, or None when the sha would parse as a PEP 440
    version (LeRobot would treat it as a version tag; the training then runs
    unpinned, WARNING logged). Raises ValueError with the twin's German sentence
    when the dataset is missing, private or gated (TR-1: a 401/403/404 too) or
    has no ``v3.0`` tag; any other failure (timeout, network, 5xx, a malformed
    answer) propagates for the caller's 502.
    """
    hub = _hub()
    try:
        refs = hub.HfApi(token=False).list_repo_refs(dataset_name, repo_type="dataset")
    except (hub.RepositoryNotFoundError, hub.GatedRepoError):
        raise ValueError(not_found_sentence(dataset_name)) from None
    except hub.HfHubHTTPError as exc:
        if _http_status(exc) in (401, 403, 404):
            raise ValueError(not_found_sentence(dataset_name)) from None
        raise
    tags = getattr(refs, "tags", None) or []
    sha = None
    for tag in tags:
        if getattr(tag, "name", None) == EXPECTED_CODEBASE_VERSION:
            sha = getattr(tag, "target_commit", None)
            break
    if sha is None:
        raise ValueError(no_tag_sentence(dataset_name))
    if type(sha) is not str or not _SHA_RE.fullmatch(sha):
        raise RuntimeError("v3.0 tag has a malformed target commit")
    if _PEP440_LIKE_RE.fullmatch(sha):
        logger.warning(
            "dataset %r: the v3.0 commit parses as a version string; the training runs unpinned",
            dataset_name,
        )
        return None
    return sha
