"""采样工具只用合成 SSE 验证完成判定；本测试不连接真实模型。"""
import unittest

from sample_self_facts import parse_sse


class StreamTests(unittest.TestCase):
    def test_done_is_drained_to_eof_before_next_sample(self):
        consumed = []

        def stream():
            yield b'data: {"choices":[{"delta":{"content":"ok"}}]}\n'
            yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n'
            yield b'data: [DONE]\n'
            consumed.append('server scene cleanup finished')

        result = parse_sse(stream())
        self.assertTrue(result['complete'])
        self.assertEqual(consumed, ['server scene cleanup finished'])

    def test_interrupted_length_empty_and_error_are_not_complete(self):
        content = b'data: {"choices":[{"delta":{"content":"partial"}}]}\n'
        stop = b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n'
        done = b'data: [DONE]\n'
        for frames in ([content], [content, done], [stop, done],
                       [content, stop], [content, stop, b'data: {"error":"x"}\n', done],
                       [content, b'data: {"choices":[{"finish_reason":"length"}]}\n', done],
                       [content, stop, b'data: not-json\n', done]):
            with self.subTest(frames=frames):
                self.assertFalse(parse_sse(frames)['complete'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
