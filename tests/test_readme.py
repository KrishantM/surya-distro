import unittest

from surya_distro.readme import parse, plain, sentences
from tests.fakes import README


class ReadmeFacts(unittest.TestCase):
    def setUp(self):
        self.r = parse(README)

    def test_title_and_summary_skip_badges(self):
        self.assertEqual(self.r.title, "Lantern")
        self.assertTrue(self.r.summary.startswith("Lantern keeps a log of what your AI agent changed."))
        self.assertNotIn("badge", self.r.summary)

    def test_bullets_keep_their_bold_lead(self):
        self.assertEqual([b.lead for b in self.r.bullets], ["One file per session", "No server", "Small"])
        self.assertEqual(self.r.bullets[1].text, "No server. It runs on your machine and stores nothing elsewhere.")

    def test_install_is_the_first_code_block_in_its_section(self):
        self.assertEqual(self.r.install, "pip install lantern-log")
        self.assertTrue(self.r.has_install)

    def test_limits_comparison_and_tests(self):
        self.assertEqual(self.r.limits, ["Built and tested on macOS.", "It doesn't redact secrets yet."])
        self.assertIn("hosted dashboard", self.r.comparison)
        self.assertTrue(self.r.has_tests)

    def test_badges_are_not_images(self):
        self.assertEqual(self.r.images, 0)
        self.assertEqual(parse("# X\n\nText.\n\n![demo](docs/demo.gif)\n").images, 1)

    def test_a_hash_inside_code_is_not_a_heading(self):
        r = parse("# Tool\n\nDoes a thing.\n\n```bash\n# not a heading\nrun\n```\n")
        self.assertEqual([s.heading for s in r.sections if s.heading], ["Tool"])

    def test_no_title_still_finds_a_summary(self):
        r = parse("Some tool that does one thing.\n\n- a\n- b\n")
        self.assertEqual(r.title, "")
        self.assertEqual(r.summary, "Some tool that does one thing.")
        self.assertEqual(len(r.bullets), 2)

    def test_plain_and_sentences(self):
        self.assertEqual(plain("**Bold** and [link](https://x.y) with `code`"), "Bold and link with code")
        self.assertEqual(sentences("One. Two three. 4 four."), ["One.", "Two three.", "4 four."])


if __name__ == "__main__":
    unittest.main()
