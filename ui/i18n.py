"""English/Turkish presentation only; engine IDs, settings values and logs stay stable."""
import re
from string import Formatter
from weakref import WeakKeyDictionary

from PySide6.QtCore import QObject, QLibraryInfo, QLocale, QSignalBlocker, Qt, QTranslator
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (QAbstractButton, QApplication, QComboBox, QGroupBox,
                               QLabel, QLineEdit, QPlainTextEdit, QSpinBox, QWidget)
from shiboken6 import isValid

from settings import SCHEMA_VERSION, _read_document
from ui.help_texts import FIELD_HELP


TR = {
    'An independent video and audio downloader for YouTube and other sites supported by yt-dlp.': 'YouTube başta olmak üzere yt-dlp tarafından desteklenen siteler için bağımsız video ve ses indirici.',
    'Download a single video, or add multiple links and playlists.': 'Tek bir video indirin veya birden fazla bağlantı ve oynatma listesi ekleyin.',
    'Paste a video URL…': 'Video bağlantısını yapıştırın…', 'Video URL': 'Video bağlantısı',
    'Add to queue': 'Kuyruğa ekle', 'Add multiple links': 'Birden fazla bağlantı ekle',
    'Add multiple links…': 'Birden fazla bağlantı ekle…', 'Download options': 'İndirme seçenekleri',
    'Advanced options': 'Gelişmiş seçenekler', 'Mode': 'Mod', 'Video': 'Video', 'Audio': 'Ses',
    'Resolution limit': 'Çözünürlük sınırı', 'Audio format': 'Ses biçimi',
    'Container': 'Kapsayıcı', 'Video codec': 'Video codec’i', 'Audio codec': 'Ses codec’i',
    'Conversion quality': 'Dönüştürme kalitesi', 'Conversion bitrate': 'Dönüştürme bit hızı',
    'High quality': 'Yüksek kalite', 'Balanced': 'Dengeli', 'Smaller file': 'Daha küçük dosya',
    'Automatic': 'Otomatik', 'Automatic container': 'Otomatik kapsayıcı', 'Original': 'Orijinal',
    'No audio': 'Ses yok', 'Bit depth': 'Bit derinliği', 'WAV (LPCM)': 'WAV (LPCM)',
    '16-bit': '16 bit', '24-bit': '24 bit', 'Custom (advanced)': 'Özel (gelişmiş)',
    'Parallel downloads': 'Eş zamanlı indirmeler', 'Parallel video conversions': 'Eş zamanlı video dönüşümleri',
    'FFmpeg threads': 'FFmpeg iş parçacıkları', 'Parallel fragments': 'Eş zamanlı parçalar',
    'Media download budget': 'Medya bağlantı bütçesi', 'Trim start / end': 'Kırpma başlangıcı / bitişi',
    'Trim start': 'Kırpma başlangıcı', 'Trim end': 'Kırpma bitişi',
    'Start: 1:30': 'Başlangıç: 1:30', 'End: 2:45': 'Bitiş: 2:45', 'end': 'son',
    'Trim: {start} – {end}': 'Kırpma: {start} – {end}',
    'No download folder selected': 'İndirme klasörü seçilmedi', 'Download folder': 'İndirme klasörü',
    'Choose folder': 'Klasör seç', 'Choose download folder': 'İndirme klasörünü seç',
    'Choose download folder…': 'İndirme klasörünü seç…',
    'Pause': 'Duraklat', 'Resume': 'Devam et', 'Cancel selected': 'Seçilenleri iptal et',
    'Retry failed tasks': 'Başarısız görevleri yeniden dene', 'Retry selected': 'Seçilenleri yeniden dene',
    'Retry selected failed tasks': 'Seçilen başarısız görevleri yeniden dene',
    'Retry all failed tasks': 'Tüm başarısız görevleri yeniden dene', 'Clear list…': 'Listeyi temizle…',
    'Show in folder': 'Klasörde göster', 'View diagnostics': 'Tanılama ayrıntıları',
    'Task actions': 'Görev işlemleri', 'Overall progress': 'Genel ilerleme', 'Ready': 'Hazır',
    '0 tasks': '0 görev', '{total} task(s) • {pending} queued': '{total} görev • {pending} kuyrukta',
    'Task': 'Görev', 'Title': 'Başlık', 'Format': 'Biçim', 'Quality': 'Kalite', 'Status': 'Durum',
    'Progress': 'İlerleme', 'Speed': 'Hız', 'Time left': 'Kalan süre', 'Download tasks': 'İndirme görevleri',
    'Your queue is empty. Paste a video URL above to get started.': 'Kuyruk boş. Başlamak için yukarıya bir video bağlantısı yapıştırın.',
    'Preview': 'Önizleme', 'Select a task to see its preview': 'Önizlemek için bir görev seçin',
    'Preview not available yet.': 'Önizleme henüz hazır değil.', 'Loading preview…': 'Önizleme yükleniyor…',
    'Preview unavailable': 'Önizleme kullanılamıyor', 'Resolving title…': 'Başlık alınıyor…',
    'Completed': 'Tamamlandı', 'Completed with warnings': 'Uyarılarla tamamlandı',
    'Failed (see details)': 'Başarısız (ayrıntılara bakın)', 'Cancelled': 'İptal edildi',
    'Cancelling…': 'İptal ediliyor…', 'Paused': 'Duraklatıldı',
    'Waiting for conversion': 'Dönüştürme bekleniyor', 'Waiting for connection': 'Bağlantı bekleniyor',
    'Converting': 'Dönüştürülüyor', 'Remuxing': 'Akışlar birleştiriliyor', 'Queued': 'Kuyrukta',
    'Downloading': 'İndiriliyor', 'Processing': 'İşleniyor', 'Trimming': 'Kırpılıyor',
    'Verifying compatibility': 'Uyumluluk doğrulanıyor', 'Saving': 'Kaydediliyor', 'Starting…': 'Başlatılıyor…',
    '&File': '&Dosya', '&Help': '&Yardım', 'Focus URL': 'Bağlantı alanına git', 'Quit': 'Çıkış',
    'Language': 'Dil', 'English': 'English', 'Türkçe': 'Türkçe',
    'Open log folder': 'Günlük klasörünü aç', 'Copy error details': 'Hata ayrıntılarını kopyala',
    'Export diagnostic report…': 'Tanılama raporunu dışa aktar…', 'Export diagnostic report': 'Tanılama raporunu dışa aktar',
    'Text files (*.txt)': 'Metin dosyaları (*.txt)', 'About BekoDLP': 'BekoDLP hakkında',
    'BekoDLP\n\nAn independent video and audio downloader for YouTube and other sites supported by yt-dlp.\nInterface by PySide6. Media processing by FFmpeg.\nNot affiliated with the yt-dlp project.\n\nThis project was developed with AI using Claude and ChatGPT.':
        'BekoDLP\n\nYouTube başta olmak üzere yt-dlp tarafından desteklenen siteler için bağımsız video ve ses indirici.\nArayüz: PySide6. Medya işleme: FFmpeg.\nyt-dlp projesiyle bağlantılı değildir.\n\nBu proje, Claude ve ChatGPT kullanılarak yapay zekâ ile geliştirilmiştir.',
    'Paste one video or playlist URL per line.': 'Her satıra bir video veya oynatma listesi bağlantısı yapıştırın.',
    'Close': 'Kapat', '{count} link': '{count} bağlantı', '{count} links': '{count} bağlantı',
    'Clear task list': 'Görev listesini temizle', 'Cancel tasks': 'Görevleri iptal et',
    'Remove queued tasks as well?\n\nYes: clear finished and queued tasks.\nNo: clear only completed, failed and cancelled tasks.\nActive downloads and saved files are kept.':
        'Kuyruktaki görevler de kaldırılsın mı?\n\nEvet: bitenleri ve kuyruktakileri temizle.\nHayır: yalnızca tamamlanan, başarısız ve iptal edilenleri temizle.\nEtkin indirmeler ve kaydedilen dosyalar korunur.',
    'Cancellation and process termination have timed out. Close now?\n\nCompleted files are preserved, but partial files may remain.\n\nChoose No to wait another {seconds} seconds.':
        'İptal ve işlem sonlandırma zaman aşımına uğradı. Şimdi kapatılsın mı?\n\nTamamlanan dosyalar korunur; kısmi dosyalar kalabilir.\n\n{seconds} saniye daha beklemek için Hayır’ı seçin.',
    'Cancel {count} selected tasks?': 'Seçilen {count} görev iptal edilsin mi?',
    'Select a task': 'Bir görev seçin', 'Select the tasks you want to cancel.': 'İptal etmek istediğiniz görevleri seçin.',
    'Missing URL': 'Bağlantı eksik', 'Enter a video URL first.': 'Önce bir video bağlantısı girin.',
    'Choose an existing download folder first.': 'Önce mevcut bir indirme klasörü seçin.',
    'Invalid trim range': 'Geçersiz kırpma aralığı', 'Invalid output profile': 'Geçersiz çıktı profili',
    'Use seconds, mm:ss or hh:mm:ss (for example, 90 or 1:30). Values must be non-negative, and the end must be later than the start.':
        'Saniye, mm:ss veya hh:mm:ss kullanın (örneğin 90 veya 1:30). Değerler negatif olamaz; bitiş başlangıçtan sonra olmalıdır.',
    'Could not show file': 'Dosya gösterilemedi',
    'The file may have been moved or deleted, or the file manager could not start.': 'Dosya taşınmış veya silinmiş olabilir ya da dosya yöneticisi başlatılamadı.',
    'Task diagnostics': 'Görev tanılaması',
    'Diagnostic details for {task}. Engine and provider messages are shown verbatim. Use the diagnostic actions to inspect or share the error context.':
        '{task} için tanılama ayrıntıları. Motor ve sağlayıcı mesajları özgün hâliyle gösterilir. Hatayı incelemek veya paylaşmak için tanılama işlemlerini kullanın.',
    'Error details copied.': 'Hata ayrıntıları kopyalandı.', 'Could not open log folder': 'Günlük klasörü açılamadı',
    'The log folder could not be opened.': 'Günlük klasörü açılamadı.',
    'Could not export diagnostic report': 'Tanılama raporu dışa aktarılamadı',
    'The report could not be saved. Choose another filename or folder.': 'Rapor kaydedilemedi. Başka bir dosya adı veya klasör seçin.',
    'Diagnostic report saved: {path}': 'Tanılama raporu kaydedildi: {path}',
    'Preferences warning': 'Tercih uyarısı',
    'Your recent preferences could not be saved. Click OK to continue closing.': 'Son tercihleriniz kaydedilemedi. Kapatmaya devam etmek için Tamam’a tıklayın.',
    'Use the log file location below for details.': 'Ayrıntılar için aşağıdaki günlük dosyasına bakın.',
    'Closing — cancelling tasks and cleaning up…': 'Kapatılıyor — görevler iptal edilip geçici dosyalar temizleniyor…',
    'Some tasks have not stopped': 'Bazı görevler henüz durmadı',
    'Preferences could not be loaded. Default values are in use. The original settings file will not be overwritten.':
        'Tercihler yüklenemedi. Varsayılan değerler kullanılıyor. Özgün ayar dosyasının üzerine yazılmayacak.',
    'Some preferences were invalid or recovered. Valid settings or defaults were loaded.': 'Bazı tercihler geçersizdi veya kurtarıldı. Geçerli ayarlar ya da varsayılanlar yüklendi.',
    'Your preferences could not be saved. They will still apply for this session.': 'Tercihleriniz kaydedilemedi. Bu oturumda geçerli olmaya devam edecekler.',
    'Unexpected error': 'Beklenmeyen hata',
    'An unexpected error occurred. Use the diagnostic actions for details.': 'Beklenmeyen bir hata oluştu. Ayrıntılar için tanılama işlemlerini kullanın.',
    'Startup failed': 'Başlatma başarısız', 'Diagnostic action failed': 'Tanılama işlemi başarısız',
    'The download engine could not start. Install the project dependencies and make sure ffmpeg and ffprobe are available.':
        'İndirme motoru başlatılamadı. Proje bağımlılıklarını kurun ve ffmpeg ile ffprobe araçlarının kullanılabilir olduğunu kontrol edin.',
    'The interface could not start. Use the diagnostic actions for details.': 'Arayüz başlatılamadı. Ayrıntılar için tanılama işlemlerini kullanın.',
    'No failed tasks to retry.': 'Yeniden denenecek başarısız görev yok.',
    'No tasks could be retried. The URL may already be active, or cleanup is still running.': 'Görevler yeniden denenemedi. Bağlantı zaten etkin olabilir veya temizlik sürüyor olabilir.',
    'Task cancelled.': 'Görev iptal edildi.', 'Download completed.': 'İndirme tamamlandı.', 'Task status updated.': 'Görev durumu güncellendi.',
    '{count} video(s) added to the queue.': '{count} video kuyruğa eklendi.',
    '{count} task(s) queued for retry.': '{count} görev yeniden denemek için kuyruğa alındı.',
    '{count} video(s) added to the queue. {skipped} skipped: the URL is already queued or active.': '{count} video kuyruğa eklendi. {skipped} atlandı: bağlantı zaten kuyrukta veya etkin.',
    '{count} task(s) queued for retry. {skipped} skipped: the URL is already queued or active.': '{count} görev yeniden denemek için kuyruğa alındı. {skipped} atlandı: bağlantı zaten kuyrukta veya etkin.',
    'Resolving {count} link(s)…': '{count} bağlantı çözümleniyor…',
    'These links are already queued, downloading or being resolved.': 'Bu bağlantılar zaten kuyrukta, indiriliyor veya çözümleniyor.',
    'Cancellation requested for {count} task(s).': '{count} görev için iptal istendi.', 'Removed {count} task(s).': '{count} görev kaldırıldı.',
    'No tasks to clear.': 'Temizlenecek görev yok.', 'Playlist link': 'Oynatma listesi bağlantısı',
    "Use 'Add multiple links' to download a playlist or album.": "Oynatma listesi veya albüm indirmek için 'Birden fazla bağlantı ekle' seçeneğini kullanın.",
    'Link resolution failed': 'Bağlantı çözümlenemedi',
    'The link could not be resolved. Check the URL and your connection.': 'Bağlantı çözümlenemedi. Adresi ve internet bağlantınızı kontrol edin.',
    'Playlist information': 'Oynatma listesi bilgisi',
    'The playlist could not be fully expanded. Available videos were queued.': 'Oynatma listesi tamamen alınamadı. Alınabilen videolar kuyruğa eklendi.',
    'Mode: {value}': 'Mod: {value}', 'Container: {value}': 'Kapsayıcı: {value}',
    'Video codec: {value}': 'Video codec’i: {value}', 'Audio codec: {value}': 'Ses codec’i: {value}',
    'Bit depth: {value}': 'Bit derinliği: {value}', 'Saved as: {value}': 'Kaydedilen dosya: {value}',
    'Task: {value}': 'Görev: {value}', 'Attempt: {value}': 'Deneme: {value}', 'Stage: {value}': 'Aşama: {value}',
    '{status}. Right-click for diagnostic details.': '{status}. Tanılama ayrıntıları için sağ tıklayın.',
    'Double-click to show the downloaded file in its folder.': 'İndirilen dosyayı klasöründe görmek için çift tıklayın.',
    'Log file: {path}': 'Günlük dosyası: {path}', 'Fallback log file: {path}': 'Yedek günlük dosyası: {path}',
    'Latest diagnostics could not be saved.\nPrimary log: {primary}\nFallback log: {fallback}': 'Son tanılama kaydedilemedi.\nAna günlük: {primary}\nYedek günlük: {fallback}',
    'Codecs, conversion, trimming and download limits': 'Codec, dönüştürme, kırpma ve indirme sınırları',
    'Choose an audio output format; codec and container are set together.': 'Bir ses biçimi seçin; codec ve kapsayıcı birlikte ayarlanır.',
    'FLAC/LPCM only. Original keeps known source precision; unavailable precision uses 24-bit. Increasing depth does not restore lost quality.': 'Yalnızca FLAC/LPCM için. Orijinal, bilinen kaynak bit derinliğini korur; bilinmiyorsa 24 bit kullanılır. Bit derinliğini artırmak kaybolan kaliteyi geri getirmez.',
    'Maximum videos encoded at once, within the parallel task limit. Active conversions finish when this limit is reduced.': 'Görev sınırı içinde aynı anda dönüştürülen en fazla video sayısı. Sınır azaltıldığında süren dönüşümler tamamlanır.',
    'Threads per FFmpeg codec/filter pool. Automatic shares available CPU capacity across video conversions, up to 8 threads per pool. Running processes keep their current budget.': 'FFmpeg codec/filtre havuzu başına iş parçacığı sayısı. Otomatik, işlemci kapasitesini video dönüşümleri arasında havuz başına en fazla 8 parçacıkla paylaşır. Süren işlemler mevcut sınırını korur.',
    'Maximum parallel fragments for native DASH/HLS downloads. The shared media budget can lower this number. Applies to new download sessions.': 'DASH/HLS indirmeleri için eş zamanlı parça sınırı. Ortak medya bütçesi bu sayıyı düşürebilir. Yeni indirme oturumlarında geçerlidir.',
    'Shared media-download capacity, including fragment workers and request overhead. Metadata and previews have separate limits. Existing downloads finish when the budget is reduced.': 'Parça işçileri ve istekler için ortak medya indirme kapasitesi. Üst veri ve önizleme sınırları ayrıdır. Bütçe düşürüldüğünde süren indirmeler tamamlanır.',
    'Automatic uses 192k for MP3/AAC or 128k for Opus when conversion is needed. Matching codecs are copied and keep the source bitrate.': 'Dönüştürme gerekiyorsa Otomatik, MP3/AAC için 192k ve Opus için 128k kullanır. Eşleşen codec’ler kopyalanır ve kaynak bit hızı korunur.',
    'Used only when video is converted. Matching codecs are copied without quality changes.': 'Yalnızca video dönüştürülürken kullanılır. Eşleşen codec’ler kalite değiştirilmeden kopyalanır.',
    'Trim start (e.g. 1:30). Leave blank for the full range.': 'Kırpma başlangıcı (ör. 1:30). Tüm aralık için boş bırakın.',
    'Trim end (e.g. 1:30). Leave blank for the full range.': 'Kırpma bitişi (ör. 1:30). Tüm aralık için boş bırakın.',
    'Lossless output does not restore quality lost in the source. Original preserves known bit depth; otherwise 24-bit is used.': 'Kayıpsız çıktı, kaynakta kaybolmuş kaliteyi geri getirmez. Orijinal, bilinen bit derinliğini korur; bilinmiyorsa 24 bit kullanılır.',
    'Matching codecs are copied. Quality and bitrate apply only when conversion is needed (slower, potentially lossy).': 'Eşleşen codec’ler kopyalanır. Kalite ve bit hızı yalnızca dönüştürme gerektiğinde uygulanır (daha yavaş ve kayıplı olabilir).',
    'Original streams are copied. An incompatible container requires another container or explicit codec conversion.': 'Orijinal akışlar kopyalanır. Kapsayıcı uyumsuzsa başka bir kapsayıcı veya codec dönüşümü seçin.',
    '{choices} reset to Original to match the selected output profile. ': '{choices}, çıktı profiline uyması için Orijinal olarak sıfırlandı. ',
    'No encoder is available in this FFmpeg installation.': 'Bu FFmpeg kurulumunda uygun kodlayıcı yok.',
    'Not supported by the selected output profile.': 'Seçilen çıktı profili desteklemiyor.',
    'Invalid resolution limit.': 'Geçersiz çözünürlük sınırı.', 'Unknown output mode.': 'Bilinmeyen çıktı modu.',
    'The selected container is not available for this output mode.': 'Seçilen kapsayıcı bu çıktı modunda kullanılamıyor.',
    'Unknown video or audio codec.': 'Bilinmeyen video veya ses codec’i.',
    '{selected} is not supported by the {profile} profile. Choose compatible codecs or a different container.': '{profile} profili {selected} seçimini desteklemiyor. Uyumlu codec’ler veya başka bir kapsayıcı seçin.',
    'Invalid audio bit depth.': 'Geçersiz ses bit derinliği.', 'Invalid audio bitrate.': 'Geçersiz ses bit hızı.',
    'Invalid video quality preset.': 'Geçersiz video kalite ayarı.',
    'Clear finished tasks, with an option to remove queued tasks as well.': 'Tamamlanan görevleri temizleyin; kuyruktakileri de kaldırmayı seçebilirsiniz.',
}

TR.update({
    'Check for updates…': 'Güncellemeleri denetle…',
    'Third-party licenses': 'Üçüncü taraf lisansları',
    'The license folder could not be opened.': 'Lisans klasörü açılamadı.',
    'Software update': 'Uygulama güncellemesi',
    'No published release is available yet.': 'Henüz yayımlanmış bir sürüm yok.',
    'You are using the latest version.': 'En güncel sürümü kullanıyorsunuz.',
    'Version {version} is available. Download it now?': '{version} sürümü mevcut. Şimdi indirilsin mi?',
    'Your current downloads will continue. Installation requires a separate confirmation.': 'Mevcut indirmeleriniz devam eder. Kurulum için ayrıca onayınız istenir.',
    'Version {version} is available. Open the release page?': '{version} sürümü mevcut. Sürüm sayfası açılsın mı?',
    'Automatic installation is available in the portable Windows folder build.': 'Otomatik kurulum, taşınabilir Windows klasör sürümünde kullanılabilir.',
    'Verifying and unpacking update…': 'Güncelleme doğrulanıyor ve hazırlanıyor…',
    'Downloading update…': 'Güncelleme indiriliyor…',
    'The update is downloaded and verified. Restart and install now?': 'Güncelleme indirildi ve doğrulandı. Şimdi yeniden başlatılıp kurulsun mu?',
    'Installation waits until all queued and active downloads finish. You can also install later from Help → Check for updates.': 'Kurulum için kuyruktaki ve aktif indirmelerin bitmesi gerekir. Daha sonra Yardım → Güncellemeleri denetle bölümünden de kurabilirsiniz.',
    'Finish or cancel pending downloads before installing the update. The verified update is ready for this session.': 'Güncellemeyi kurmadan önce bekleyen indirmeleri tamamlayın veya iptal edin. Doğrulanmış güncelleme bu oturum için hazır tutuluyor.',
    'The update could not be completed. Your current version is unchanged.': 'Güncelleme tamamlanamadı. Mevcut sürümünüz değiştirilmedi.',
})

TR.update({english: turkish for english, turkish in FIELD_HELP.values()})
TR['About {field}'] = '{field} hakkında'

_language = 'en'
_bindings = WeakKeyDictionary()
_callbacks = WeakKeyDictionary()
_qt_translator = None
_source_role = int(Qt.ItemDataRole.UserRole) + 99
_reverse = {value: key for key, value in TR.items() if '{' not in key}
_patterns = []
for source, translated in TR.items():
    if '{' in source:
        expression = ''.join(re.escape(literal) + (f'(?P<{field}>.*?)' if field else '')
                             for literal, field, _, _ in Formatter().parse(source))
        _patterns.append((re.compile('^' + expression + '$', re.DOTALL), translated))


def language():
    return _language


def tr(source, **values):
    if _language == 'en':
        return source.format(**values) if values else source
    translated = TR.get(source)
    if translated is not None:
        return translated.format(**values) if values else translated
    if not values:
        for pattern, template in _patterns:
            match = pattern.fullmatch(source)
            if match:
                return template.format(**match.groupdict())
    return source.format(**values) if values else source


def duration_text(text):
    if _language == 'tr':
        return re.sub(r'(?<=\d)([dhms])\b', lambda match: {'d': 'g', 'h': 'sa', 'm': 'dk', 's': 'sn'}[match[1]], text)
    return text


def translate_combo(combo):
    blocker = QSignalBlocker(combo)
    for index in range(combo.count()):
        source = combo.itemData(index, _source_role)
        if source is None:
            source = _reverse.get(combo.itemText(index), combo.itemText(index))
            combo.setItemData(index, source, _source_role)
        combo.setItemText(index, tr(source))
        tip = combo.itemData(index, Qt.ItemDataRole.ToolTipRole)
        if tip:
            combo.setItemData(index, tr(_reverse.get(tip, tip)), Qt.ItemDataRole.ToolTipRole)
    del blocker


def register(root, refresh=None):
    """Bind static UI properties once; dynamic callbacks retain raw task state."""
    for obj in [root, *root.findChildren(QObject)]:
        if not isinstance(obj, (QWidget, QAction)) or not isValid(obj):
            continue
        properties = ['toolTip', 'statusTip', 'accessibleName', 'windowTitle']
        if isinstance(obj, (QLabel, QAbstractButton, QAction)) and not obj.property('_i18n_user_text'):
            properties.append('text')
        if isinstance(obj, QGroupBox):
            properties.append('title')
        if isinstance(obj, (QLineEdit, QPlainTextEdit)):
            properties.append('placeholderText')
        if isinstance(obj, QSpinBox):
            properties.append('specialValueText')
        bindings = _bindings.setdefault(obj, {})
        for prop in properties:
            getter = getattr(obj, prop, None)
            if not callable(getter):
                continue
            value = getter()
            source = _reverse.get(value, value)
            if source in TR:
                bindings[prop] = source
        if isinstance(obj, QComboBox):
            bindings['_items'] = True
        _apply(obj, bindings)
    if refresh:
        _callbacks[root] = refresh.__name__
        refresh()


def _apply(obj, bindings):
    blocker = QSignalBlocker(obj)
    for prop, source in bindings.items():
        if prop == '_items':
            translate_combo(obj)
        else:
            getattr(obj, 'set' + prop[0].upper() + prop[1:])(tr(source))
    del blocker


def set_language(code):
    global _language, _qt_translator
    if code not in ('en', 'tr'):
        raise ValueError('Unsupported interface language')
    app = QApplication.instance()
    QLocale.setDefault(QLocale('tr_TR' if code == 'tr' else 'en_US'))
    if app and _qt_translator is not None:
        app.removeTranslator(_qt_translator)
        _qt_translator.deleteLater()
        _qt_translator = None
    if app and code == 'tr':
        translator = QTranslator(app)
        if translator.load('qtbase_tr', QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
            app.installTranslator(translator)
            _qt_translator = translator
        else:
            translator.deleteLater()
    changed = _language != code
    _language = code
    if not changed:
        return
    for obj, bindings in list(_bindings.items()):
        if isValid(obj):
            _apply(obj, bindings)
    for obj, method in list(_callbacks.items()):
        if isValid(obj):
            getattr(obj, method)()


def startup_language(path):
    """Read only the language, leaving recovery/reporting to Preferences.load()."""
    try:
        data = _read_document(path)
        if (isinstance(data, dict) and type(data.get('schema_version', SCHEMA_VERSION)) is int
                and data.get('schema_version', SCHEMA_VERSION) == SCHEMA_VERSION):
            code = data.get('language', 'en')
            return code if isinstance(code, str) and code in ('en', 'tr') else 'en'
    except (OSError, ValueError, RecursionError):
        pass
    return 'en'
