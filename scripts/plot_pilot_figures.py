"""Plot saved pilot results: python -m scripts.plot_pilot_figures (cache only)."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
from math import ceil, floor
from pathlib import Path

from lp_pilot.config import load_config, utc_seconds
from lp_pilot.rpc_ingest import checked_artifact, historical_state
from lp_pilot.storage import read_table, sha256
from lp_pilot.uniswap import price_from_sqrt

UTC = timezone.utc
CAPTIONS = {
    "pilot_pool_reconstruction": (
        "Reconstructed pool price (USDC per ETH) over the captured interval, 14 January to "
        "17 January 2024, including warm-up and the outcome tail. The step line retains the "
        "last stored state in each block; dashed boundaries delimit the decision period "
        "(15 January to 17 January, exclusive). Circles show separately queried historical "
        "end-of-block RPC states at their actual block times. These control states use the "
        "same RPC provider as the logs. This price comparison does not itself validate the "
        "other state variables or arbitrary counterfactual position outcomes."
    ),
    "pilot_markout_resolution": (
        "Historical five-minute markout at 24 hourly decisions on 15 January 2024 (UTC). "
        "Each value is the USDC-volume-weighted, signed five-minute reference-price movement "
        "over the same 4,527 mature swaps, using identical weights; it is not realized LP loss. "
        "Minute and aggTrade references are compared against equality. Finer transaction prices "
        "are neither ground truth nor order-book midprices. The figure shows measurement "
        "sensitivity, not forecasting performance."
    ),
}


def pool_data(cfg):
    root = Path(cfg["_root"])
    run = json.loads((root / "data/runs/real/manifest.json").read_text())
    assert run["mode"] == "real" and run["scope"] == "full"
    assert all(run["query_parameters"][key] == cfg[key] for key in ("pool", "pilot", "timing"))
    path = root / "data/runs/real/states.parquet"
    assert sha256(path) == run["artifacts"]["states.parquet"]
    states = read_table(path)
    # A block can contain several events. Keep its last transaction/log state.
    selected = {}
    for row in sorted(states, key=lambda row: (int(row["block_number"]),
                      int(row["transaction_index"]), int(row["log_index"]))):
        block = int(row["block_number"])
        if block in selected:
            previous = selected[block]
            assert (previous["timestamp"], previous["block_hash"]) == (row["timestamp"], row["block_hash"])
        selected[block] = row
    checkpoints = json.loads((root / cfg["data"]["checkpoints_path"]).read_text())["checkpoints"]
    points = []
    for record in checkpoints:
        # The marker comes from the archived RPC response, independently of replay.
        point = historical_state(checked_artifact(record, root))
        replay = selected[point["block_number"]]
        assert (point["block_number"], point["block_hash"], point["timestamp"]) == (
            int(record["block_number"]), record["block_hash"], record["timestamp"])
        assert (point["block_hash"], point["timestamp"], point["sqrt_price_x96"]) == (
            replay["block_hash"], replay["timestamp"], int(replay["sqrt_price_x96"]))
        points.append(point)
    assert len(states) == run["row_counts"]["states"] and len(points) == 4
    return list(selected.values()), points, run


def markout_data(root, run):
    rows = sorted(read_table(root / "data/validation/reference_resolution/common_markouts.parquet"),
                  key=lambda row: row["decision_time"])
    path = root / "data/runs/real/events.parquet"
    assert sha256(path) == run["artifacts"]["events.parquet"]
    events = {f"{row['transaction_hash']}:{row['log_index']}": row for row in read_table(path)}
    for row in rows:
        keys = json.loads(row["common_identities"])
        count = int(row["common_valid_swaps"])
        assert len(keys) == len(set(keys)) == count
        assert all(int(value) == count for value in json.loads(row["valid_counts"]).values())
        assert all(not value for key in ("excluded_identities", "lost_to_intersection")
                   for value in json.loads(row[key]).values())
        volume = sum((Decimal(abs(int(events[key]["amount0"]))) / 10**6 for key in keys), Decimal(0))
        assert volume == Decimal(row["common_quote_volume_usdc"])
    start = utc_seconds("2024-01-15T00:00:00Z")
    assert [row["decision_time"] for row in rows] == [start + hour*3600 for hour in range(24)]
    assert sum(int(row["common_valid_swaps"]) for row in rows) == 4527
    return rows


def draw_figures(output, replay, points, markouts, capture, cfg):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as dates
    import matplotlib.pyplot as plt
    from matplotlib.ticker import StrMethodFormatter

    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9, "axes.labelsize": 9,
                         "xtick.labelsize": 8, "ytick.labelsize": 8, "legend.fontsize": 8,
                         "axes.spines.top": False, "axes.spines.right": False,
                         "pdf.fonttype": 42, "path.simplify": False})
    for name in CAPTIONS:
        fig, ax = plt.subplots(figsize=(14.5/2.54, (8.5 if name.endswith("reconstruction") else 13.5)/2.54))
        if name.endswith("reconstruction"):
            fig.subplots_adjust(left=.13, right=.98, bottom=.21, top=.79)
            times = lambda rows: [datetime.fromtimestamp(float(row["timestamp"]), UTC) for row in rows]
            prices = lambda rows: [float(price_from_sqrt(int(row["sqrt_price_x96"]), 6, 18, True)) for row in rows]
            ax.step(times(replay), prices(replay), where="post", color="0.15", linewidth=.75,
                    label="Reconstructed (last state per block)")
            ax.plot(times(points), prices(points), linestyle="none", marker="o", markersize=6.5,
                    markerfacecolor="white", markeredgecolor="black", markeredgewidth=1.1,
                    clip_on=False, label="Separate RPC checkpoint", zorder=4)
            first, last = [datetime.fromtimestamp(stamp, UTC) for stamp in capture]
            start, end = [datetime.fromtimestamp(utc_seconds(cfg["pilot"][key]), UTC) for key in ("start", "end")]
            ax.axvspan(first, start, facecolor="0.93", zorder=0)
            ax.axvspan(end, last, facecolor="0.94", edgecolor="0.6", hatch="///", linewidth=0, zorder=0)
            for boundary in (start, end):
                ax.axvline(boundary, color="0.45", linestyle="--", linewidth=.8, zorder=1)
            for position, label, align in ((first+(start-first)/2, "Warm-up", "center"),
                                           (start+(end-start)/2, "Decision period", "center"), (last, "Tail", "right")):
                ax.text(position, 1.03, label, transform=ax.get_xaxis_transform(), ha=align, va="bottom", fontsize=8)
            ax.set_xlim(first, last)
            ax.xaxis.set_major_locator(dates.HourLocator(byhour=(0, 12), tz=UTC))
            ax.xaxis.set_major_formatter(dates.DateFormatter("%d %b\n%H:%M", tz=UTC))
            ax.yaxis.set_major_formatter(StrMethodFormatter("{x:,.0f}"))
            ax.set(xlabel="Date and time (UTC, 2024)", ylabel="Pool price (USDC per ETH)")
            ax.legend(loc="lower left", bbox_to_anchor=(0, 1.16), ncol=2, frameon=False, borderaxespad=0)
            ax.grid(axis="y", color="0.86", linewidth=.5)
        else:
            fig.subplots_adjust(left=.15, right=.98, bottom=.13, top=.98)
            x, y = ([row[key] for row in markouts] for key in ("A_markout_bps", "C_markout_bps"))
            span = max(x+y)-min(x+y)
            limits = (floor(min(x+y)-.08*span), ceil(max(x+y)+.08*span))
            ax.axhline(0, color="0.72", linewidth=.7, zorder=0)
            ax.axvline(0, color="0.72", linewidth=.7, zorder=0)
            ax.plot(limits, limits, color="0.3", linestyle="--", linewidth=.9, label="Equality (y = x)")
            ax.scatter(x, y, s=25, color="0.1", edgecolors="white", linewidths=.45, zorder=3)
            ax.set(xlim=limits, ylim=limits, xlabel="Historical 5-minute markout — minute reference (bps)",
                   ylabel="Historical 5-minute markout — aggTrade reference (bps)", aspect="equal")
            ax.legend(loc="lower right", frameon=False)
        fig.savefig(output / f"{name}.pdf", metadata={"CreationDate": None, "ModDate": None, "Title": name})
        fig.savefig(output / f"{name}.png", dpi=300)
        plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/pilot.toml")
    parser.add_argument("--output-dir", default="reports/figures")
    args = parser.parse_args()
    cfg = load_config(args.config)
    root = Path(cfg["_root"])
    replay, points, run = pool_data(cfg)
    markouts = markout_data(root, run)
    output = root / args.output_dir
    output.mkdir(parents=True, exist_ok=True)
    draw_figures(output, replay, points, markouts, run["required_interval"], cfg)
    snippets = ["% Requires \\usepackage{graphicx}; paths are relative to the project root."]
    for name, caption in CAPTIONS.items():
        snippets.append("\n".join([
            "\\begin{figure}[tbp]", "  \\centering",
            f"  \\includegraphics[width=14.5cm]{{{output.relative_to(root)}/{name}.pdf}}",
            f"  \\caption{{{caption}}}", f"  \\label{{fig:{name.replace('_', '-')}}}", "\\end{figure}"]))
    (output / "figures.tex").write_text("\n\n".join(snippets) + "\n")
    print(f"Saved two figures as PDF and PNG: {len(replay)} block states, {len(points)} RPC checkpoints; "
          f"{len(markouts)} markout decisions, 4,527 common swaps.")


if __name__ == "__main__":
    main()
