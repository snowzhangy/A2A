import unittest

from context_translation import payload_for


class ContextPromptTests(unittest.TestCase):
    def test_only_supplied_previous_source_and_relevant_terms_enter_prompt(self):
        history = [dict(unit=f"u{i}", source=f"prior {i}", target="incorrect earlier translation")
                   for i in range(10)]
        corrected, prompt = payload_for("店小二", history, {
            "glossary": {"店小二": "waiter", "无关术语": "irrelevant term"}})
        self.assertEqual(corrected, "店小二")
        self.assertEqual([h["unit"] for h in prompt["preceding"]], [f"u{i}" for i in range(2, 10)])
        self.assertEqual(prompt["terminology"], {"店小二": "waiter"})
        self.assertNotIn("incorrect earlier translation", str(prompt))

    def test_dev_aliases_are_explicit_and_precede_term_selection(self):
        corrected, prompt = payload_for("电小二", [], {
            "asr_aliases": {"电小二": "店小二"}, "glossary": {"店小二": "waiter"}})
        self.assertEqual(corrected, "店小二")
        self.assertEqual(prompt["CURRENT"], corrected)
        self.assertEqual(prompt["terminology"], {"店小二": "waiter"})
        unchanged, _ = payload_for("电小二", [], {})
        self.assertEqual(unchanged, "电小二")


if __name__ == "__main__":
    unittest.main()
