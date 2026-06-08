"""
Тесты парсера на координатной фикстуре реального отчёта (день после 23:00, ∅).
Фикстура — слова с координатами (геометрия настоящей вёрстки), email обезличен.

Запуск:  python tests/test_parser.py   ИЛИ   python -m pytest tests/
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from parser import parse_words, words_from_tuples, seconds_to_time, detect_report_kind  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sample_2026-06-05.words.json")
FIXTURE_WEEKLY = os.path.join(os.path.dirname(__file__), "fixtures", "sample_weekly_2026-05-25.words.json")


def load_words():
    data = json.load(open(FIXTURE, encoding="utf-8"))
    return words_from_tuples(data["words"])


def load_weekly_words():
    data = json.load(open(FIXTURE_WEEKLY, encoding="utf-8"))
    return words_from_tuples(data["words"])


def test_daily_report_detected_as_daily():
    assert detect_report_kind(load_words()) == "daily"


def test_weekly_report_detected_and_rejected():
    """Недельный отчёт (Twoje tygodniowe podsumowanie) НЕ должен приниматься как дневной."""
    assert detect_report_kind(load_weekly_words()) == "weekly"


def test_empty_delivery_is_none_not_a_neighbor_value():
    """
    Корень проблемы — вёрстка: значение времени доставки = ∅ (левая колонка).
    Парсер НЕ должен подхватить ни время выезда (01:34, ниже), ни значение соседней
    правой карточки (0). Время доставки = None, а время выезда читается отдельно.
    """
    r = parse_words(load_words(), filename="2026-06-05-Twoje-podsumowanie.pdf")
    assert r.delivery_sec is None, f"ожидали None (∅), получили {r.delivery_sec}"
    assert r.start_sec == 1 * 60 + 34, f"время выезда: {r.start_sec}"  # 01:34 — отдельное поле


def test_date_extracted():
    assert parse_words(load_words()).report_date == "2026-06-05"


def test_orders_and_earnings():
    r = parse_words(load_words())
    assert r.delivered_orders == 8, r.delivered_orders
    assert r.orders_all == 8, r.orders_all
    assert r.orders_before_23 == 0, r.orders_before_23   # смена 23:00 -> 0 заказов до 23:00
    assert abs(r.earnings - 112.5) < 1e-6, r.earnings


def test_seconds_to_time():
    assert seconds_to_time(94) == "1:34"
    assert seconds_to_time(None) == "∅"
    assert seconds_to_time(8 * 60) == "8:00"


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS  {name}")
            except AssertionError as e:
                failed += 1
                print(f"FAIL  {name}: {e}")
    print("\nALL GREEN" if not failed else f"\n{failed} FAILED")
    sys.exit(1 if failed else 0)
