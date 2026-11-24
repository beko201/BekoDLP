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
import base64

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkProxy
from PySide6.QtCore import QEvent

from task_manager import TaskManager, TaskOptions
from ui.main_window import MainWindow
from qt_support import finish_window_close
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
        self.proxy_auth = []
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
                    if route.get('auth'):
                        auth = self.headers.get('Proxy-Authorization')
                        owner.proxy_auth.append(auth)
                        if auth != route['auth']:
                            self.send_response(407)
                            self.send_header('Proxy-Authenticate', 'Basic realm="thumbnail-test"')
                            self.send_header('Content-Length', '0')
                            self.end_headers()
                            return
                    if route.get('redirect'):
                        self.send_response(302)
                        self.send_header('Location', route['redirect'])
                        self.send_header('Content-Length', '0')
                        self.end_headers()
                        return
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

    def proxy_options(self, credentials='', scheme='http'):
        return dict(proxy_mode='custom', proxy_url=f'{scheme}://{credentials}127.0.0.1:{self.server.server_port}')

    def test_custom_proxy_fetches_remote_host_without_direct_dns_or_connection(self):
        url = 'http://thumbnail.example.invalid/image'
        self.routes[url] = dict(body=png('green'))
        self.loader.register('T1', url, self.proxy_options())
        self.loader.request('T1')
        self.wait_until(lambda: len(self.images) == 1)
        self.assertEqual(self.requests, [url])
        self.assertEqual(self.images[0][1].pixelColor(0, 0), QColor('green'))

    def test_authenticated_proxy_credentials_are_separate_from_image_url(self):
        url = 'http://thumbnail.example.invalid/private'
        auth = 'Basic ' + base64.b64encode(b'audit-user:private-pass').decode('ascii')
        self.routes[url] = dict(body=png(), auth=auth)
        self.loader.register('T1', url, self.proxy_options('audit-user:private-pass@'))
        self.loader.request('T1')
        self.wait_until(lambda: bool(self.images) or 'T1' in self.loader._failed)
        self.assertEqual(len(self.images), 1)
        self.assertIn(auth, self.proxy_auth)
        self.assertTrue(all('private-pass' not in path for path in self.requests))

    def test_redirects_keep_custom_proxy_and_block_loopback_destinations(self):
        first = 'http://thumbnail.example.invalid/redirect'
        final = 'http://second.example.invalid/image'
        self.routes[first] = dict(body=b'', redirect=final)
        self.routes[final] = dict(body=png())
        self.loader.register('T1', first, self.proxy_options())
        self.loader.request('T1')
        self.wait_until(lambda: bool(self.images))
        self.assertEqual(self.requests, [first, final])
        blocked = 'http://thumbnail.example.invalid/blocked'
        local = self.route('/must-not-connect')
        self.routes[blocked] = dict(body=b'', redirect=local)
        self.loader.register('T2', blocked, self.proxy_options())
        self.loader.request('T2')
        self.wait_until(lambda: 'T2' in self.loader._failed)
        self.assertNotIn('/must-not-connect', self.requests)
        self.assertEqual(len(self.images), 1)

    def test_unsupported_proxy_invalid_options_and_loopback_targets_never_connect(self):
        local = self.route('/must-not-connect')
        options = [self.proxy_options(scheme=scheme) for scheme in ('https', 'socks4', 'socks4a')]
        options += [dict(proxy_mode='custom', proxy_url='invalid'), self.proxy_options()]
        with patch.object(self.loader.network, 'get', wraps=self.loader.network.get) as default_get:
            for i, option in enumerate(options):
                tid = f'T{i}'
                self.loader.register(tid, local, option)
                self.loader.request(tid)
                self.assertIn(tid, self.loader._failed)
            default_get.assert_not_called()
        self.assertEqual(self.requests, [])
        self.assertEqual(self.images, [])

    def test_proxy_failure_does_not_retry_on_default_connection(self):
        url = 'http://thumbnail.example.invalid/fail'
        # The live HTTP server rejects this SOCKS handshake; no default GET occurs.
        with patch.object(thumbnails, 'TIMEOUT_MS', 60), \
                patch.object(self.loader.network, 'get', wraps=self.loader.network.get) as default_get:
            self.loader.register('T1', url, self.proxy_options(scheme='socks5h'))
            self.loader.request('T1')
            self.wait_until(lambda: 'T1' in self.loader._failed)
            self.loader.request('T1')
            self.app.processEvents()
            default_get.assert_not_called()
        self.assertEqual(self.images, [])

    def test_parallel_previews_keep_proxy_credentials_isolated(self):
        gate = self.gate()
        for i, color in enumerate(('red', 'green')):
            url = f'http://thumbnail.example.invalid/private-{i}'
            credentials = f'audit-user:private-{i}'
            auth = 'Basic ' + base64.b64encode(credentials.encode('ascii')).decode('ascii')
            self.routes[url] = dict(body=png(color), auth=auth, gate=gate)
            self.loader.register(f'T{i}', url, self.proxy_options(credentials + '@'))
            self.loader.request(f'T{i}')
        self.wait_until(lambda: len([auth for auth in self.proxy_auth if auth]) >= 2)
        gate.set()
        self.wait_until(lambda: len(self.images) == 2)
        self.assertEqual({tid: image.pixelColor(0, 0).name() for tid, image in self.images},
                         {'T0': '#ff0000', 'T1': '#008000'})

    def test_numeric_loopback_aliases_are_blocked_before_network_access(self):
        for i, host in enumerate(('localhost', 'foo.localhost', '127.1', '2130706433',
                                  '0177.0.0.1', '0x7f000001', '[::1]', '[::ffff:127.0.0.1]')):
            tid = f'T{i}'
            self.loader.register(tid, f'http://{host}/image', self.proxy_options())
            self.loader.request(tid)
            self.assertIn(tid, self.loader._failed)
        self.assertEqual(self.requests, [])

    def test_proxy_capabilities_and_direct_mode_are_explicit_and_isolated(self):
        for scheme, kind, dns in (('http', QNetworkProxy.ProxyType.HttpProxy, None),
                                  ('socks5', QNetworkProxy.ProxyType.Socks5Proxy, False),
                                  ('socks5h', QNetworkProxy.ProxyType.Socks5Proxy, True)):
            network, custom = self.loader._network_for(self.proxy_options('user:p%40ss@', scheme))
            try:
                self.assertTrue(custom)
                self.assertEqual(network.proxy().type(), kind)
                self.assertEqual(network.proxy().password(), 'p@ss')
                if dns is not None:
                    self.assertEqual(bool(network.proxy().capabilities() & QNetworkProxy.Capability.HostNameLookupCapability), dns)
            finally:
                network.deleteLater()
        network, custom = self.loader._network_for(dict(proxy_mode='direct'))
        self.assertFalse(custom)
        self.assertEqual(network.proxy().type(), QNetworkProxy.ProxyType.NoProxy)
        network.deleteLater()
        self.assertIs(self.loader._network_for({})[0], self.loader.network)

    def test_network_change_invalidates_cached_image_and_managers_are_released(self):
        url = self.route('/cached')
        self.loader.register('T1', url)
        self.loader.request('T1')
        self.wait_until(lambda: bool(self.images))
        self.loader.register('T1', url, dict(proxy_mode='direct'))
        self.assertNotIn('T1', self.loader._cache)
        self.loader.request('T1')
        self.wait_until(lambda: len(self.images) == 2)
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertEqual(len(self.loader.findChildren(QNetworkAccessManager)), 1)

    def test_window_passes_frozen_task_network_settings_to_preview(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = TaskManager(engine_factory=lambda manager: Mock())
            with patch.object(manager, 'set_worker_count'):
                window = MainWindow(manager, settings_path=Path(folder, 'settings.json'))
            try:
                options = self.proxy_options('audit-user:private-pass@')
                tid = manager.add_urls(['https://example.invalid/video'], TaskOptions(folder=folder, **options))[0]
                with patch.object(window.thumbnails, 'register') as register:
                    window._register_thumbnail(tid, 'http://thumbnail.example.invalid/image')
                self.assertEqual(register.call_args.args[2]['proxy_url'], options['proxy_url'])
            finally:
                finish_window_close(window)
                window.controller.poll()
                self.app.processEvents()

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
                finish_window_close(window)
                window.controller.poll()
                self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
