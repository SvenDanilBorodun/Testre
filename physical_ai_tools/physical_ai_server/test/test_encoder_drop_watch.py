"""Frame-drop detection through LeRobot's public warning (spec §6.3, owner Q2 A).

The watch parses the exact lines LeRobot 0.5.1's ``StreamingVideoEncoder``
logs on ``lerobot.datasets.video_utils`` — the first queue-full drop of an
episode, every 10th after it, and the finish summary — and nothing else.
"""

from __future__ import annotations

import logging
import threading

import pytest

from physical_ai_server.data_processing import encoder_drop_watch as edw

_KEY = 'observation.images.gripper'


def _queue_full(key, count):
    # verbatim f-string of video_utils.StreamingVideoEncoder.feed_frame
    return (f"Encoder queue full for {key}, dropped {count} frame(s). "
            f"Consider using vcodec='auto' for hardware encoding or increasing "
            f"encoder_queue_maxsize.")


def _finished(key, count):
    # verbatim f-string of video_utils.StreamingVideoEncoder.finish_episode
    return f"Episode finished with {count} dropped frame(s) for {key}."


@pytest.fixture
def logger_name(request):
    # a fresh logger per test, so tests never share a handler
    name = f'edubotics.test.video_utils.{request.node.name}'
    logger = logging.getLogger(name)
    yield name
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    logger.setLevel(logging.NOTSET)


def test_the_logger_name_is_lerobots():
    assert edw.LOGGER_NAME == 'lerobot.datasets.video_utils'


def test_counts_the_queue_full_warning(logger_name):
    watch = edw.install(logger_name)
    log = logging.getLogger(logger_name)
    assert watch.dropped() == 0
    log.warning(_queue_full(_KEY, 1))
    assert watch.dropped() == 1
    log.warning(_queue_full(_KEY, 10))
    log.warning(_queue_full('observation.images.scene', 1))
    assert watch.dropped_by_key() == {_KEY: 10, 'observation.images.scene': 1}
    assert watch.dropped() == 11


def test_the_finish_line_gives_the_exact_count(logger_name):
    watch = edw.install(logger_name)
    log = logging.getLogger(logger_name)
    log.warning(_queue_full(_KEY, 1))
    log.warning(_finished(_KEY, 5))
    assert watch.dropped_by_key() == {_KEY: 5}


def test_lerobot_style_lazy_formatting_is_parsed(logger_name):
    watch = edw.install(logger_name)
    logging.getLogger(logger_name).warning('Encoder queue full for %s, dropped %d frame(s).',
                                           _KEY, 3)
    assert watch.dropped() == 3


def test_arm_starts_a_new_take(logger_name):
    watch = edw.install(logger_name)
    log = logging.getLogger(logger_name)
    log.warning(_queue_full(_KEY, 1))
    watch.arm()
    assert watch.dropped() == 0
    log.warning(_queue_full(_KEY, 1))
    assert watch.dropped() == 1


def test_other_lines_and_other_loggers_are_ignored(logger_name):
    watch = edw.install(logger_name)
    log = logging.getLogger(logger_name)
    log.warning('Encoder thread for observation.images.gripper did not finish in time')
    log.info(_queue_full(_KEY, 1).replace('Encoder', 'Decoder'))
    logging.getLogger('lerobot.datasets.lerobot_dataset_edubotics_test').warning(
        _queue_full(_KEY, 7))
    assert watch.dropped() == 0


def test_attaches_once(logger_name):
    a = edw.install(logger_name)
    b = edw.install(logger_name)
    assert a is b
    log = logging.getLogger(logger_name)
    assert sum(1 for h in log.handlers if getattr(h, 'edubotics_encoder_drop_watch', False)) == 1
    log.warning(_queue_full(_KEY, 1))
    assert a.dropped() == 1


def test_a_logger_above_warning_is_lowered(logger_name):
    log = logging.getLogger(logger_name)
    log.setLevel(logging.ERROR)
    watch = edw.install(logger_name)
    assert log.getEffectiveLevel() <= logging.WARNING
    log.warning(_queue_full(_KEY, 1))
    assert watch.dropped() == 1


def test_a_malformed_record_never_raises(logger_name):
    watch = edw.install(logger_name)
    record = logging.LogRecord(logger_name, logging.WARNING, __file__, 1,
                               'bad %d', ('not a number',), None)
    watch.handle(record)          # getMessage() raises inside; swallowed
    assert watch.dropped() == 0


def test_arm_and_read_are_safe_against_the_writer(logger_name):
    watch = edw.install(logger_name)
    log = logging.getLogger(logger_name)
    stop = threading.Event()

    def writer():
        n = 0
        while not stop.is_set():
            n += 1
            log.warning(_queue_full(_KEY, n))

    t = threading.Thread(target=writer)
    t.start()
    try:
        for _ in range(200):
            watch.arm()
            assert watch.dropped() >= 0
    finally:
        stop.set()
        t.join()
