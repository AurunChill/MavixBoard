import pytest
from mavixboard.token.generator import generate


def test_even_length_returns_correct_length():
    assert len(generate(4)) == 4

def test_odd_length_returns_correct_length():
    assert len(generate(5)) == 5

def test_length_one():
    assert len(generate(1)) == 1

def test_length_two():
    assert len(generate(2)) == 2

def test_zero_returns_empty_string():
    assert generate(0) == ''

def test_negative_returns_empty_string():
    assert generate(-1) == ''

def test_wrong_type_raises_type_error():
    with pytest.raises(TypeError):
        generate("10")

def test_float_raises_type_error():
    with pytest.raises(TypeError):
        generate(4.0)

def test_none_raises_type_error():
    with pytest.raises(TypeError):
        generate(None)

def test_bool_treated_as_int():
    assert len(generate(True)) == 1

def test_returns_hex_string():
    result = generate(8)
    assert all(c in "0123456789abcdef" for c in result)

def test_output_is_different_each_call():
    results = {generate(16) for _ in range(10)}
    assert len(results) > 1
