import unittest

from validator import parse_port


class ParsePortTest(unittest.TestCase):
    def test_lower_boundary_one_is_valid(self):
        self.assertEqual(parse_port("1"), 1)

    def test_upper_boundary_65535_is_valid(self):
        self.assertEqual(parse_port("65535"), 65535)

    def test_zero_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_port("0")

    def test_65536_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_port("65536")

    def test_ordinary_port_is_valid(self):
        self.assertEqual(parse_port("8080"), 8080)

    def test_non_decimal_format_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_port("80a")

    def test_empty_string_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_port("")

    def test_unicode_digits_are_rejected(self):
        for text in ("１２", "١٢"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_port(text)

    def test_signs_and_whitespace_are_rejected(self):
        for text in ("+80", "-80", " 80", "80 ", "1.5"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_port(text)


if __name__ == "__main__":
    unittest.main()
