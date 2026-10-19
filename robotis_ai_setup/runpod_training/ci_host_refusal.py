"""CI smoke for the trainer image (spec §4.12): the host check must REFUSE a GPU-less runner.

    docker run --rm --entrypoint python -v <this file>:/ci/ci_host_refusal.py:ro <image> /ci/ci_host_refusal.py

Exit 0 iff training_handler._assert_training_host() raises HostUnfit (on a GitHub
runner: reason no_cuda), 1 when it passes, 2 when it fails in any other way.
"""

import sys


def main() -> int:
    import training_handler
    try:
        training_handler._assert_training_host()
    except training_handler.HostUnfit as e:
        print(f"OK: the host check refused this host ({e.reason}: {e.detail})")
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"FAIL: the host check raised {type(e).__name__} instead of HostUnfit: {e}")
        return 2
    print("FAIL: the host check passed on a host without a usable GPU")
    return 1


if __name__ == "__main__":
    sys.exit(main())
