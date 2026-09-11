import pytest

from pyedit.session import BudgetExceeded, EditSession


def test_write_budget_trips_on_growth(project):
    session = EditSession(max_bytes=10)
    session.write("src/a.py", "12345")  # 5 bytes, within budget
    with pytest.raises(BudgetExceeded, match="memory budget"):
        session.write("src/b.py", "12345678901")  # 11 bytes -> 16 > 10


def test_budget_counts_utf8_bytes(project):
    session = EditSession(max_bytes=1)
    with pytest.raises(BudgetExceeded):
        session.write("src/x.py", "é")  # 2 utf-8 bytes


def test_replacement_counts_only_growth(project):
    session = EditSession(max_bytes=6)
    session.write("src/a.py", "123456")
    session.write("src/a.py", "123")  # shrink: fine
    session.write("src/a.py", "123456")  # back to 6: fine
    with pytest.raises(BudgetExceeded):
        session.write("src/a.py", "1234567")


def test_delete_frees_budget(project):
    session = EditSession(max_bytes=10)
    session.write("src/a.py", "1234567890")
    session.delete("src/a.py")
    session.write("src/b.py", "1234567890")  # budget fully freed
    assert session.staged()[project / "src" / "b.py"] == "1234567890"


def test_file_budget_trips_on_third_file(project):
    session = EditSession(max_files=2)
    session.write("src/1.py", "x")
    session.write("src/2.py", "x")
    with pytest.raises(BudgetExceeded, match="file budget"):
        session.write("src/3.py", "x")
    session.delete("src/1.py")
    session.write("src/3.py", "x")  # slot freed


def test_materializing_read_is_budgeted(project):
    (project / "src" / "big.bin").write_bytes(b"x" * 100)
    session = EditSession(max_bytes=50)
    with pytest.raises(BudgetExceeded, match="memory budget"):
        session.read("src/big.bin")


def test_prune_unchanged_releases_budget(project):
    session = EditSession(max_files=1)
    session.read("src/a.py")  # materialized: 1 file used
    with pytest.raises(BudgetExceeded):
        session.write("src/brand_new.py", "x\n")  # would be 2 -> trips
    session.prune_unchanged()  # materialized read was unchanged: freed
    session.write("src/brand_new.py", "x\n")
    session.prune_unchanged()  # materialized read was unchanged: freed
    session.write("src/brand_new.py", "x\n")


def test_none_disables_budgets(project):
    session = EditSession()
    session.write("src/a.py", "x" * 100000)
    session.write("src/b.py", "x" * 100000)
    assert len(session.staged()) == 2
