"""
Тесты хранилища: дедуп, недельная агрегация (∅-дни), подписки.
Запуск:  python tests/test_db.py
"""

import os
import sys
import tempfile
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# временная база — задать ДО импорта db (DB_PATH читается при импорте)
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["DB_PATH"] = _tmp.name

import db  # noqa: E402
from parser import ParsedReport  # noqa: E402

db.init_db()


def rep(d, delivery_sec, n, earnings=100.0):
    return ParsedReport(d, delivery_sec, 90, n, n, n, earnings, "x.pdf")


def test_dedup_same_day():
    assert db.upsert_report(1, rep("2026-06-02", 480, 10)) == "new"
    assert db.upsert_report(1, rep("2026-06-02", 480, 10)) == "updated"


def test_weekly_aggregation_excludes_empty_days():
    db.upsert_report(2, rep("2026-06-02", 8 * 60, 10))   # 8:00, 10 заказов
    db.upsert_report(2, rep("2026-06-03", 10 * 60, 5))   # 10:00, 5 заказов
    db.upsert_report(2, rep("2026-06-05", None, 8))      # ∅-день
    w = db.weekly_stats(2, ref=date(2026, 6, 5))
    assert w.days_total == 3 and w.days_with_delivery == 2
    # (480*10 + 600*5) / 15 = 520
    assert w.avg_delivery_sec == 520, w.avg_delivery_sec


def test_weeks_split():
    db.upsert_report(3, rep("2026-06-02", 480, 5))
    db.upsert_report(3, rep("2026-06-09", 540, 5))   # следующая неделя
    weeks = db.all_weeks_stats(3)
    assert len(weeks) == 2
    assert weeks[0].week_start == "2026-06-01" and weeks[1].week_start == "2026-06-08"


def test_user_isolation():
    db.upsert_report(4, rep("2026-06-02", 480, 5))
    db.upsert_report(5, rep("2026-06-02", 999, 7))
    assert db.all_weeks_stats(4)[0].total_orders == 5
    assert db.all_weeks_stats(5)[0].total_orders == 7
    db.delete_user_data(4)
    assert not db.all_weeks_stats(4) and db.all_weeks_stats(5)


def test_subscription_lifecycle():
    U = 100
    assert not db.is_subscribed(U)
    until = db.grant_subscription(U, 30)
    assert db.is_subscribed(U)
    assert until == (date.today() + timedelta(days=30)).isoformat()
    # продление идёт от конца активной подписки
    until2 = db.grant_subscription(U, 30)
    assert until2 == (date.today() + timedelta(days=60)).isoformat()
    db.revoke_subscription(U)
    assert not db.is_subscribed(U)


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
    os.unlink(_tmp.name)
    print("\nALL GREEN" if not failed else f"\n{failed} FAILED")
    sys.exit(1 if failed else 0)
