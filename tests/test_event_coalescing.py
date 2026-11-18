from concurrent.futures import ThreadPoolExecutor
from queue import Empty, Queue
import unittest
from unittest.mock import Mock, patch

from task_manager import TaskManager, TaskOptions


class EventCoalescingTests(unittest.TestCase):
    def setUp(self):
        self.manager = TaskManager(engine_factory=lambda manager: Mock())
        self.tid = self.manager.add_urls(
            ['https://example.com/video'], TaskOptions(folder='.'))[0]
        self.drain()

    def drain(self):
        events = []
        while True:
            try:
                events.append(self.manager.events.get_nowait())
                self.manager.events.task_done()
            except Empty:
                return events

    def test_stalled_consumer_keeps_only_latest_progress_and_completion(self):
        for step in range(5000):
            self.manager.report_stage_progress(self.tid, 'downloading', step / 5000)
        self.assertEqual(self.manager.events.qsize(), 2)
        self.manager.update_task(self.tid, progress=100, status='Completed', output_path='done.mp4')
        events = self.drain()
        updates = [event for event in events if event.kind == 'task_updated']
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].data['task']['progress'], 100)
        self.assertEqual(updates[0].data['task']['output_path'], 'done.mp4')

    def test_notices_errors_and_removal_are_preserved_in_order(self):
        for step in range(100):
            self.manager.report_stage_progress(self.tid, 'processing', step / 100)
            self.manager.emit('notice', message=str(step))
        self.manager.emit('error', title='Error', message='failed')
        self.manager.emit('task_removed', self.tid)
        events = self.drain()
        notices = [event.data['message'] for event in events if event.kind == 'notice']
        self.assertEqual(notices, [str(step) for step in range(100)])
        self.assertEqual([event.kind for event in events[-2:]], ['error', 'task_removed'])
        self.assertEqual(len(events), 104)

    def test_consumed_key_can_be_enqueued_again_and_queue_accounting_is_valid(self):
        self.manager.emit('queue_changed', value=1)
        self.manager.emit('queue_changed', value=2)
        self.assertEqual(self.manager.events.get_nowait().data['value'], 2)
        self.manager.events.task_done()
        self.assertEqual(self.manager.events.unfinished_tasks, 0)
        self.manager.emit('queue_changed', value=3)
        self.assertEqual(self.manager.events.get_nowait().data['value'], 3)
        self.manager.events.task_done()
        self.assertEqual(self.manager.events.unfinished_tasks, 0)

    def test_compact_task_events_do_not_copy_task_payloads(self):
        self.manager.use_compact_task_events()
        with patch.object(self.manager, '_snapshot', side_effect=AssertionError('no task copy')):
            other = self.manager.add_urls(['https://example.com/compact'], TaskOptions(folder='.'))[0]
            self.manager.update_task(self.tid, title='Latest', progress=20)
            self.manager.cancel_task(other)
        events = [event for event in self.drain() if event.kind in ('task_added', 'task_updated')]
        self.assertTrue(events)
        self.assertTrue(all(not event.data for event in events))
        tasks = self.manager.get_tasks([self.tid, other, 'missing'])
        self.assertEqual([task['id'] for task in tasks], [self.tid, other])
        self.assertEqual(tasks[0]['title'], 'Latest')
        tasks[0]['title'] = 'External mutation'
        self.assertEqual(self.manager.get_task(self.tid)['title'], 'Latest')

    def test_parallel_tasks_retain_independent_latest_snapshots(self):
        tids = self.manager.add_urls(
            [f'https://example.com/{index}' for index in range(4)], TaskOptions(folder='.'))
        self.drain()

        def produce(tid):
            for step in range(1000):
                self.manager.report_stage_progress(tid, 'processing', step / 999)

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(produce, tids))
        self.assertEqual(self.manager.events.qsize(), len(tids) + 1)
        updates = {event.tid: event.data['task'] for event in self.drain()
                   if event.kind == 'task_updated'}
        self.assertEqual(set(updates), set(tids))
        self.assertTrue(all(task['progress'] == 90 for task in updates.values()))

    def test_concurrent_consumer_receives_final_update_without_losing_signals(self):
        def consume():
            values = []
            while True:
                event = self.manager.events.get(timeout=2)
                self.manager.events.task_done()
                if event.kind == 'notice':
                    return values
                if event.kind == 'task_updated':
                    values.append(event.data['task']['progress'])

        with ThreadPoolExecutor(max_workers=1) as pool:
            consumer = pool.submit(consume)
            for step in range(1000):
                self.manager.report_stage_progress(self.tid, 'processing', step / 999)
            self.manager.emit('notice', message='producer finished')
            values = consumer.result(timeout=3)
        self.assertEqual(values, sorted(values))
        self.assertEqual(values[-1], 90)
        self.assertTrue(self.manager.events.empty())
        self.assertEqual(self.manager.events.unfinished_tasks, 0)


if __name__ == '__main__':
    unittest.main()
