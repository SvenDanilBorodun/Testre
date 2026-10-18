"""compare.py <ckptA_pretrained_dir> <ckptB_pretrained_dir> <logA> <logB>

Tensor-by-tensor comparison of two model.safetensors and of the logged loss per
step (spec §B.5). compare() is what resume_proof.py uses; the CLI prints the same.
"""
import hashlib
import re
import sys

_LOSS_RE = re.compile(r"step:(\d+) .*?loss:([\d.]+)")


def losses(*paths) -> dict:
    """step -> logged loss text, from one or more LeRobot logs (later files win)."""
    out = {}
    for p in paths:
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = _LOSS_RE.search(line)
                if m:
                    out[int(m.group(1))] = m.group(2)
    return out


def _sha(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def compare_models(dir_a, dir_b) -> dict:
    from safetensors.torch import load_file
    a = load_file(f"{dir_a}/model.safetensors")
    b = load_file(f"{dir_b}/model.safetensors")
    if a.keys() != b.keys():
        return {"tensors": len(a), "keys_equal": False, "differing_tensors": None,
                "max_abs_diff": None, "file_sha_equal": False}
    maxdiff, ndiff = 0.0, 0
    for k in a:
        if not (a[k] == b[k]).all():
            ndiff += 1
            maxdiff = max(maxdiff, (a[k].double() - b[k].double()).abs().max().item())
    return {"tensors": len(a), "keys_equal": True, "differing_tensors": ndiff,
            "max_abs_diff": maxdiff,
            "file_sha_equal": _sha(f"{dir_a}/model.safetensors") == _sha(f"{dir_b}/model.safetensors")}


def compare_losses(la: dict, lb: dict) -> dict:
    common = sorted(set(la) & set(lb))
    return {"logged_steps_compared": len(common),
            "differing_loss_steps": [s for s in common if la[s] != lb[s]]}


def main(argv) -> int:
    m = compare_models(argv[1], argv[2])
    lo = compare_losses(losses(argv[3]), losses(argv[4]))
    print(f"tensors={m['tensors']} differing_tensors={m['differing_tensors']} "
          f"max_abs_diff={m['max_abs_diff']} file_sha_equal={m['file_sha_equal']}")
    print(f"logged_steps_compared={lo['logged_steps_compared']} "
          f"differing_loss_steps={lo['differing_loss_steps'][:20]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
