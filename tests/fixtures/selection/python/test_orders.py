from orders import normalize_reference


def test_normalizes_reference():
    assert normalize_reference(" abc ") == "ABC"
