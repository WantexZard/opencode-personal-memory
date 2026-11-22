"""Synthetic-only tests for the eight FICTIONAL rule seed candidates.

These patterns are FICTIONAL TEST DATA for the manual seed route (limited
statistical evidence accepted). They do not describe any real user; they are
invented examples that exercise the rule engine. The runtime must never
auto-import or apply them: no production rules/Memory/DB/manifest files are
read or written here. SEED_ADDITIONS is a module constant so the parent can
construct the exact rule diff in RAM; applying rules / prepare / commit /
Learning batch creation remain unapproved and are NOT exercised.

Limitation (documented, not guaranteed): regex matching does not understand
negation, quotes, or intent. A regex HIT is only statistical evidence; every
candidate still requires semantic review before any rule is applied.
"""

import importlib.util
import json
import sys
from pathlib import Path
import re
import unittest

sys.dont_write_bytecode = True

MEMORY_OPS = Path(__file__).absolute().parents[1] / "memory_ops.py"
BUNDLED_RULES = Path(__file__).absolute().parents[1] / "examples" / "rules.json"
_SPEC = importlib.util.spec_from_file_location("memory_ops_seed_test", MEMORY_OPS)
_ops = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_ops)

# Eight FICTIONAL seed additions: Preference 6, Project 1, Remember 1.
# Invented generically; not derived from any real conversation.
SEED_ADDITIONS = {
    "Preference": [
        r"我(?:喜欢|偏好|习惯|倾向于)",
        r"我(?:不喜欢|不接受|讨厌)",
        r"以后(?:都|一直|默认)",
        r"尽量.{0,8}(?:简单|简洁|少一点)",
        r"不确定.{0,12}(?:先问我|问我|确认一下)",
        r"(?:prefer|favor)[\s\S]{0,20}(?:simple|plain|minimal)",
    ],
    "Project": [
        r"这个项目|这个仓库|这次任务",
    ],
    "Remember": [
        r"记一下|记住|帮我记|请记下",
    ],
}

PREFERENCE = "Preference"
PROJECT = "Project"
REMEMBER = "Remember"


def load_engine(additions):
    """Compile via the existing pure memory_ops.validate_rules engine
    (regex patterns -> re.IGNORECASE, plain strings -> substring lower)."""
    return _ops.validate_rules(additions)


def match_any(engine, categories, text):
    for cat in categories:
        for pat in engine.get(cat, []):
            if isinstance(pat, re.Pattern):
                if pat.search(text):
                    return cat, pat.pattern
            elif pat in text.lower():
                return cat, pat
    return None, None


def compose_proposal(existing, additions):
    """Append-compose a proposal fixture: preserves preexisting entries,
    unknown categories and their order; additions append per category.
    Restricted to this test module; no production file access."""
    proposal = {k: list(v) for k, v in existing.items()}
    for cat, patterns in additions.items():
        proposal.setdefault(cat, [])
        for pat in patterns:
            if pat not in proposal[cat]:
                proposal[cat].append(pat)
    return proposal


class TestSeedAdditions(unittest.TestCase):
    def setUp(self):
        self.engine = load_engine(SEED_ADDITIONS)

    # --- structure --------------------------------------------------------

    def test_at_most_eight_additions_three_categories(self):
        self.assertEqual(len(SEED_ADDITIONS), 3)
        total = sum(len(v) for v in SEED_ADDITIONS.values())
        self.assertEqual(total, 8)
        self.assertEqual(set(SEED_ADDITIONS), {PREFERENCE, PROJECT, REMEMBER})
        self.assertEqual(len(SEED_ADDITIONS[PREFERENCE]), 6)
        self.assertEqual(len(SEED_ADDITIONS[PROJECT]), 1)
        self.assertEqual(len(SEED_ADDITIONS[REMEMBER]), 1)

    def test_all_eight_compile_via_engine(self):
        # validate_rules raises on any invalid pattern, so a successful
        # compile proves all eight are valid regex-vs-substring syntax
        for cat in (PREFERENCE, PROJECT, REMEMBER):
            self.assertIn(cat, self.engine)
            for pat in self.engine[cat]:
                self.assertTrue(
                    isinstance(pat, re.Pattern) or isinstance(pat, str), cat
                )

    # --- family positives (>=3 per family) ---------------------------------

    def test_preference_positives(self):
        positives = [
            "我喜欢简洁的实现",
            "我不接受太复杂的方案",
            "以后都默认这样处理",
            "代码尽量简单一点",
            "不确定的时候先问我",
            "I prefer plain output",
        ]
        for text in positives:
            cat, _pat = match_any(self.engine, [PREFERENCE], text)
            self.assertEqual(cat, PREFERENCE, text)

    def test_project_positives(self):
        positives = [
            "这个项目先别动",
            "这个仓库要小心处理",
            "这次任务优先级高",
        ]
        for text in positives:
            cat, _pat = match_any(self.engine, [PROJECT], text)
            self.assertEqual(cat, PROJECT, text)

    def test_remember_positives_without_colon(self):
        positives = [
            "记住这一点",
            "帮我记下这个参数",
            "记一下我常用的路径",
            "请你帮我记住",
        ]
        for text in positives:
            cat, _pat = match_any(self.engine, [REMEMBER], text)
            self.assertEqual(cat, REMEMBER, text)

    # --- negatives ---------------------------------------------------------

    def test_no_trigger_on_bare_opinion_and_unrelated_questions(self):
        negatives = [
            "我觉得这个方案还行",  # bare 我觉得
            "今天天气怎么样",
            "这个函数怎么写",
            "帮我看看这个报错是什么意思",
        ]
        for text in negatives:
            cat, _pat = match_any(self.engine, [PREFERENCE, PROJECT, REMEMBER], text)
            self.assertIsNone(cat, text)

    def test_no_trigger_on_procedural_denials(self):
        # one-shot denials must not trigger ANY of the eight patterns
        for text in ("此步不允许修改文件", "我已确认这次修改"):
            cat, _pat = match_any(self.engine, [PREFERENCE, PROJECT, REMEMBER], text)
            self.assertIsNone(cat, text)

    def test_plain_remember_denial_negative(self):
        cat, _pat = match_any(self.engine, [REMEMBER], "不要记录这件事")
        self.assertIsNone(cat)

    # --- engine semantics ---------------------------------------------------

    def test_tendency_and_default_language(self):
        tendency = [
            "我倾向于用更简单的写法",
            "我习惯先确认一下",
            "以后都默认用这个格式",
        ]
        for text in tendency:
            cat, _pat = match_any(self.engine, [PREFERENCE], text)
            self.assertEqual(cat, PREFERENCE, text)

    def test_case_insensitive_ascii_match(self):
        cat, _pat = match_any(self.engine, [PREFERENCE], "I PREFER Simple Output")
        self.assertEqual(cat, PREFERENCE)

    def test_multiline_match(self):
        text = "I prefer\nsimple\noutput"
        cat, _pat = match_any(self.engine, [PREFERENCE], text)
        self.assertEqual(cat, PREFERENCE)

    def test_ambiguous_quoted_principle_hits_but_needs_semantic_review(self):
        # EXPECTED regex HIT on a quoted third-party statement: the engine
        # has no notion of quoting or intent. This is statistical evidence
        # only — semantic review is still REQUIRED before applying; this
        # assertion documents the hit, not that the text is a safe fact.
        text = '他写道："我习惯用简单方案"'
        cat, _pat = match_any(self.engine, [PREFERENCE], text)
        self.assertEqual(cat, PREFERENCE)

    # --- proposal composition fixture ---------------------------------------

    def test_compose_preserves_existing_entries_unknown_categories_order(self):
        existing = {
            "Preference": ["已有偏好规则"],
            "UnknownCat": ["未知类别保留"],
        }
        proposal = compose_proposal(existing, SEED_ADDITIONS)
        # preexisting entries and unknown categories preserved first
        self.assertEqual(proposal["Preference"][0], "已有偏好规则")
        self.assertIn("UnknownCat", proposal)
        self.assertEqual(proposal["UnknownCat"], ["未知类别保留"])
        # additions appended per category, no duplicates
        for cat, patterns in SEED_ADDITIONS.items():
            for pat in patterns:
                self.assertIn(pat, proposal[cat])
            self.assertEqual(
                len(proposal[cat]),
                len(set(proposal[cat])),
                cat,
            )
        # no production file access: everything stays in RAM
        self.assertEqual(len(proposal["Preference"]), 7)


class TestBundledExampleRules(unittest.TestCase):
    """Bounded consistency check against the repo's bundled examples/rules.json.

    This class reads the shipped EXAMPLE file (not a production rules file and
    not a copy embedded in this test), compiles it with the existing
    memory_ops.validate_rules engine, and checks the English seed families that
    the example file adds on top of SEED_ADDITIONS above.

    All example sentences are invented generic fixtures; none is a real user
    preference. The file access is limited to examples/rules.json.
    """

    @classmethod
    def setUpClass(cls):
        assert "learning" not in BUNDLED_RULES.parts, BUNDLED_RULES
        assert BUNDLED_RULES.parent.name == "examples", BUNDLED_RULES
        assert BUNDLED_RULES.name == "rules.json", BUNDLED_RULES
        with open(BUNDLED_RULES, encoding="utf-8") as handle:
            cls.bundled = json.load(handle)
        cls.engine = _ops.validate_rules(cls.bundled)
        cls.seed_only_engine = _ops.validate_rules(SEED_ADDITIONS)

    # --- proof that the assertions target the bundled file -------------------

    def test_bundled_file_not_the_in_test_constant(self):
        # The file carries the historical eight entries...
        for cat, patterns in SEED_ADDITIONS.items():
            for pat in patterns:
                self.assertIn(pat, self.bundled[cat], cat)
        # ...plus English entries that exist nowhere in this test module's
        # SEED_ADDITIONS, so the loaded object cannot be a re-declared copy.
        english = [
            r"\bI\s+(?:prefer|like|dislike)\b",
            r"\bI(?:'m| am)\s+used to\b",
            r"\bfrom now on\b",
        ]
        for pat in english:
            self.assertIn(pat, self.bundled[PREFERENCE])
            self.assertNotIn(pat, SEED_ADDITIONS[PREFERENCE])
        self.assertEqual(len(self.bundled[PREFERENCE]), 9)
        self.assertEqual(len(self.bundled[PROJECT]), 2)
        self.assertEqual(len(self.bundled[REMEMBER]), 2)

    def test_new_english_matches_come_only_from_bundled_rules(self):
        # Under SEED_ADDITIONS alone these must NOT match; they match only via
        # entries that exist in examples/rules.json.
        bundled_only = [
            "I dislike noisy logs",
            "I'm used to short names",
            "From now on use the demo format",
            "This repo needs a cleanup",
            "make a note about the demo",
        ]
        for text in bundled_only:
            cat, _pat = match_any(self.seed_only_engine, [PREFERENCE], text)
            self.assertIsNone(cat, text)
            categories = _ops.match_rules(text, self.engine)
            self.assertTrue(categories, text)

    # --- English examples, all fictional --------------------------------

    def test_english_preference_examples(self):
        positives = [
            "I prefer simple names",
            "I like plain output",
            "I dislike noisy logs",
            "I'm used to short names",
            "I am used to small diffs",
            "From now on use the demo format",
        ]
        for text in positives:
            categories = _ops.match_rules(text, self.engine)
            self.assertIn(PREFERENCE, categories, text)

    def test_english_project_examples(self):
        positives = [
            "This project keeps the demo simple",
            "this repository uses the demo layout",
            "This repo needs a cleanup",
            "this task is small",
        ]
        for text in positives:
            categories = _ops.match_rules(text, self.engine)
            self.assertIn(PROJECT, categories, text)

    def test_english_remember_examples(self):
        positives = [
            "Remember this: the demo value is 42",
            "remember that the demo flag is off",
            "Please remember the demo path",
            "make a note about the demo",
        ]
        for text in positives:
            categories = _ops.match_rules(text, self.engine)
            self.assertIn(REMEMBER, categories, text)

    def test_english_matching_is_ascii_case_insensitive(self):
        cases = [
            ("I PREFER SIMPLE NAMES", PREFERENCE),
            ("i dislike noisy logs", PREFERENCE),
            ("FROM NOW ON USE THE DEMO FORMAT", PREFERENCE),
            ("THIS REPO NEEDS A CLEANUP", PROJECT),
            ("PLEASE REMEMBER THE DEMO PATH", REMEMBER),
        ]
        for text, expected in cases:
            categories = _ops.match_rules(text, self.engine)
            self.assertIn(expected, categories, text)

    def test_unrelated_english_does_not_match(self):
        negatives = [
            "The build finished in two seconds.",
            "What does this function return?",
            "Today the weather is nice.",
            "Open the file and run the demo script.",
        ]
        for text in negatives:
            categories = _ops.match_rules(text, self.engine)
            self.assertEqual(categories, [], text)


if __name__ == "__main__":
    unittest.main()
