"""
Извлечение полей из отчёта курьера (Twoje podsumowanie / Looker PDF).

ПОЧЕМУ КООРДИНАТЫ, А НЕ РЕГЭКСП ПО ТЕКСТУ.
Отчёт свёрстан сеткой «карточек» в две колонки. Значение лежит не рядом со своей
подписью, а в отдельной ячейке под ней. Когда PDF разворачивают в плоский текст,
порядок строк перемешивается, и подпись со значением «расходятся» — поэтому приём
«первое число после метки» хватает чужое значение (раньше так время доставки
подменялось временем выезда).

Правильный приём: найти подпись, взять значение строго ПОД ней и В ТОЙ ЖЕ колонке
по X. Это не зависит от того, как PDF свернулся в текст.

Парсер работает со списком слов с координатами (Word) — его можно тестировать на
JSON-фикстуре реального отчёта без самого PDF.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict, field
from typing import Optional


_TIME_FULL = re.compile(r"^(\d{1,2}):(\d{2})$")
_EMPTY_MARKERS = {"∅", "-", "—", "–", "n/a", "N/A"}

# Геометрические допуски (в единицах PDF). Подобраны по реальному отчёту:
# значение-карточка стоит на ~60–70 ниже подписи, следующая секция — на ~210 ниже.
_VALUE_MAX_GAP = 150     # макс. вертикальный разрыв подпись -> её значение
_COL_X_PAD = 35          # допуск по X, чтобы значение считалось «в колонке подписи»


@dataclass
class Word:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str

    @property
    def xc(self) -> float:
        return (self.x0 + self.x1) / 2


@dataclass
class ParsedReport:
    report_date: Optional[str]        # 'YYYY-MM-DD'
    delivery_sec: Optional[int]       # None, если ∅ (смена после 23:00)
    start_sec: Optional[int]          # среднее время выезда, считается всегда
    delivered_orders: int
    orders_before_23: int             # вес для среднего доставки
    orders_all: int
    earnings: float
    raw_filename: Optional[str] = None

    def as_dict(self) -> dict:
        return asdict(self)


# ─────────────────────────── утилиты ───────────────────────────
def words_from_tuples(raw) -> list[Word]:
    """Список [x0,y0,x1,y1,text] (формат PyMuPDF get_text('words')/фикстуры) -> [Word]."""
    return [Word(float(x0), float(y0), float(x1), float(y1), t) for x0, y0, x1, y1, t, *_ in raw]


def _time_to_seconds(value: str) -> Optional[int]:
    m = _TIME_FULL.match(value.strip())
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def seconds_to_time(seconds: Optional[int]) -> str:
    """Секунды -> 'M:SS'. None -> '∅'."""
    if seconds is None:
        return "∅"
    minutes, sec = divmod(int(round(seconds)), 60)
    return f"{minutes}:{sec:02d}"


@dataclass
class _Label:
    x0: float
    x1: float
    y: float          # верх подписи


def _find_label(words: list[Word], phrase: str) -> Optional[_Label]:
    """Найти подпись из нескольких слов, стоящих подряд на ~одной строке."""
    toks = phrase.split()
    n = len(toks)
    for i in range(len(words) - n + 1):
        window = words[i : i + n]
        if [w.text for w in window] != toks:
            continue
        ys = [w.y0 for w in window]
        if max(ys) - min(ys) > 6:          # должны быть на одной строке
            continue
        return _Label(min(w.x0 for w in window), max(w.x1 for w in window), window[0].y0)
    return None


def _value_below(words: list[Word], label: _Label,
                 max_gap: float = _VALUE_MAX_GAP, x_pad: float = _COL_X_PAD) -> Optional[str]:
    """
    Первое (самое верхнее) слово ПОД подписью, чей центр по X попадает в её колонку
    и которое не дальше max_gap по вертикали. Это значение нужной карточки.
    """
    cands = [
        w for w in words
        if w.y0 > label.y
        and (w.y0 - label.y) <= max_gap
        and (label.x0 - x_pad) <= w.xc <= (label.x1 + x_pad)
    ]
    if not cands:
        return None
    cands.sort(key=lambda w: w.y0)
    return cands[0].text


def _card_time(words: list[Word], *label_phrases: str) -> Optional[int]:
    """Время из карточки: ищем подпись (рус/англ), берём значение под ней.
    Возвращает секунды, либо None если значение пустое (∅) или это не время."""
    for phrase in label_phrases:
        label = _find_label(words, phrase)
        if not label:
            continue
        val = _value_below(words, label)
        if val is None:
            return None
        if val in _EMPTY_MARKERS or "∅" in val:
            return None
        return _time_to_seconds(val)
    return None


def _card_int(words: list[Word], *label_phrases: str) -> Optional[int]:
    for phrase in label_phrases:
        label = _find_label(words, phrase)
        if not label:
            continue
        val = _value_below(words, label)
        if val and val.isdigit():
            return int(val)
    return None


# ─────────────────────────── текстовые поля ───────────────────────────
def _flat_text(words: list[Word]) -> str:
    """Грубая склейка в текст для полей, где вёрстка не мешает (дата, заработок)."""
    return " ".join(w.text for w in words)


def extract_report_date(words: list[Word], filename: Optional[str] = None) -> Optional[str]:
    text = _flat_text(words)
    m = re.search(r"Work Date is on\s+(\d{4})[/-](\d{2})[/-](\d{2})", text)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    if filename:
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", filename)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


def _extract_earnings(words: list[Word]) -> float:
    """Первое число с 'zł' после первого 'Total earnings'."""
    text = _flat_text(words)
    if "Total earnings" not in text:
        return 0.0
    area = text.split("Total earnings", 1)[-1]
    nums = re.findall(r"(\d+[.,]?\d*)(\s*zł)?", area)
    seen_plain = False
    for value, is_zloty in nums:
        if not is_zloty:
            seen_plain = True
        elif seen_plain and is_zloty:
            return float(value.replace(",", "."))
    return 0.0


def _extract_hour_table(words: list[Word]) -> tuple[int, int]:
    """
    Таблица заказов по часам: 'jush 23:00 8 ...'. -> (orders_before_23, orders_all).
    NB: hour < 23 наивно относит часы после полуночи к 'до 23:00' — приближение для
    ночных сквозных смен. TODO пересмотреть на реальных ночных отчётах.
    """
    rows = re.findall(r"jush\s+(\d{1,2}):00\s+(\d+)\b", _flat_text(words))
    before_23 = total = 0
    for hour, orders in rows:
        h, o = int(hour), int(orders)
        total += o
        if h < 23:
            before_23 += o
    return before_23, total


# ─────────────────────────── главный разбор ───────────────────────────
def parse_words(words: list[Word], filename: Optional[str] = None) -> ParsedReport:
    before_23, orders_all = _extract_hour_table(words)
    return ParsedReport(
        report_date=extract_report_date(words, filename),
        # время доставки — координатно (левая карточка); ∅ -> None
        delivery_sec=_card_time(words, "Average order delivery time", "Średni czas dostawy"),
        start_sec=_card_time(words, "Average Jush task start time", "Średni czas wyjazdu z"),
        delivered_orders=_card_int(words, "Number of delivered orders") or 0,
        orders_before_23=before_23,
        orders_all=orders_all,
        earnings=_extract_earnings(words),
        raw_filename=filename,
    )


# Маркеры типа отчёта. Дневной: "Work Date is on <дата>". Недельный ("tygodniowe"):
# заголовок + диапазон дат "Work Date is from ... until ..." / "Summary Range".
_WEEKLY_MARKERS = ("tygodniowe", "summary range", "zakres podsumowania", "work date is from")
_DAILY_MARKER = "work date is on"


# Имя файла отчёта: дата в начале + 'podsumowanie' + '.pdf'. Примеры:
#   '2026-06-05-Twoje-podsumowanie.pdf'
#   '2026-05-25-to-2026-05-31-Twoje-tygodniowe-podsumowanie.pdf'
_REPORT_FILENAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}.*podsumowanie.*\.pdf$", re.IGNORECASE)


def looks_like_report_filename(filename) -> bool:
    """Дешёвый фильтр ДО скачивания: похоже ли имя файла на отчёт."""
    return bool(filename and _REPORT_FILENAME_RE.match(filename.strip()))


def detect_report_kind(words: list[Word]) -> str:
    """Тип отчёта: 'daily' | 'weekly' | 'unknown'. Принимаем только 'daily'."""
    tl = _flat_text(words).lower()
    if any(m in tl for m in _WEEKLY_MARKERS):
        return "weekly"
    if _DAILY_MARKER in tl:
        return "daily"
    return "unknown"


def extract_words_from_pdf(data: bytes) -> list[Word]:
    """Слова с координатами из первой страницы PDF (PyMuPDF)."""
    import fitz

    page = fitz.open(stream=data, filetype="pdf")[0]
    return words_from_tuples(page.get_text("words"))


def parse_pdf(data: bytes, filename: Optional[str] = None) -> ParsedReport:
    """Разбор PDF-байтов в ParsedReport (без проверки типа отчёта)."""
    return parse_words(extract_words_from_pdf(data), filename)
