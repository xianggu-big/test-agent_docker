from calc import add, div


def test_add():
    assert add(1, 2) == 3


def test_div_wrong_expectation():
    assert div(4, 2) == 5
