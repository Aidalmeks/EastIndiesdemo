"""Client-side overview navigator used by the Trading Batteries Lab."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import streamlit.components.v1 as components


_navigator = components.declare_component(
    "batteries_overview_navigator",
    path=str(Path(__file__).parent / "components" / "batteries_navigator"),
)


def render_overview_navigator(
    spread: np.ndarray,
    time_labels: list[str],
    start_index: int,
    end_index: int,
    *,
    key: str,
):
    """Render a decimated client-side navigator; submit only on Apply."""
    values = np.asarray(spread, dtype=float)
    if len(time_labels) != len(values):
        raise ValueError("time_labels must align one-for-one with spread")
    stride = max(1, int(np.ceil(len(values) / 3000)))
    indices = np.arange(0, len(values), stride, dtype=int)
    if len(indices) == 0 or indices[-1] != len(values) - 1:
        indices = np.append(indices, len(values) - 1)
    return _navigator(
        indices=indices.tolist(),
        values=values[indices].tolist(),
        time_labels=time_labels,
        total_bars=int(len(values)),
        start_index=int(start_index),
        end_index=int(end_index),
        default=None,
        key=key,
    )
