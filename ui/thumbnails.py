"""Bounded asynchronous thumbnail downloads and an LRU of scaled QImages."""
from collections import OrderedDict
from app_info import APP_NAME, APP_VERSION
from dataclasses import dataclass, field

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QObject, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QImageReader
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from utils import log_warning


MAX_ACTIVE = 3
MAX_PENDING = 16
MAX_DOWNLOAD_BYTES = 4 * 1024 * 1024
MAX_IMAGE_PIXELS = 8_000_000
MAX_CACHE_ENTRIES = 32
MAX_CACHE_BYTES = 8 * 1024 * 1024
TIMEOUT_MS = 8000
PREVIEW_SIZE = QSize(480, 270)


@dataclass
class Download:
    tid: str
    generation: int
    reply: QNetworkReply
    timer: QTimer
    data: bytearray = field(default_factory=bytearray)
    failure: str = ""


class ThumbnailLoader(QObject):
    image_ready = Signal(str, object)
    state_changed = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.network = QNetworkAccessManager(self)
        self._entries = {}
        self._generation = 0
        self._pending = OrderedDict()
        self._active = {}
        self._failed = set()
        self._cache = OrderedDict()
        self._cache_bytes = 0
        self._closed = False

    def register(self, tid, url):
        if self._closed:
            return
        current = self._entries.get(tid)
        if current and current[0] == url and tid not in self._failed:
            return
        self.remove(tid)
        self._generation += 1
        self._entries[tid] = (url or "", self._generation)

    def request(self, tid):
        if self._closed:
            return
        if tid in self._cache:
            self._cache.move_to_end(tid)
            self.image_ready.emit(tid, self._cache[tid])
            return
        entry = self._entries.get(tid)
        if entry is None:
            self.state_changed.emit(tid, "waiting")
            return
        url, generation = entry
        if tid in self._failed:
            self.state_changed.emit(tid, "unavailable")
            return
        if QUrl(url).scheme().lower() not in ("http", "https"):
            self._failed.add(tid)
            self.state_changed.emit(tid, "unavailable")
            return
        self.state_changed.emit(tid, "loading")
        if any(job.tid == tid and job.generation == generation for job in self._active.values()):
            return
        self._pending[tid] = generation
        self._pending.move_to_end(tid)
        while len(self._pending) > MAX_PENDING:
            self._pending.popitem(last=False)
        self._pump()

    def remove(self, tid):
        self._entries.pop(tid, None)
        self._pending.pop(tid, None)
        self._failed.discard(tid)
        image = self._cache.pop(tid, None)
        if image is not None:
            self._cache_bytes -= image.sizeInBytes()
        for job in list(self._active.values()):
            if job.tid == tid:
                job.reply.abort()

    def close(self):
        self._closed = True
        self._entries.clear()
        self._pending.clear()
        self._failed.clear()
        self._cache.clear()
        self._cache_bytes = 0
        for job in list(self._active.values()):
            job.reply.abort()

    def _pump(self):
        while not self._closed and self._pending and len(self._active) < MAX_ACTIVE:
            # Prioritize recent selections over older queued requests.
            tid, generation = self._pending.popitem(last=True)
            entry = self._entries.get(tid)
            if entry is None or entry[1] != generation:
                continue
            request = QNetworkRequest(QUrl(entry[0]))
            request.setRawHeader(b"User-Agent", f"{APP_NAME}/{APP_VERSION}".encode('ascii'))
            request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                                 QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy)
            reply = self.network.get(request)
            reply.setReadBufferSize(64 * 1024)
            timer = QTimer(reply)
            timer.setSingleShot(True)
            job = Download(tid, generation, reply, timer)
            self._active[reply] = job
            reply.metaDataChanged.connect(lambda job=job: self._check_headers(job))
            reply.readyRead.connect(lambda job=job: self._ready_read(job))
            reply.finished.connect(lambda job=job: self._finished(job))
            timer.timeout.connect(lambda job=job: self._abort(job, "Thumbnail download timed out."))
            timer.start(TIMEOUT_MS)

    def _abort(self, job, reason):
        job.failure = reason
        job.reply.abort()

    def _check_headers(self, job):
        length = job.reply.header(QNetworkRequest.KnownHeaders.ContentLengthHeader)
        if length is not None and int(length) > MAX_DOWNLOAD_BYTES:
            self._abort(job, "Thumbnail exceeds the download size limit.")

    @staticmethod
    def _collect(job):
        chunk = bytes(job.reply.readAll()) if job.reply.bytesAvailable() else b""
        if len(job.data) + len(chunk) > MAX_DOWNLOAD_BYTES:
            job.failure = "Thumbnail exceeds the download size limit."
            return False
        job.data.extend(chunk)
        return True

    def _ready_read(self, job):
        if not self._collect(job):
            job.reply.abort()

    @staticmethod
    def _decode(data):
        buffer = QBuffer()
        buffer.setData(QByteArray(data))
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        reader = QImageReader(buffer)
        size = reader.size()
        if not size.isValid() or size.width() * size.height() > MAX_IMAGE_PIXELS:
            raise ValueError("Thumbnail dimensions are invalid or exceed the pixel limit.")
        reader.setAutoTransform(True)
        if size.width() > PREVIEW_SIZE.width() or size.height() > PREVIEW_SIZE.height():
            reader.setScaledSize(size.scaled(PREVIEW_SIZE, Qt.AspectRatioMode.KeepAspectRatio))
        image = reader.read()
        if image.isNull():
            raise ValueError("Thumbnail is not a supported image.")
        # Some decoders do not honor setScaledSize.
        if image.width() > PREVIEW_SIZE.width() or image.height() > PREVIEW_SIZE.height():
            image = image.scaled(PREVIEW_SIZE, Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
        return image

    def _finished(self, job):
        if self._active.pop(job.reply, None) is None:
            return
        job.timer.stop()
        entry = self._entries.get(job.tid)
        try:
            # Discard responses for removed tasks or replaced thumbnail URLs.
            if self._closed or entry is None or entry[1] != job.generation:
                return
            self._collect(job)
            if job.failure:
                raise ValueError(job.failure)
            if job.reply.error() != QNetworkReply.NetworkError.NoError:
                raise OSError(job.reply.errorString())
            image = self._decode(job.data)
            self._cache[job.tid] = image
            self._cache_bytes += image.sizeInBytes()
            while len(self._cache) > MAX_CACHE_ENTRIES or self._cache_bytes > MAX_CACHE_BYTES:
                _, evicted = self._cache.popitem(last=False)
                self._cache_bytes -= evicted.sizeInBytes()
            self.image_ready.emit(job.tid, image)
        except (OSError, ValueError) as error:
            self._failed.add(job.tid)
            log_warning("Thumbnail unavailable", error, [("task", job.tid), ("url", entry[0])],
                        once_key=("qt-thumbnail", job.tid, job.generation))
            self.state_changed.emit(job.tid, "unavailable")
        finally:
            job.reply.deleteLater()
            self._pump()
