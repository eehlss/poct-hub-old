"""Strip-lot drift surveillance (EWMA of % bias vs hub reference). Deterministic, no AI."""
from __future__ import annotations
LAMBDA, L_HOLD, L_WATCH, MIN_N = 0.2, 3.0, 2.0, 5


def ewma_status(biases: list[float], sd_bias: float, tea_half: float, lam: float = LAMBDA) -> dict:
    """biases: chronological % bias of paired comparisons (device vs hub reference).
    sd_bias: expected SD of % bias for an in-control lot; tea_half: allowable bias % (e.g. 7.5 for glucose)."""
    e = 0.0
    for b in biases:
        e = lam * b + (1 - lam) * e
    n = len(biases)
    sigma_e = sd_bias * (lam / (2 - lam)) ** 0.5
    if n >= MIN_N and abs(e) > L_HOLD * sigma_e and abs(e) > tea_half:
        flag = "hold"
    elif n >= MIN_N and abs(e) > L_WATCH * sigma_e:
        flag = "watch"
    else:
        flag = "ok"
    return {"n": n, "ewma": round(e, 2), "limit_hold": round(max(L_HOLD * sigma_e, tea_half), 2), "flag": flag}


def pct_bias(device_value: float, reference_value: float) -> float:
    return 100.0 * (device_value - reference_value) / reference_value
