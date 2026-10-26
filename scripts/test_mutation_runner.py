"""变异执行器的隔离回归；只用临时文件和假执行器，不调用模型。"""
import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mutation_check as mutation


class IsolationTests(unittest.TestCase):
    def test_crash_is_not_assertion_evidence(self):
        self.assertIsNone(mutation.failure_evidence('backend',
            'Fatal Python error: _PyEval_EvalFrameDefault: Executing a cache.'))
        self.assertIsNone(mutation.failure_evidence('backend',
            'Traceback (most recent call last):\nTypeError: null card'))
        self.assertIsNone(mutation.failure_evidence('frontend',
            'FAIL: node unavailable\n[harness] 用例没跑完'))
        for kind, output in (
            ('backend', 'FAIL: test_history\nAssertionError: wrong history\nFAILED (failures=1)'),
            ('frontend', 'FAIL: alignment\nAssertionError [ERR_ASSERTION]: alignment'),
            ('selfcheck', '  ✗ 示例 括号不配对\n合计错误 1 条'),
            ('sources', '  × 标签不符\n1 条对不上。'),
        ):
            with self.subTest(kind=kind):
                self.assertIsNotNone(mutation.failure_evidence(kind, output))

    def test_valid_mutation_followed_by_runtime_crash_is_unverified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'personas').mkdir()
            for name in ('yuki.json', 'yuki_school.json'):
                (root / 'personas' / name).write_text('{"system_prompt":"x","examples":[]}', encoding='utf-8')
            outputs = iter([(True, 'baseline'), (False, 'Fatal Python error: Executing a cache.')])
            output = io.StringIO()
            with patch.object(mutation, 'ROOT', root), \
                 patch.object(mutation, 'PERSONAS', root / 'personas'), \
                 patch.object(mutation, 'MUTATIONS', [('BAD', 'crash', 'yuki.json', lambda card: dict(card, system_prompt='changed'), 'backend')]), \
                 patch.dict(mutation.RUNNERS, {'backend': (lambda: next(outputs), 'fake')}), \
                 contextlib.redirect_stdout(output):
                self.assertNotEqual(mutation.run_mutations(), 0)
            self.assertIn('未确证', output.getvalue())
            # 区分成功行与「不能算已拦住」的诊断说明。
            self.assertNotIn('✓ BAD 已拦住', output.getvalue())

    def test_bracket_mutation_preserves_card_and_adds_broken_example(self):
        card = json.loads((mutation.PERSONAS / 'yuki.json').read_text(encoding='utf-8'))
        changed = mutation.m_broken_bracket_example(copy.deepcopy(card))
        self.assertIsInstance(changed, dict)
        self.assertEqual(changed['system_prompt'], card['system_prompt'])
        self.assertEqual(len(changed['examples']), len(card['examples']) + 1)
        self.assertNotEqual(changed['examples'][-1]['assistant'].count('（'),
                            changed['examples'][-1]['assistant'].count('）'))

    def test_invalid_mutation_is_broken_not_counted_as_killed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'personas').mkdir()
            for name in ('yuki.json', 'yuki_school.json'):
                (root / 'personas' / name).write_text('{"system_prompt":"x","examples":[]}', encoding='utf-8')
            output = io.StringIO()
            with patch.object(mutation, 'ROOT', root), \
                 patch.object(mutation, 'PERSONAS', root / 'personas'), \
                 patch.object(mutation, 'MUTATIONS', [('BAD', 'returns None', 'yuki.json', lambda card: None, 'backend')]), \
                 patch.dict(mutation.RUNNERS, {'backend': (lambda: (True, 'baseline'), 'fake')}), \
                 contextlib.redirect_stdout(output):
                self.assertEqual(mutation.run_mutations(), 1)
            self.assertIn('变异失效', output.getvalue())
            self.assertNotIn('已拦住', output.getvalue())
            self.assertEqual(json.loads((root / 'personas/yuki.json').read_text())['system_prompt'], 'x')

    def test_default_run_never_mutates_source_even_on_interrupt(self):
        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / 'source'
                (root / 'personas').mkdir(parents=True)
                card = root / 'personas/yuki.json'
                card.write_text('original', encoding='utf-8')
                for name in ('data/memory/private.json', '.env', 'out/private-chat.md'):
                    file = root / name
                    file.parent.mkdir(parents=True, exist_ok=True)
                    file.write_text('private', encoding='utf-8')
                snapshot = []

                def fake_suite():
                    snapshot.append(mutation.ROOT)
                    self.assertNotEqual(mutation.ROOT, root)
                    copied = mutation.PERSONAS / 'yuki.json'
                    self.assertEqual(copied.read_text(encoding='utf-8'), 'original')
                    copied.write_text('mutated', encoding='utf-8')
                    self.assertEqual(card.read_text(encoding='utf-8'), 'original')
                    for name in ('data/memory/private.json', '.env', 'out/private-chat.md'):
                        self.assertFalse((mutation.ROOT / name).exists())
                    if interrupted:
                        raise KeyboardInterrupt
                    return 7

                with patch.object(mutation, 'ROOT', root), \
                     patch.object(mutation, 'PERSONAS', root / 'personas'), \
                     patch.object(mutation, 'run_mutations', fake_suite):
                    if interrupted:
                        with self.assertRaises(KeyboardInterrupt):
                            mutation.main()
                    else:
                        self.assertEqual(mutation.main(), 7)
                    self.assertEqual(mutation.ROOT, root)
                    self.assertEqual(mutation.PERSONAS, root / 'personas')
                self.assertEqual(card.read_text(encoding='utf-8'), 'original')
                self.assertFalse(snapshot[0].exists(), '正常退出与中断均清理副本')

    def test_snapshot_uses_working_files_and_corpus_without_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, target = Path(tmp) / 'source', Path(tmp) / 'copy'
            for name in ('server.py', 'scripts/new_test.py', 'web/index.html',
                         'docs/VALIDATION.md', 'data/seed_nagato_lines.md',
                         'data/index/docs.jsonl', 'data/parsed/episode.dialogue.jsonl',
                         'data/_deprecated/candidates_heuristic/lines.jsonl'):
                file = root / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text('working content', encoding='utf-8')
            mutation.copy_snapshot(root, target)
            for file in root.rglob('*'):
                if file.is_file():
                    copy = target / file.relative_to(root)
                    self.assertEqual(copy.read_bytes(), file.read_bytes())
                    copy.write_text('changed', encoding='utf-8')
                    self.assertEqual(file.read_text(encoding='utf-8'), 'working content')

    def test_concurrent_edit_during_copy_fails_without_overwriting_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, target = Path(tmp) / 'source', Path(tmp) / 'copy'
            root.mkdir()
            file = root / 'server.py'
            file.write_text('before', encoding='utf-8')
            real_copy = mutation.shutil.copy2

            def changing_copy(src, dst):
                result = real_copy(src, dst)
                Path(src).write_text('concurrent user edit', encoding='utf-8')
                return result

            with patch.object(mutation.shutil, 'copy2', changing_copy):
                with self.assertRaisesRegex(RuntimeError, '源文件发生变化'):
                    mutation.copy_snapshot(root, target)
            self.assertEqual(file.read_text(encoding='utf-8'), 'concurrent user edit')


if __name__ == '__main__':
    unittest.main(verbosity=2)
