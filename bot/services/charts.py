import io

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime

from bot.categories import category_label
from bot.markers import display_name

plt.rcParams["font.family"] = "DejaVu Sans"

PALETTE = ["#AEC6CF", "#FFD1DC", "#B5EAD7", "#FFDAC1", "#C7CEEA", "#F8B4B4", "#D4F1F4", "#E2D9F3"]



async def build_pie_chart(data: list[dict], title: str) -> bytes:
    labels = [category_label(row["category"]) for row in data]
    values = [float(row["total"]) for row in data]
    colors = (PALETTE * ((len(data) // len(PALETTE)) + 1))[:len(data)]

    fig, ax = plt.subplots(figsize=(8, 5))
    wedges, texts, autotexts = ax.pie(
        values,
        labels=labels,
        autopct="%1.0f%%",
        colors=colors,
        startangle=140,
        pctdistance=0.82,
    )
    for t in autotexts:
        t.set_fontsize(9)
    ax.set_title(title, fontsize=14, pad=20)
    plt.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120)
    plt.close(fig)
    buf.seek(0)
    return buf.read()


async def build_bar_chart(data: list[dict], title: str, x_key: str, y_key: str) -> bytes:
    labels = [str(display_name(row[x_key])) for row in data]
    values = [float(row[y_key]) for row in data]

    fig, ax = plt.subplots(figsize=(8, 5))
    bars = ax.bar(labels, values, color="#AEC6CF")
    ax.set_title(title, fontsize=13)
    ax.set_ylabel("PLN")
    ax.bar_label(bars, fmt="%.2f", padding=3, fontsize=9)

    if len(labels) > 5:
        plt.xticks(rotation=30, ha="right")

    plt.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120)
    plt.close(fig)
    buf.seek(0)
    return buf.read()


async def build_trend_chart(data: list[dict], title: str) -> bytes:
    dates = [datetime.strptime(row["date"], "%Y-%m-%d") for row in data]
    values = [float(row["total"]) for row in data]

    fig, ax = plt.subplots(figsize=(8, 5))
    ax.plot(dates, values, color="#AEC6CF", linewidth=2, marker="o", markersize=4)
    ax.fill_between(dates, values, alpha=0.2, color="#AEC6CF")
    ax.set_title(title, fontsize=13)
    ax.set_ylabel("PLN")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    plt.xticks(rotation=30, ha="right")
    plt.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120)
    plt.close(fig)
    buf.seek(0)
    return buf.read()
