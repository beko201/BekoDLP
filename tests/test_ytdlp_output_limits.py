"""Reject excessive subprocess output before decoding or retaining metadata."""
import json
import sys
import time
import unittest
from unittest.mock import patch

import ytdlp_output
from ytdlp_output import BoundedLines, JsonBudget, OutputLimitError
from ytdlp_process import YtDlpProcess, YtDlpProcessError


class OutputLimitTests(unittest.TestCase):
    def test_chunk_boundaries_preserve_unicode_crlf_and_final_unterminated_line(self):
        wire = 'Türkçe🔒\r\nsecond\rthird\nfourth'.encode('utf-8')
        for chunk_size in (1, 2, 7, 65536):
            with self.subTest(chunk_size=chunk_size):
                output = BoundedLines()
                lines = []
                for offset in range(0, len(wire), chunk_size):
                    lines.extend(output.feed(wire[offset:offset + chunk_size]))
                lines.extend(output.finish())
                self.assertEqual(lines, ['Türkçe🔒', 'second', 'third', 'fourth'])

    def test_line_limit_applies_to_bytes_and_accepts_exact_boundary(self):
        with patch.object(ytdlp_output, 'MAX_OUTPUT_LINE_BYTES', 8):
            output = BoundedLines()
            self.assertEqual(list(output.feed('şşşş\n'.encode('utf-8'))), ['şşşş'])
            with self.assertRaises(OutputLimitError):
                list(output.feed(b'x' * 9))
            self.assertLessEqual(len(output.pending), 8)

    def test_real_child_exceeding_line_limit_without_newline_is_killed(self):
        processes = []
        adapter = YtDlpProcess(on_start=processes.append)
        script = 'import sys,time; sys.stdout.write("x"*513); sys.stdout.flush(); time.sleep(60)'
        started = time.monotonic()
        with patch.object(ytdlp_output, 'MAX_OUTPUT_LINE_BYTES', 512):
            with self.assertRaisesRegex(YtDlpProcessError, 'per-line size limit'):
                adapter._run([sys.executable, '-u', '-c', script], lambda line: True)
        self.assertLess(time.monotonic() - started, 5)
        self.assertIsNotNone(processes[0].poll())
        self.assertTrue(processes[0].stdout.closed)

    def test_real_process_preserves_unterminated_unicode_output(self):
        lines = []
        script = "import sys; sys.stdout.buffer.write('Türkçe🔒'.encode('utf-8'))"
        YtDlpProcess()._run([sys.executable, '-c', script], lambda line: lines.append(line) or True)
        self.assertEqual(lines, ['Türkçe🔒'])

    def test_document_size_is_checked_before_json_decode(self):
        with patch.object(ytdlp_output, 'MAX_JSON_DOCUMENT_BYTES', 8), \
                patch.object(ytdlp_output.json, 'loads') as decode:
            with self.assertRaises(OutputLimitError):
                JsonBudget().read('{"x":"şş"}')
            decode.assert_not_called()

    def test_total_bytes_and_document_count_are_checked_before_decode(self):
        budget = JsonBudget(max_documents=2, max_bytes=4)
        self.assertEqual(budget.read('{}'), {})
        self.assertEqual(budget.read('{}'), {})
        with patch.object(ytdlp_output.json, 'loads') as decode:
            with self.assertRaisesRegex(OutputLimitError, 'size limit'):
                budget.read('{}')
            decode.assert_not_called()
        budget = JsonBudget(max_documents=1)
        budget.read('{}')
        with self.assertRaisesRegex(OutputLimitError, 'document count'):
            budget.read('{}')

    def test_invalid_deeply_nested_and_nonobject_json_have_controlled_errors(self):
        for payload in ('{bad', '{"x":' + '[' * 2000 + '0' + ']' * 2000 + '}', '[]', 'null'):
            with self.subTest(payload=payload[:12]):
                with self.assertRaises(OutputLimitError):
                    JsonBudget().read(payload)

    def test_brackets_and_escaped_quotes_inside_strings_do_not_count_as_nesting(self):
        value = {'title': '[{' * 1000 + '\\"' + '}]' * 1000}
        self.assertEqual(JsonBudget().read(json.dumps(value)), value)

    def test_metadata_rejects_second_document_immediately(self):
        seen = []
        def run(args, consume):
            for index in range(1000):
                seen.append(index)
                consume(json.dumps({'id': str(index)}))
        adapter = YtDlpProcess(executable='unused')
        with patch.object(adapter, '_run', side_effect=run):
            with self.assertRaisesRegex(YtDlpProcessError, 'document count'):
                adapter.extract_info('https://example.invalid/')
        self.assertEqual(seen, [0, 1])

    def test_streaming_playlist_limit_preserves_only_already_read_entries(self):
        adapter = YtDlpProcess(executable='unused')
        batches = []
        def run(args, consume):
            for i in range(10):
                consume(json.dumps({'playlist_id': 'audit', 'id': str(i)}))
        with patch('ytdlp_process.MAX_PLAYLIST_DOCUMENTS', 3), patch.object(adapter, '_run', side_effect=run):
            with self.assertRaisesRegex(YtDlpProcessError, 'document count'):
                adapter.flat_extract('https://example.invalid/', on_entries=lambda batch: batches.extend(batch), batch_size=2)
        self.assertEqual([entry['id'] for entry in batches], ['0', '1', '2'])

    def test_playlist_total_size_limit_is_distinct_from_document_size(self):
        adapter = YtDlpProcess(executable='unused')
        payload = json.dumps({'playlist_id': 'audit', 'id': '0'})
        def run(args, consume):
            consume(payload)
            consume(payload)
        with patch('ytdlp_process.MAX_COLLECTED_PLAYLIST_JSON_BYTES', len(payload.encode('utf-8'))), \
                patch.object(adapter, '_run', side_effect=run):
            with self.assertRaisesRegex(YtDlpProcessError, 'size limit'):
                adapter.flat_extract('https://example.invalid/')

    def test_download_result_and_subtitles_reject_repeated_documents(self):
        adapter = YtDlpProcess(executable='unused')
        for operation, tag in ((lambda: adapter.download(['https://example.invalid/']), 'BEKO_RESULT:'),
                               (lambda: adapter.download_subtitles({'id': 'audit'}, 'unused', ['en']), 'BEKO_SUBTITLES:')):
            with self.subTest(tag=tag):
                def run(args, consume):
                    consume(tag + '{}')
                    consume(tag + '{}')
                with patch.object(adapter, '_run', side_effect=run):
                    with self.assertRaisesRegex(YtDlpProcessError, 'document count'):
                        operation()

    def test_many_small_progress_lines_remain_valid(self):
        adapter = YtDlpProcess(executable='unused')
        updates = []
        adapter.options['progress_hooks'] = [updates.append]
        def run(args, consume):
            for i in range(2000):
                consume('BEKO_PROGRESS:' + json.dumps({'downloaded_bytes': i}) + '\t"video"')
            consume('BEKO_RESULT:{"id":"done"}')
        with patch.object(adapter, '_run', side_effect=run):
            self.assertEqual(adapter.download(['https://example.invalid/']), 0)
        self.assertEqual(len(updates), 2000)
