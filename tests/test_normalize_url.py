import unittest
from unittest.mock import Mock

from task_manager import TaskManager, TaskOptions
from url_utils import normalize_url


class NormalizeUrlTests(unittest.TestCase):
    VIDEO = 'aB_cD-12345'
    CANONICAL = 'https://www.youtube.com/watch?v=' + VIDEO

    def variants(self):
        return [
            self.CANONICAL,
            self.CANONICAL + '&si=tracking&feature=shared',
            'https://www.youtube.com/watch?list=PLdemo&v=' + self.VIDEO + '&index=3',
            'http://m.youtube.com/watch?v=' + self.VIDEO + '#t=30',
            'https://music.youtube.com/watch?v=' + self.VIDEO,
            'https://youtu.be/' + self.VIDEO + '?si=tracking',
            'https://www.youtube.com/shorts/' + self.VIDEO + '?feature=share',
            'https://www.youtube.com/live/' + self.VIDEO,
            'https://www.youtube.com/embed/' + self.VIDEO,
            'https://www.youtube-nocookie.com/embed/' + self.VIDEO,
            'HTTPS://WWW.YOUTUBE.COM/watch?v=' + self.VIDEO,
            'youtu.be/' + self.VIDEO,
            '//www.youtube.com/watch?v=' + self.VIDEO,
        ]

    def test_video_variants_share_one_canonical_url(self):
        for url in self.variants():
            with self.subTest(url=url):
                self.assertEqual(normalize_url(url), self.CANONICAL)
                self.assertEqual(normalize_url(normalize_url(url)), self.CANONICAL)

    def test_other_sites_and_unrecognized_paths_are_preserved(self):
        urls = [
            'https://example.com/?next=https://youtu.be/' + self.VIDEO,
            'https://example.com/youtube.com/shorts/' + self.VIDEO,
            'https://youtube.com.example.org/watch?v=' + self.VIDEO,
            'https://notyoutu.be/' + self.VIDEO,
            'https://youtube.com@other.example/watch?v=' + self.VIDEO,
            'https://user:password@youtube.com/watch?v=' + self.VIDEO,
            'https://www.youtube.com/playlist?list=PLdemo',
            'https://www.youtube.com/@channel',
            'https://www.youtube.com/clip/clip-id',
            'https://www.youtube.com/watch',
            'https://youtu.be/',
            'https://youtu.be/a/b',
            'https://youtu.be:invalid/video',
            'https://[invalid',
            'ftp://youtu.be/' + self.VIDEO,
            'https://www.tiktok.com/@name/video/123?token=keep',
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(normalize_url(url), url)
        self.assertEqual(normalize_url('  ' + urls[-1] + '  '), urls[-1])
        self.assertEqual(normalize_url(''), '')
        self.assertIsNone(normalize_url(None))

    def test_task_manager_deduplicates_variants_but_keeps_different_videos(self):
        manager = TaskManager(engine_factory=lambda manager: Mock())
        options = TaskOptions(folder='.')
        added = manager.add_urls(self.variants(), options)
        self.assertEqual(len(added), 1)
        self.assertEqual(manager.get_task(added[0])['url'], self.CANONICAL)
        self.assertEqual(len(manager.add_urls(['https://youtu.be/other-video'], options)), 1)
        self.assertEqual(len(manager.list_tasks()), 2)


if __name__ == '__main__':
    unittest.main()
