#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
convert_xml80020.py

Берёт ежедневные XML 80020 программы TaskGroup из каталогов счётчиков,
приводит их к виду старых файлов «Пирамида» и объединяет два счётчика
в один XML за каждый день.

Запуск (из каталога, где лежат папки счётчиков):

    cd "D:\\...\\serial"
    python convert_xml80020.py

Исходные файлы НЕ изменяются. Результат пишется в каталог OUTPUT_DIR_NAME.
"""

from __future__ import annotations

import logging
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

# ============================================================
# НАСТРОЙКИ
# ============================================================

# ИНН: используется в проверках и в имени итогового файла.
INN = "11111111111"

# Коэффициент: new_value = old_value * TRANSFORMATION_COEFFICIENT
# Это настройка пользователя, а не проверенный нормативный коэффициент.
TRANSFORMATION_COEFFICIENT = Decimal("750")

# Каталог результата (создаётся рядом с каталогами счётчиков).
OUTPUT_DIR_NAME = "converted"

# Счётчики. Ключ = имя каталога TaskGroup.
# Порядок записей = порядок <measuringpoint> в итоговом XML.
# В старом файле «Пирамида» (август 2026) первым идёт Ввод1 (28760252),
# вторым Ввод2 (26049958) — порядок сохранён таким же.
METERS = {
    "28760252": {
        "code": "1480296328760252",
        "name": "Ввод1 М-234 28760252",
        "channel_code": "01",
        "channel_desc": "(A+) на Ввод1 М-234 28760252",
    },
    "26049958": {
        "code": "1480296326049958",
        "name": "Ввод2 М-234 26049958",
        "channel_code": "01",
        "channel_desc": "(A+) на Ввод2 М-234 26049958",
    },
}

# Сколько <period> ожидается в сутках.
EXPECTED_PERIODS = 48

# Знаков после точки в <value>.
VALUE_DECIMALS = 5

# Кодировка итогового XML (и в declaration, и физически в файле).
XML_ENCODING = "windows-1251"

# Суффикс итогового файла: 80020_<ИНН>_<ГГГГММДД>_<суффикс>.xml
OUTPUT_FILE_SUFFIX = "00"

# Форматирование итогового XML (требуется Python 3.9+).
#   ""   - как в «Пирамиде»: перенос строки после каждого тега, без отступов
#   "  " - с отступами для чтения человеком
#   None - не форматировать (одна строка)
XML_INDENT = ""

# Окончание строк: "\r\n" (как в файле «Пирамиды», проверено) или "\n".
LINE_ENDING = "\r\n"

# Привести ПОРЯДОК элементов/атрибутов к виду «Пирамиды» (данные не меняются):
#   message: class, version, number;  sender и area: сначала inn, потом name;
#   period: start, end.  False - оставить порядок как в TaskGroup.
MATCH_PYRAMIDA_LAYOUT = True

# Служебные значения, которые у TaskGroup и «Пирамиды» ОТЛИЧАЮТСЯ.
# По умолчанию None/False = оставить как в TaskGroup. Меняйте только после проверки.
MESSAGE_NUMBER = "0"             # <message number>: Пирамида "0", TaskGroup "1"
DAYLIGHT_SAVING_TIME = None    # <daylightsavingtime>: оставлено "0" как в TaskGroup (у Пирамиды было "1")
REMOVE_AREA_TIMEZONE = True    # <area timezone="1"> удаляется (у Пирамиды атрибута нет)

# Имя лог-файла (пишется в рабочий каталог).
LOG_FILE_NAME = "convert_xml80020.log"

# ============================================================
# КОНЕЦ НАСТРОЕК
# ============================================================

logger = logging.getLogger("convert_xml80020")

FILENAME_RE = re.compile(
    r"^80020_(?P<inn>\d+)_(?P<date>\d{8})_(?P<suffix>\d+)\.xml$",
    re.IGNORECASE,
)
VALUE_QUANTUM = Decimal(1).scaleb(-VALUE_DECIMALS)
OUTPUT_VALUE_RE = re.compile(r"^-?\d+\.\d{%d}$" % VALUE_DECIMALS)
SEP = "=" * 60


class SourceError(Exception):
    """Ошибка конкретного файла/дня. Не останавливает весь запуск."""


# ---------------------------------------------------------------------------
# ЛОГИРОВАНИЕ
# ---------------------------------------------------------------------------

class LogFormatter(logging.Formatter):
    """Консоль: текст как есть; WARNING/ERROR с префиксом. Файл: + время."""

    def __init__(self, with_time: bool):
        super().__init__()
        self.with_time = with_time

    def format(self, record):
        msg = record.getMessage()
        if record.levelno >= logging.ERROR:
            msg = f"[ERROR] {msg}"
        elif record.levelno >= logging.WARNING:
            msg = f"[WARNING] {msg}"
        if self.with_time:
            stamp = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S")
            msg = f"{stamp} | {msg}"
        return msg


def setup_logging(log_path: Path) -> None:
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(LogFormatter(with_time=False))
    logger.addHandler(console)

    file_handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    file_handler.setFormatter(LogFormatter(with_time=True))
    logger.addHandler(file_handler)


# ---------------------------------------------------------------------------
# СЛУЖЕБНЫЕ ФУНКЦИИ
# ---------------------------------------------------------------------------

def coefficient_text() -> str:
    return format(TRANSFORMATION_COEFFICIENT.normalize(), "f")


def date_iso(date_str: str) -> str:
    """20260901 -> 2026-09-01"""
    return datetime.strptime(date_str, "%Y%m%d").strftime("%Y-%m-%d")


def make_output_name(date_str: str) -> str:
    return f"80020_{INN}_{date_str}_{OUTPUT_FILE_SUFFIX}.xml"


def short_path(path: Path, base_dir: Path) -> str:
    try:
        return str(path.relative_to(base_dir))
    except ValueError:
        return str(path)


def log_blank():
    logger.info("")


# ---------------------------------------------------------------------------
# ПОИСК ФАЙЛОВ
# ---------------------------------------------------------------------------

def find_meter_files(base_dir: Path, meter_id: str) -> dict[str, list[Path]]:
    """Возвращает {YYYYMMDD: [файлы]} для каталога одного счётчика."""
    result: dict[str, list[Path]] = {}
    meter_dir = base_dir / meter_id

    for path in sorted(meter_dir.glob("*.xml")):
        match = FILENAME_RE.match(path.name)
        if not match:
            logger.warning("%s: имя не соответствует шаблону 80020, файл пропущен: %s",
                           meter_id, path.name)
            continue
        if match.group("inn") != INN:
            logger.warning("%s: ИНН в имени файла (%s) не равен %s, файл пропущен: %s",
                           meter_id, match.group("inn"), INN, path.name)
            continue
        date_str = match.group("date")
        try:
            datetime.strptime(date_str, "%Y%m%d")
        except ValueError:
            logger.warning("%s: некорректная дата в имени файла, файл пропущен: %s",
                           meter_id, path.name)
            continue
        result.setdefault(date_str, []).append(path)

    return result


# ---------------------------------------------------------------------------
# ЧТЕНИЕ И ПРОВЕРКА ИСХОДНОГО XML
# ---------------------------------------------------------------------------

def parse_xml(path: Path) -> ET.Element:
    """Читает XML. Кодировку берёт из XML declaration файла."""
    if not path.is_file():
        raise SourceError("файл не существует")
    try:
        return ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise SourceError(f"XML не разобран: {exc}") from exc
    except OSError as exc:
        raise SourceError(f"ошибка чтения файла: {exc}") from exc


def validate_xml(root: ET.Element) -> tuple[ET.Element, ET.Element, ET.Element]:
    """
    Проверяет структуру исходного файла.
    Возвращает (area, measuringpoint, measuringchannel).
    """
    if root.tag != "message":
        raise SourceError(f"корневой элемент <{root.tag}> вместо <message>")
    if root.get("class") != "80020":
        raise SourceError(f'class={root.get("class")!r} вместо "80020"')

    sender_inn = root.findtext("sender/inn")
    if sender_inn is not None and sender_inn.strip() != INN:
        raise SourceError(f"ИНН отправителя {sender_inn.strip()!r} не равен {INN}")

    areas = root.findall("area")
    if len(areas) != 1:
        raise SourceError(f"ожидался один <area>, найдено {len(areas)}")
    area = areas[0]

    points = area.findall("measuringpoint")
    if len(points) != 1:
        raise SourceError(f"ожидался один <measuringpoint>, найдено {len(points)}")
    point = points[0]

    channels = point.findall("measuringchannel")
    if len(channels) != 1:
        codes = [c.get("code") for c in channels]
        raise SourceError(
            f"ожидался один <measuringchannel>, найдено {len(channels)} (коды: {codes}); "
            "скрипт не угадывает, какой канал нужен"
        )
    channel = channels[0]

    periods = channel.findall("period")
    if len(periods) != EXPECTED_PERIODS:
        raise SourceError(
            f"найдено {len(periods)} периодов вместо {EXPECTED_PERIODS}"
        )

    for idx, period in enumerate(periods, start=1):
        values = period.findall("value")
        if len(values) != 1:
            raise SourceError(
                f"в периоде №{idx} (start={period.get('start')}) "
                f"найдено <value>: {len(values)} вместо 1"
            )

    return area, point, channel


# ---------------------------------------------------------------------------
# ПРЕОБРАЗОВАНИЕ
# ---------------------------------------------------------------------------

def parse_decimal(text: str | None) -> Decimal:
    """'0,1278' -> Decimal('0.1278'). Без float."""
    if text is None or not text.strip():
        raise ValueError("пустое значение")
    cleaned = text.strip().replace("\u00a0", "").replace(" ", "").replace(",", ".")
    try:
        number = Decimal(cleaned)
    except InvalidOperation:
        raise ValueError(f"не число: {text!r}") from None
    if not number.is_finite():
        raise ValueError(f"недопустимое число: {text!r}")
    return number


def format_value(number: Decimal) -> str:
    """Умножение на коэффициент, 5 знаков, точка как разделитель."""
    result = (number * TRANSFORMATION_COEFFICIENT).quantize(
        VALUE_QUANTUM, rounding=ROUND_HALF_UP
    )
    return format(result, "f")


def convert_values(channel: ET.Element) -> tuple[list[tuple], int]:
    """
    Меняет текст <value> и удаляет у него атрибут status
    (остальные атрибуты не трогаются).
    Возвращает ([(start, end, новое значение)], число значений со status != "0").
    """
    new_values: list[tuple] = []
    nonzero_status = 0

    for idx, period in enumerate(channel.findall("period"), start=1):
        value = period.find("value")
        try:
            new_text = format_value(parse_decimal(value.text))
        except (ValueError, InvalidOperation) as exc:
            raise SourceError(
                f"период №{idx} (start={period.get('start')}): {exc}"
            ) from exc

        status = value.attrib.pop("status", None)
        if status is not None and status.strip() != "0":
            nonzero_status += 1

        value.text = new_text
        new_values.append((period.get("start"), period.get("end"), new_text))

    return new_values, nonzero_status


def convert_measuringpoint(
    point: ET.Element, channel: ET.Element, meter_cfg: dict
) -> tuple[list[tuple], int]:
    """Заменяет идентификаторы точки/канала и пересчитывает значения."""
    point.set("code", meter_cfg["code"])
    point.set("name", meter_cfg["name"])
    channel.set("code", meter_cfg["channel_code"])
    channel.set("desc", meter_cfg["channel_desc"])
    return convert_values(channel)


# ---------------------------------------------------------------------------
# ОБЪЕДИНЕНИЕ И ЗАПИСЬ
# ---------------------------------------------------------------------------

def merge_day(roots: list[ET.Element], points: list[ET.Element]) -> ET.Element:
    """
    Основа — XML первого счётчика (message/datetime/sender/area и т.д.).
    Его <measuringpoint> заменяются на точки всех счётчиков, в порядке METERS.
    """
    merged_root = roots[0]
    area = merged_root.find("area")

    children = list(area)
    first_index = next(
        i for i, child in enumerate(children) if child.tag == "measuringpoint"
    )
    for child in children:
        if child.tag == "measuringpoint":
            area.remove(child)
    for offset, point in enumerate(points):
        area.insert(first_index + offset, point)

    return merged_root


def reorder_attrib(element: ET.Element, order: list[str]) -> None:
    """Меняет порядок атрибутов (значения не меняются)."""
    items = dict(element.attrib)
    element.attrib.clear()
    for key in order:
        if key in items:
            element.set(key, items.pop(key))
    for key, val in items.items():
        element.set(key, val)


def reorder_children(element: ET.Element, first_tags: list[str]) -> None:
    """Ставит дочерние элементы с тегами first_tags в начало; остальные
    идут следом в прежнем порядке."""
    children = list(element)
    head = [c for tag in first_tags for c in children if c.tag == tag]
    tail = [c for c in children if not any(c is h for h in head)]
    for child in children:
        element.remove(child)
    for child in head + tail:
        element.append(child)


def finalize_message(root: ET.Element) -> None:
    """Порядок как в «Пирамиде» + необязательные замены служебных значений."""
    if MESSAGE_NUMBER is not None:
        root.set("number", str(MESSAGE_NUMBER))

    if DAYLIGHT_SAVING_TIME is not None:
        dst = root.find("datetime/daylightsavingtime")
        if dst is None:
            raise SourceError("нет <datetime>/<daylightsavingtime> для замены")
        dst.text = str(DAYLIGHT_SAVING_TIME)

    area = root.find("area")
    if REMOVE_AREA_TIMEZONE:
        area.attrib.pop("timezone", None)

    if MATCH_PYRAMIDA_LAYOUT:
        reorder_attrib(root, ["class", "version", "number"])
        sender = root.find("sender")
        if sender is not None:
            reorder_children(sender, ["inn", "name"])
        reorder_children(area, ["inn", "name"])
        for period in area.iter("period"):
            reorder_attrib(period, ["start", "end"])


def write_xml(root: ET.Element, path: Path) -> None:
    """Пишет XML: declaration и физическая кодировка совпадают."""
    if XML_INDENT is not None and hasattr(ET, "indent"):
        ET.indent(root, space=XML_INDENT)

    body = ET.tostring(root, encoding="unicode")
    text = f'<?xml version="1.0" encoding="{XML_ENCODING}"?>\n{body}\n'
    text = text.replace("\n", LINE_ENDING)
    try:
        data = text.encode(XML_ENCODING)  # strict: без тихой подмены символов
    except UnicodeEncodeError as exc:
        raise SourceError(f"символ не представим в {XML_ENCODING}: {exc}") from exc

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


# ---------------------------------------------------------------------------
# ПРОВЕРКА РЕЗУЛЬТАТА
# ---------------------------------------------------------------------------

def validate_output(path: Path, expected: dict[str, list[tuple]]) -> tuple[list[str], dict[str, int]]:
    """
    Перечитывает созданный файл с диска.
    Возвращает (список проблем, {код точки: число периодов}).
    """
    problems: list[str] = []
    counts: dict[str, int] = {}

    if not path.is_file():
        return ["файл не создан"], counts
    if path.stat().st_size == 0:
        return ["файл пустой"], counts

    data = path.read_bytes()
    head = data[:200].decode("ascii", errors="replace")
    if not re.search(r'encoding="%s"' % re.escape(XML_ENCODING), head, re.IGNORECASE):
        problems.append(f"в XML declaration нет encoding={XML_ENCODING}")
    try:
        data.decode(XML_ENCODING)
    except UnicodeDecodeError:
        problems.append(f"файл физически не в кодировке {XML_ENCODING}")

    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        problems.append(f"XML не открывается: {exc}")
        return problems, counts

    if root.get("class") != "80020":
        problems.append(f'class={root.get("class")!r}, ожидалось "80020"')

    points = root.findall("area/measuringpoint")
    if len(points) != len(METERS):
        problems.append(f"measuringpoint: {len(points)} вместо {len(METERS)}")

    for point, (meter_id, cfg) in zip(points, METERS.items()):
        code = point.get("code")
        if code != cfg["code"]:
            problems.append(f"{meter_id}: code={code!r}, ожидалось {cfg['code']!r}")
        if point.get("name") != cfg["name"]:
            problems.append(f"{meter_id}: name={point.get('name')!r}")

        channels = point.findall("measuringchannel")
        if len(channels) != 1:
            problems.append(f"{meter_id}: каналов {len(channels)} вместо 1")
            continue
        channel = channels[0]
        if channel.get("code") != cfg["channel_code"]:
            problems.append(f"{meter_id}: channel code={channel.get('code')!r}")
        if channel.get("desc") != cfg["channel_desc"]:
            problems.append(f"{meter_id}: channel desc={channel.get('desc')!r}")

        periods = channel.findall("period")
        counts[code] = len(periods)
        if len(periods) != EXPECTED_PERIODS:
            problems.append(f"{meter_id}: периодов {len(periods)} вместо {EXPECTED_PERIODS}")

        actual_values: list[tuple] = []
        for idx, period in enumerate(periods, start=1):
            values = period.findall("value")
            if len(values) != 1:
                problems.append(f"{meter_id}: период №{idx}: <value> {len(values)} вместо 1")
                continue
            value = values[0]
            if "status" in value.attrib:
                problems.append(f"{meter_id}: период №{idx}: остался status у <value>")
            text = (value.text or "").strip()
            if not OUTPUT_VALUE_RE.match(text):
                problems.append(f"{meter_id}: период №{idx}: значение {text!r} не в формате "
                                f"с {VALUE_DECIMALS} знаками")
            actual_values.append((period.get("start"), period.get("end"), text))

        if actual_values != expected.get(meter_id):
            problems.append(f"{meter_id}: периоды/значения в файле не совпадают с исходными и рассчитанными")

    return problems, counts


# ---------------------------------------------------------------------------
# ОБРАБОТКА ДНЯ
# ---------------------------------------------------------------------------

def load_meter_day(base_dir: Path, meter_id: str, path: Path):
    """Читает, проверяет и преобразует файл одного счётчика за день."""
    cfg = METERS[meter_id]
    logger.info("  %s:", meter_id)
    logger.info("    файл найден")

    root = parse_xml(path)
    logger.info("    XML прочитан")

    _area, point, channel = validate_xml(root)
    logger.info("    периодов: %d", len(channel.findall("period")))

    src_code, src_desc = channel.get("code"), channel.get("desc")
    if src_code != cfg["channel_code"] or src_desc != cfg["channel_desc"]:
        logger.info("    канал в источнике: code=%r desc=%r -> code=%r desc=%r",
                    src_code, src_desc, cfg["channel_code"], cfg["channel_desc"])

    new_values, nonzero = convert_measuringpoint(point, channel, cfg)
    logger.info("    значения обработаны")
    logger.info("    коэффициент: x%s", coefficient_text())
    if nonzero:
        logger.warning("%s: значений со status не равным 0: %d (status удалён по настройке)",
                       meter_id, nonzero)

    return root, point, new_values


def process_day(
    base_dir: Path,
    output_dir: Path,
    date_str: str,
    files: dict[str, list[Path]],
) -> str:
    """Обрабатывает один день. Возвращает 'ok' | 'skipped' | 'error'."""
    day_iso = date_iso(date_str)

    # 1. Комплект файлов: нужны оба счётчика.
    missing = [m for m in METERS if not files[m]]
    if missing:
        for meter_id in missing:
            logger.warning("%s: отсутствует файл счётчика %s — день пропущен.",
                           day_iso, meter_id)
        return "skipped"

    # 2. Чтение и преобразование каждого счётчика.
    roots, points = [], []
    expected: dict[str, list[tuple]] = {}
    failed = False

    for meter_id in METERS:
        candidates = files[meter_id]
        path = candidates[0]
        if len(candidates) > 1:
            logger.warning("%s: за %s найдено файлов: %d, используется первый: %s",
                           meter_id, day_iso, len(candidates), path.name)
        try:
            root, point, new_values = load_meter_day(base_dir, meter_id, path)
        except SourceError as exc:
            failed = True
            logger.error("Не удалось обработать:\n%s\n\nПричина: %s",
                         short_path(path, base_dir), exc)
            continue
        roots.append(root)
        points.append(point)
        expected[meter_id] = new_values

    if failed:
        return "error"

    # 3. Объединение и запись.
    logger.info("")
    logger.info("  объединение...")
    output_path = output_dir / make_output_name(date_str)
    if output_path.exists():
        logger.info("  Файл уже существует — будет перезаписан.")

    try:
        merged = merge_day(roots, points)
        finalize_message(merged)
        write_xml(merged, output_path)
    except (SourceError, OSError) as exc:
        logger.error("Не удалось сохранить %s\n\nПричина: %s", output_path.name, exc)
        return "error"

    logger.info("  сохранено:")
    logger.info("    %s", short_path(output_path, base_dir))

    # 4. Контроль результата.
    problems, counts = validate_output(output_path, expected)
    logger.info("  Проверка результата:")
    logger.info("    measuringpoint: %d", len(counts))
    for code, count in counts.items():
        logger.info("    %s: %d периодов", code, count)

    if problems:
        logger.error("Проверка созданного файла %s не пройдена:\n%s",
                     output_path.name, "\n".join("  - " + p for p in problems))
        return "error"

    logger.info("    XML: OK")
    logger.info("  OK")
    return "ok"


# ---------------------------------------------------------------------------
# ОБЩИЙ ЦИКЛ
# ---------------------------------------------------------------------------

def log_header(base_dir: Path, output_dir: Path) -> None:
    logger.info(SEP)
    logger.info("Конвертация XML80020")
    logger.info(SEP)
    logger.info("Рабочий каталог:")
    logger.info("%s", base_dir)
    log_blank()
    logger.info("ИНН: %s", INN)
    logger.info("Коэффициент преобразования: %s", coefficient_text())
    logger.info("Служебные поля: number=%s, daylightsavingtime=%s, area timezone=%s",
                "как в TaskGroup" if MESSAGE_NUMBER is None else MESSAGE_NUMBER,
                "как в TaskGroup" if DAYLIGHT_SAVING_TIME is None else DAYLIGHT_SAVING_TIME,
                "удаляется" if REMOVE_AREA_TIMEZONE else "как в TaskGroup")
    log_blank()
    logger.info("Счётчиков: %d", len(METERS))
    for meter_id, cfg in METERS.items():
        logger.info("  %s -> %s", meter_id, cfg["code"])
    log_blank()
    logger.info("Выходной каталог:")
    logger.info("%s", output_dir)
    logger.info(SEP)


def discover(base_dir: Path) -> tuple[dict[str, dict[str, list[Path]]], list[str]]:
    """Ищет файлы всех счётчиков. Возвращает (файлы по счётчикам, все даты)."""
    logger.info("Поиск файлов...")
    log_blank()

    by_meter = {}
    for meter_id in METERS:
        by_meter[meter_id] = find_meter_files(base_dir, meter_id)
        logger.info("Счётчик %s:", meter_id)
        logger.info("  найдено файлов: %d", len(by_meter[meter_id]))

    all_dates = sorted(set().union(*(set(d) for d in by_meter.values())))
    common = set.intersection(*(set(d) for d in by_meter.values()))

    log_blank()
    logger.info("Обнаружено общих дат: %d", len(common))

    if len(common) != len(all_dates):
        log_blank()
        for meter_id in METERS:
            logger.info("Счётчик %s: %d", meter_id, len(by_meter[meter_id]))
        for meter_id in METERS:
            absent = [d for d in all_dates if d not in by_meter[meter_id]]
            if absent:
                logger.info("Отсутствуют даты у счётчика %s:", meter_id)
                for d in absent:
                    logger.info("  %s", d)

    logger.info(SEP)
    return by_meter, all_dates


def process_all_days(base_dir: Path, output_dir: Path) -> int:
    by_meter, all_dates = discover(base_dir)

    if not all_dates:
        logger.error("XML80020-файлы не найдены.")
        return 3

    output_dir.mkdir(parents=True, exist_ok=True)

    stats = {"ok": 0, "skipped": 0, "error": 0}
    problem_days: dict[str, list[str]] = {"skipped": [], "error": []}
    total = len(all_dates)
    width = len(str(total))

    for number, date_str in enumerate(all_dates, start=1):
        logger.info("[%0*d/%d] %s", width, number, total, date_iso(date_str))
        files = {m: by_meter[m].get(date_str, []) for m in METERS}
        try:
            status = process_day(base_dir, output_dir, date_str, files)
        except Exception:  # непредвиденное: не валим весь запуск
            logger.exception("Непредвиденная ошибка при обработке %s", date_iso(date_str))
            status = "error"
        stats[status] += 1
        if status != "ok":
            problem_days[status].append(date_str)
        log_blank()

    logger.info(SEP)
    logger.info("ОБРАБОТКА ЗАВЕРШЕНА")
    logger.info(SEP)
    log_blank()
    logger.info("Дней найдено:        %4d", total)
    logger.info("Успешно обработано:  %4d", stats["ok"])
    logger.info("Пропущено:           %4d", stats["skipped"])
    logger.info("Ошибок:              %4d", stats["error"])
    log_blank()
    logger.info("Создано XML:         %4d", stats["ok"])

    if problem_days["skipped"]:
        log_blank()
        logger.info("Пропущенные даты (нет файла одного из счётчиков):")
        for d in problem_days["skipped"]:
            logger.info("  %s", d)
    if problem_days["error"]:
        log_blank()
        logger.info("Даты с ошибками:")
        for d in problem_days["error"]:
            logger.info("  %s", d)

    log_blank()
    logger.info("Выходной каталог:")
    logger.info("%s", output_dir)
    logger.info(SEP)

    return 0 if stats["error"] == 0 else 4


def main() -> int:
    base_dir = Path.cwd()
    output_dir = base_dir / OUTPUT_DIR_NAME
    setup_logging(base_dir / LOG_FILE_NAME)

    log_header(base_dir, output_dir)

    missing_dirs = [m for m in METERS if not (base_dir / m).is_dir()]
    if missing_dirs:
        for meter_id in missing_dirs:
            logger.error("Каталог счётчика не найден: %s", base_dir / meter_id)
        logger.error("Запускайте скрипт из каталога, где лежат папки счётчиков.")
        return 2

    return process_all_days(base_dir, output_dir)


if __name__ == "__main__":
    raise SystemExit(main())
