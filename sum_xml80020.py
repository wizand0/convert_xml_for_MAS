#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
sum_xml80020.py

Суммирует значения <value> из ежедневных XML 80020 за месяц
(отдельно по каждой точке учёта и по дням). Значения берутся как есть,
никаких коэффициентов не применяется.

Запуск из папки, где лежат файлы за месяц (30 или 31 файл):

    cd "D:\\...\\converted"
    python sum_xml80020.py

Результат: таблица в консоли + файл summary_80020.csv (рядом со скриптом
в рабочей папке; разделитель ";", открывается в Excel).
"""

from __future__ import annotations

import calendar
import csv
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from decimal import Decimal
from pathlib import Path

# ============================================================
# НАСТРОЙКИ
# ============================================================
FILE_PATTERN = re.compile(r"^80020_(\d+)_(\d{8})_(\d+)\.xml$", re.IGNORECASE)
EXPECTED_PERIODS = 48
CSV_NAME = "summary_80020.csv"
# ============================================================


def read_file(path: Path) -> tuple[dict[str, tuple[str, Decimal, int]], str | None]:
    """
    Возвращает ({код точки: (название, сумма, число периодов)}, <day> из файла).
    """
    root = ET.parse(path).getroot()
    if root.get("class") != "80020":
        raise ValueError(f'class={root.get("class")!r}, ожидалось "80020"')

    result = {}
    for point in root.iter("measuringpoint"):
        code = point.get("code")
        total = Decimal(0)
        count = 0
        for period in point.iter("period"):
            text = period.findtext("value")
            if text is None or not text.strip():
                raise ValueError(f"точка {code}: пустое <value>")
            total += Decimal(text.strip().replace(",", "."))
            count += 1
        result[code] = (point.get("name") or "", total, count)

    if not result:
        raise ValueError("нет <measuringpoint>")
    return result, root.findtext("datetime/day")


def fmt(number: Decimal) -> str:
    return f"{number:,.5f}".replace(",", " ")


def main() -> int:
    base = Path.cwd()
    files: dict[str, Path] = {}

    for path in sorted(base.glob("*.xml")):
        match = FILE_PATTERN.match(path.name)
        if not match:
            continue
        date = match.group(2)
        if date in files:
            print(f"[WARNING] {date}: несколько файлов, используется {files[date].name}, "
                  f"пропущен {path.name}")
            continue
        files[date] = path

    if not files:
        print(f"В папке {base} нет файлов вида 80020_<ИНН>_<ГГГГММДД>_NN.xml")
        return 1

    print("=" * 70)
    print("Суммирование XML80020")
    print("=" * 70)
    print(f"Папка: {base}")
    print(f"Файлов: {len(files)}")

    meters: dict[str, str] = {}              # код -> название (порядок появления)
    per_day: dict[str, dict[str, Decimal]] = {}
    problems: list[str] = []

    for date, path in files.items():
        try:
            data, day_in_file = read_file(path)
        except (ET.ParseError, ValueError, OSError, ArithmeticError) as exc:
            problems.append(f"{path.name}: {exc}")
            continue

        if day_in_file and day_in_file != date:
            problems.append(f"{path.name}: <day>={day_in_file} не совпадает с датой в имени")
        for code, (name, total, count) in data.items():
            meters.setdefault(code, name)
            per_day.setdefault(date, {})[code] = total
            if count != EXPECTED_PERIODS:
                problems.append(f"{path.name}: точка {code}: периодов {count} "
                                f"вместо {EXPECTED_PERIODS}")

    if not per_day:
        print("Ни один файл не удалось прочитать.")
        for p in problems:
            print("[ERROR]", p)
        return 2

    codes = list(meters)
    dates = sorted(per_day)
    months = sorted({d[:6] for d in dates})

    # Недостающие дни месяца
    missing = []
    for ym in months:
        year, month = int(ym[:4]), int(ym[4:])
        for day in range(1, calendar.monthrange(year, month)[1] + 1):
            d = f"{ym}{day:02d}"
            if d not in per_day:
                missing.append(d)

    # Таблица по дням
    print()
    print("Точки учёта:")
    for i, code in enumerate(codes, start=1):
        print(f"  [{i}] {code}  {meters[code]}")
    print()

    header = f"{'Дата':<12}" + "".join(f"{'[' + str(i) + ']':>18}" for i in range(1, len(codes) + 1)) \
        + f"{'Сумма':>20}"
    print(header)
    print("-" * len(header))

    totals = {code: Decimal(0) for code in codes}
    rows = []
    for date in dates:
        day_sum = Decimal(0)
        cells = []
        for code in codes:
            value = per_day[date].get(code)
            if value is None:
                cells.append(None)
                continue
            totals[code] += value
            day_sum += value
            cells.append(value)
        rows.append((date, cells, day_sum))
        iso = datetime.strptime(date, "%Y%m%d").strftime("%d.%m.%Y")
        line = f"{iso:<12}" + "".join(f"{(fmt(c) if c is not None else '-'):>18}" for c in cells)
        print(line + f"{fmt(day_sum):>20}")

    grand = sum(totals.values(), Decimal(0))
    print("-" * len(header))
    print(f"{'ИТОГО':<12}" + "".join(f"{fmt(totals[c]):>18}" for c in codes) + f"{fmt(grand):>20}")

    print()
    print(f"Дней в данных: {len(dates)}")
    if missing:
        print("Нет файлов за даты (итоги неполные):")
        for d in missing:
            print("  ", datetime.strptime(d, "%Y%m%d").strftime("%d.%m.%Y"))
    else:
        print("Все дни месяца присутствуют.")
    if problems:
        print()
        for p in problems:
            print("[WARNING]", p)

    # CSV
    csv_path = base / CSV_NAME
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh, delimiter=";")
        writer.writerow(["Дата"] + [f"{c} {meters[c]}" for c in codes] + ["Сумма"])
        for date, cells, day_sum in rows:
            writer.writerow([datetime.strptime(date, "%Y%m%d").strftime("%d.%m.%Y")]
                            + [("" if c is None else str(c)) for c in cells] + [str(day_sum)])
        writer.writerow(["ИТОГО"] + [str(totals[c]) for c in codes] + [str(grand)])
    print()
    print(f"Таблица сохранена: {csv_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
