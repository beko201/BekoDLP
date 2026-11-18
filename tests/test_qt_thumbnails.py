"""Exercise actual Qt networking against a local HTTP server, never public sites."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from task_manager import TaskManager, TaskOptions
from ui.main_window import MainWindow
from ui import thumbnails
from ui.thumbnails import ThumbnailLoader


def png(color="red", width=80, height=40):
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    return bytes(data)


class ThumbnailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.routes = {}
        self.requests = []
        self.gates = []
        self.lock = threading.Lock()
        self.running = 0
        self.peak = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                with owner.lock:
                    owner.requests.append(self.path)
                    owner.running += 1
                    owner.peak = max(owner.peak, owner.running)
                route = owner.routes[self.path]
                try:
                    if route.get("gate"):
                        route["gate"].wait(3)
                    body = route["body"]
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    if not route.get("no_length"):
                        self.send_header("Content-Length", str(route.get("length", len(body))))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
                finally:
                    with owner.lock:
                        owner.running -= 1

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server_thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=.02), daemon=True)
        self.server_thread.start()
        self.loader = ThumbnailLoader()
        self.images = []
        self.states = []
        self.loader.image_ready.connect(lambda tid, image: self.images.append((tid, image)))
        self.loader.state_changed.connect(lambda tid, state: self.states.append((tid, state)))

    def tearDown(self):
        self.loader.close()
        for gate in self.gates:
            gate.set()
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=1)
        self.app.processEvents()

    def route(self, path, *, color="red", gate=None, body=None, **kwargs):
        self.routes[path] = dict(body=png(color) if body is None else body, gate=gate, **kwargs)
        return f"http://127.0.0.1:{self.server.server_port}{path}"

    def gate(self):
        gate = threading.Event()
        self.gates.append(gate)
        return gate

    def wait_until(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.005)
        self.fail("Thumbnail operation timed out")

    def test_register_is_lazy_and_cached_images_are_reused_on_gui_thread(self):
        url = self.route("/large", body=png(width=1000, height=500))
        self.loader.register("T1", url)
        self.app.processEvents()
        self.assertEqual(self.requests, [])
        owner = threading.get_ident()
        threads = []
        self.loader.image_ready.connect(lambda *_: threads.append(threading.get_ident()))
        self.loader.request("T1")
        self.loader.request("T1")
        self.wait_until(lambda: len(self.images) == 1)
        image = self.images[0][1]
        self.assertEqual((image.width(), image.height()), (480, 240))
        self.assertEqual(set(threads), {owner})
        self.loader.request("T1")
        self.assertEqual(len(self.images), 2)
        self.assertEqual(self.requests, ["/large"])

    def test_concurrency_and_pending_queue_remain_bounded_during_fast_selection(self):
        gate = self.gate()
        for i in range(30):
            tid = f"T{i}"
            self.loader.register(tid, self.route(f"/{i}", gate=gate))
            self.loader.request(tid)
            self.assertLessEqual(len(self.loader._active), thumbnails.MAX_ACTIVE)
            self.assertLessEqual(len(self.loader._pending), thumbnails.MAX_PENDING)
        self.wait_until(lambda: len(self.requests) == 3)
        self.assertEqual(self.peak, 3)
        gate.set()
        self.wait_until(lambda: not self.loader._active and not self.loader._pending)
        self.assertLessEqual(self.peak, 3)
        self.assertLessEqual(len(self.requests), 3 + thumbnails.MAX_PENDING)
        self.assertIn("T29", [tid for tid, _ in self.images])

    def test_removed_or_replaced_tasks_cannot_restore_old_images(self):
        gate = self.gate()
        self.loader.register("T1", self.route("/old", gate=gate))
        self.loader.request("T1")
        self.wait_until(lambda: "/old" in self.requests)
        self.loader.remove("T1")
        gate.set()
        self.wait_until(lambda: not self.loader._active)
        self.assertEqual(self.images, [])
        self.assertNotIn("T1", self.loader._cache)
        self.assertNotIn("T1", self.loader._entries)
        self.loader.register("T1", self.route("/new", color="green"))
        self.loader.request("T1")
        self.wait_until(lambda: len(self.images) == 1)
        self.assertEqual(self.images[0][1].pixelColor(0, 0), QColor("green"))

    def test_new_url_aborts_an_old_generation_and_only_new_result_is_published(self):
        gate = self.gate()
        self.loader.register("T1", self.route("/old", gate=gate))
        self.loader.request("T1")
        self.wait_until(lambda: "/old" in self.requests)
        self.loader.register("T1", self.route("/new", color="green"))
        self.loader.request("T1")
        self.wait_until(lambda: len(self.images) == 1)
        gate.set()
        self.app.processEvents()
        self.assertEqual(len(self.images), 1)
        self.assertEqual(self.loader._cache["T1"].pixelColor(0, 0), QColor("green"))

    def test_pending_removal_and_shutdown_abort_requests_and_clear_memory(self):
        gate = self.gate()
        for i in range(4):
            self.loader.register(f"T{i}", self.route(f"/{i}", gate=gate))
            self.loader.request(f"T{i}")
        self.loader.remove("T3")
        self.wait_until(lambda: len(self.requests) == 3)
        self.loader.close()
        gate.set()
        self.wait_until(lambda: not self.loader._active)
        self.loader.request("T0")
        self.loader.register("T4", self.route("/4"))
        self.assertNotIn("/3", self.requests)
        self.assertNotIn("/4", self.requests)
        self.assertEqual(self.images, [])
        self.assertFalse(self.loader._entries or self.loader._pending or self.loader._cache)
        self.assertEqual(self.loader._cache_bytes, 0)

    def test_header_and_stream_size_limits_reject_oversized_responses(self):
        with patch.object(thumbnails, "MAX_DOWNLOAD_BYTES", 128):
            for tid, url in (("T1", self.route("/header", length=10000)),
                             ("T2", self.route("/stream", body=b"x" * 512, no_length=True))):
                self.loader.register(tid, url)
                self.loader.request(tid)
            self.wait_until(lambda: {"T1", "T2"}.issubset(self.loader._failed))
        self.assertEqual(self.images, [])
        self.assertIn(("T1", "unavailable"), self.states)
        self.assertIn(("T2", "unavailable"), self.states)

    def test_pixel_limit_invalid_images_and_timeout_fail_without_repeated_downloads(self):
        self.loader.register("T1", self.route("/bad", body=b"not an image"))
        self.loader.request("T1")
        self.wait_until(lambda: "T1" in self.loader._failed)
        self.loader.request("T1")
        self.assertEqual(self.requests.count("/bad"), 1)
        with patch.object(thumbnails, "MAX_IMAGE_PIXELS", 100):
            self.loader.register("T2", self.route("/pixels"))
            self.loader.request("T2")
            self.wait_until(lambda: "T2" in self.loader._failed)
        gate = self.gate()
        with patch.object(thumbnails, "TIMEOUT_MS", 60):
            self.loader.register("T3", self.route("/slow", gate=gate))
            self.loader.request("T3")
            self.wait_until(lambda: "T3" in self.loader._failed, timeout=1)
        gate.set()
        self.assertEqual(self.images, [])

    def test_lru_and_byte_budget_evict_images_but_allow_later_reload(self):
        with patch.object(thumbnails, "MAX_CACHE_ENTRIES", 2):
            for i in range(3):
                self.loader.register(f"T{i}", self.route(f"/{i}"))
                self.loader.request(f"T{i}")
                self.wait_until(lambda: len(self.images) == i + 1)
            self.assertEqual(list(self.loader._cache), ["T1", "T2"])
            self.loader.request("T1")
            self.assertEqual(list(self.loader._cache), ["T2", "T1"])
            self.loader.request("T0")
            self.wait_until(lambda: self.requests.count("/0") == 2 and "T0" in self.loader._cache)
            self.assertEqual(list(self.loader._cache), ["T1", "T0"])
        with patch.object(thumbnails, "MAX_CACHE_BYTES", 1):
            self.loader.register("T3", self.route("/3"))
            self.loader.request("T3")
            self.wait_until(lambda: not self.loader._active)
        self.assertFalse(self.loader._cache)
        self.assertEqual(self.loader._cache_bytes, 0)

    def test_real_window_keeps_current_preview_when_old_selection_finishes_late(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = TaskManager(engine_factory=lambda manager: Mock())
            with patch.object(manager, "set_worker_count"):
                window = MainWindow(manager, settings_path=Path(folder, "settings.json"))
            try:
                window.show()
                ids = manager.add_urls(["https://example.com/a", "https://example.com/b"],
                                       TaskOptions(folder=folder))
                manager.update_task(ids[0], title="<b>First title</b>")
                manager.update_task(ids[1], title="Second title")
                gate = self.gate()
                manager.emit("thumbnail", ids[0], url=self.route("/first", gate=gate))
                manager.emit("thumbnail", ids[1], url=self.route("/second", color="green"))
                window.controller.poll()
                self.assertEqual(self.requests, [])
                window.task_table.view.selectRow(0)
                self.wait_until(lambda: "/first" in self.requests)
                self.assertEqual(window.preview_panel.title_label.text(), "<b>First title</b>")
                self.assertEqual(window.preview_panel.title_label.textFormat(), Qt.TextFormat.PlainText)
                window.task_table.view.selectRow(1)
                self.wait_until(lambda: window.preview_panel._image is not None)
                gate.set()
                self.wait_until(lambda: not window.thumbnails._active)
                self.assertEqual(window.preview_panel._tid, ids[1])
                self.assertEqual(window.preview_panel._image.pixelColor(0, 0), QColor("green"))
                pixmap = window.preview_panel.image_label.pixmap()
                self.assertLessEqual(pixmap.width(), window.preview_panel.image_label.width())
                self.assertLessEqual(pixmap.height(), window.preview_panel.image_label.height())
                manager.cancel_task(ids[1])
                manager.clear_list()
                window.controller.poll()
                self.assertNotIn(ids[1], window.thumbnails._cache)
                window.task_table.view.clearSelection()
                self.assertIsNone(window.preview_panel._image)
                self.assertEqual(window.preview_panel.title_label.text(), "")
            finally:
                window.close()
                window.controller.poll()
                self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
