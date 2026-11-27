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
from cookie_options import BROWSER_COOKIE_COPY_HELP


TR = {
    BROWSER_COOKIE_COPY_HELP:
        'Tarayıcının çerez veritabanı okunamadı. Tarayıcıyı arka plan işlemleri dahil tamamen kapatıp '
        'yeniden deneyin. Sorun sürerse Çerez dosyası seçeneğini kullanarak Netscape biçiminde '
        'bir cookies.txt dosyası seçin.',
    'Settings profile': 'Ayar profili', 'Saved settings profiles': 'Kayıtlı ayar profilleri',
    'Save profile…': 'Profili kaydet…', 'Save profile': 'Profili kaydet', 'Delete profile': 'Profili sil',
    'Custom settings': 'Özel ayarlar',
    'Music (preset)': 'Müzik (hazır)', 'Original archive (preset)': 'Orijinal arşiv (hazır)',
    'Smaller file (preset)': 'Küçük boyut (hazır)',
    'Profile name (an existing name updates that profile):': 'Profil adı (aynı adlı profil varsa güncellenir):',
    'Save output codecs, quality, subtitles, cover artwork, chapters and naming. Profiles apply to newly queued downloads.':
        'Çıktı codec, kalite, altyazı, kapak, bölüm ve adlandırma ayarlarını kaydedin. Profiller yeni eklenen indirmelere uygulanır.',
    'Profile applied. Newly queued downloads use these settings.': 'Profil uygulandı. Yeni eklenen indirmeler bu ayarları kullanacak.',
    'Profile saved.': 'Profil kaydedildi.', 'Profile deleted.': 'Profil silindi.',
    'Profiles apply for this session, but could not be saved to disk.': 'Profiller bu oturumda kullanılabilir, ancak diske kaydedilemedi.',
    'Enter a profile name with 1 to 64 characters.': '1 ile 64 karakter arasında bir profil adı girin.',
    'The saved profile contains invalid settings.': 'Kayıtlı profil geçersiz ayarlar içeriyor.',
    'The saved profile record is invalid.': 'Ayar profili kaydı geçersiz.',
    'At most 30 saved profiles are supported.': 'En fazla 30 ayar profili kaydedilebilir.',
    'Saved profile names and identifiers must be unique.': 'Profil adları ve kimlikleri benzersiz olmalıdır.',
    'This profile requires an unavailable encoder: {codec}.': 'Bu profil için gerekli kodlayıcı bulunamadı: {codec}.',
    'Tool updates': 'Araç güncellemeleri', 'Update download tools…': 'İndirme araçlarını güncelle…',
    'Tool': 'Araç', 'Installed version': 'Kurulu sürüm', 'Latest version': 'Son sürüm',
    'Not checked': 'Kontrol edilmedi', 'Not installed': 'Kurulu değil', 'Unknown': 'Bilinmiyor',
    'Check': 'Kontrol et', 'Download and install': 'İndir ve kur',
    'Install downloaded update': 'İndirilen güncellemeyi kur', 'Cancel download': 'İndirmeyi iptal et',
    'Checks run only when requested.': 'Kontroller yalnızca istediğinizde yapılır.',
    'Update yt-dlp, FFmpeg/FFprobe and Deno separately. Downloads are verified with SHA-256. Installation requires an idle queue. Existing tools remain available if an update fails.':
        'yt-dlp, FFmpeg/FFprobe ve Deno araçlarını ayrı ayrı güncelleyin. İndirilen dosyalar SHA-256 ile doğrulanır. Kurulum için kuyruğun boş olması gerekir. Güncelleme başarısız olursa mevcut araçlar korunur.',
    'Tool updates currently support Windows x64.': 'Araç güncellemeleri şu anda Windows x64 sistemlerini destekliyor.',
    'Checking tool release…': 'Araç sürümü kontrol ediliyor…',
    'Downloading and verifying tool update…': 'Araç güncellemesi indiriliyor ve doğrulanıyor…',
    'Finish or cancel queued downloads before installing the tool update. The verified download is ready.':
        'Araç güncellemesini kurmadan önce kuyruktaki indirmeleri tamamlayın veya iptal edin. Doğrulanan güncelleme hazır.',
    'Tool updated successfully. New downloads use the updated tool.': 'Araç güncellendi. Yeni indirmeler güncel aracı kullanacak.',
    'Tool installation failed. Existing tools were preserved.': 'Araç kurulumu başarısız oldu. Mevcut araçlar korundu.',
    'Tool update failed. Existing tools were preserved.': 'Araç güncellemesi başarısız oldu. Mevcut araçlar korundu.',
    'Tool update cancelled.': 'Araç güncellemesi iptal edildi.',
    'A tool update is available.': 'Araç güncellemesi mevcut.', 'This tool is up to date.': 'Bu araç güncel.',
    'Multiple time ranges': 'Birden fazla zaman aralığı',
    'Time ranges, one per line': 'Her satıra bir zaman aralığı',
    'Time ranges: {count}': 'Kesit sayısı: {count}',
    'Enter start - end on each line, using seconds, mm:ss or hh:mm:ss. Up to 100 ranges, in the entered order. Each range becomes a separate numbered file. This replaces the single trim fields. Section mode downloads each range; full mode downloads the source once. Chapter embedding is supported; separate chapter files cannot be combined with ranges.':
        'Her satıra başlangıç - bitiş yazın; saniye, dd:ss veya sa:dd:ss kullanabilirsiniz. Girilen sırayla en fazla 100 aralık desteklenir. Her kesit ayrı, numaralı dosyaya kaydedilir. Bu alan tek kesit alanlarının yerine geçer. Kesit modunda her aralık ayrı indirilir; tam indirmede kaynak bir kez alınır. Bölüm gömme desteklenir; bölümleri ayrı dosyalara kaydetme ile birlikte kullanılamaz.',
    'Invalid time ranges.': 'Geçersiz zaman aralıkları.',
    'At most 100 time ranges are supported.': 'En fazla 100 zaman aralığı desteklenir.',
    'Range {number}: use start - end (for example, 0:30 - 1:10).': '{number}. aralık: başlangıç - bitiş yazın (örneğin, 0:30 - 1:10).',
    'Use either a single trim range or multiple time ranges.': 'Tek kesit veya birden fazla zaman aralığı seçeneklerinden birini kullanın.',
    'Multiple time ranges require a video or audio output profile.': 'Birden fazla zaman aralığı için video veya ses çıktı profili seçilmelidir.',
    'Use either multiple time ranges or separate chapter files. Chapter embedding can be combined with ranges.': 'Birden fazla zaman aralığı veya bölümleri ayrı dosyalara kaydetme seçeneklerinden birini kullanın. Bölüm gömme, zaman aralıklarıyla kullanılabilir.',
    'A time range starts beyond the source duration.': 'Bir zaman aralığı kaynak süresinin ötesinde başlıyor.',
    'A time range has no media to save.': 'Bir zaman aralığında kaydedilecek medya yok.',
    'A time range produced an empty file.': 'Bir zaman aralığı boş dosya üretti.',
    'Range {number} starts at {actual}s instead of {requested}s to preserve copied video at a keyframe. Re-encode video for exact cuts.':
        '{number}. kesit, video anahtar kareden kopyalandığı için {requested}s yerine {actual}s konumundan başlıyor. Kesin kesim için videoyu yeniden kodlayın.',
    'Chapters': 'Bölümler', 'Chapter output': 'Bölüm çıktısı',
    'Do not process chapters': 'Bölümleri işleme', 'Embed chapter markers': 'Bölüm işaretlerini dosyaya göm',
    'Save each chapter as a separate file': 'Her bölümü ayrı dosya olarak kaydet',
    'Embedding supports MP3, M4A, MP4 and MKV. Separate files share one output folder and use the selected codecs. Copying video may move cuts to keyframes. Missing chapters keep the normal output with a warning.':
        'Bölüm gömme MP3, M4A, MP4 ve MKV biçimlerini destekler. Ayrı dosyalar aynı çıktı klasörüne, seçili codec ile kaydedilir. Video kopyalanırken kesim yeri anahtar kareye kayabilir. Bölüm yoksa normal çıktı korunur ve uyarı gösterilir.',
    'Embedding chapter markers': 'Bölüm işaretleri gömülüyor',
    'Invalid chapter setting.': 'Geçersiz bölüm ayarı.',
    'Chapter processing requires an explicit video or audio output profile.': 'Bölüm işlemleri için video veya ses çıktı profili seçilmelidir.',
    'Chapter markers are not supported by this output format; the media was preserved.': 'Bu çıktı biçimi bölüm işaretlerini desteklemiyor; medya korundu.',
    'No source chapters were available in this range; the normal output was kept.': 'Bu aralıkta kaynak bölüm bilgisi bulunamadı; normal çıktı korundu.',
    'Only the first 1000 source chapters were processed.': 'Kaynağın yalnızca ilk 1000 bölümü işlendi.',
    'Chapter markers could not be embedded; the media was preserved.': 'Bölüm işaretleri gömülemedi; medya korundu.',
    '{count} chapters': '{count} bölüm',
    'Chapter {number} starts at {actual}s instead of {requested}s to preserve copied video at a keyframe. Re-encode video for exact cuts.':
        '{number}. bölüm, video anahtar kareden kopyalandığı için {requested}s yerine {actual}s konumundan başlıyor. Kesin kesim için videoyu yeniden kodlayın.',
    'Cover artwork': 'Kapak görseli', 'Embed cover artwork': 'Kapak görselini göm',
    'Embed the source thumbnail as cover artwork': 'Kaynağın küçük resmini kapak olarak dosyaya göm',
    'Supports MP3, M4A, FLAC, MP4 and MKV. A missing image or unsupported format keeps the media and adds a warning. Audio and video are not re-encoded for the cover.':
        'MP3, M4A, FLAC, MP4 ve MKV desteklenir. Görsel bulunamazsa veya biçim desteklenmiyorsa medya korunur ve uyarı eklenir. Kapak için ses ve video yeniden kodlanmaz.',
    'Invalid cover artwork setting.': 'Geçersiz kapak görseli ayarı.',
    'Downloading cover artwork': 'Kapak görseli indiriliyor', 'Preparing cover artwork': 'Kapak görseli hazırlanıyor',
    'Embedding cover artwork': 'Kapak görseli gömülüyor',
    'Cover artwork is not supported by this output format; the media was preserved.': 'Bu çıktı biçimi kapak gömmeyi desteklemiyor; medya korundu.',
    'No cover image was available; the media was preserved.': 'Kapak görseli bulunamadı; medya korundu.',
    'Cover artwork could not be embedded; the media was preserved.': 'Kapak görseli gömülemedi; medya korundu.',
    'File and folder naming': 'Dosya ve klasör adlandırma', 'Naming preset': 'Adlandırma şablonu',
    'Filename template': 'Dosya adı şablonu', 'Subfolder template': 'Alt klasör şablonu',
    'Title only': 'Yalnızca başlık', 'Channel / Title': 'Kanal / Başlık',
    'Channel / Date - Title': 'Kanal / Tarih - Başlık',
    'Playlist / Position - Title': 'Playlist / Sıra - Başlık', 'Custom': 'Özel',
    'Leave blank to use the download folder': 'İndirme klasörünü kullanmak için boş bırakın',
    'Example: {path}': 'Örnek: {path}',
    'Fields: {fields}. Use {index_example} for 001. The extension is added automatically. Folder templates may use / between folders.':
        'Alanlar: {fields}. 001 için {index_example} kullanın. Uzantı otomatik eklenir. Alt klasörler arasına / koyabilirsiniz.',
    'Invalid naming template braces.': 'Adlandırma şablonundaki süslü parantezler geçersiz.',
    'Unknown naming field or format. Use the fields shown below the templates.': 'Bilinmeyen adlandırma alanı veya biçimi. Şablonların altında gösterilen alanları kullanın.',
    'Naming templates contain invalid filename characters.': 'Adlandırma şablonunda dosya adı için geçersiz karakterler var.',
    'Invalid naming templates.': 'Geçersiz adlandırma şablonları.',
    'The filename template must be a name without folders or an extension field.': 'Dosya adı şablonu klasör veya uzantı alanı içermemelidir.',
    'The folder template must be a relative path without empty, dot or parent folders.': 'Alt klasör şablonu boş, nokta veya üst klasör bileşenleri içermeyen göreli bir yol olmalıdır.',
    'Invalid playlist naming context.': 'Geçersiz playlist adlandırma bilgisi.',
    'The output path is too long. Use a shorter folder template or download folder.': 'Çıktı yolu çok uzun. Daha kısa bir alt klasör şablonu veya indirme klasörü kullanın.',
    'Trim download mode': 'Kesit indirme yöntemi',
    'Download selected section only': 'Yalnızca seçili kesiti indir',
    'Download full source and trim': 'Tamamını indirip kırp',
    'Downloading selected section': 'Seçili kesit indiriliyor',
    'Section mode never falls back to a full download. FFmpeg section downloads do not support SOCKS proxies or speed limits.':
        'Kesit modu başarısız olduğunda tam indirmeye geçmez. FFmpeg ile kesit indirme SOCKS proxy ve hız sınırını desteklemez.',
    'Section downloads cannot enforce the speed limit. Remove the limit or select full download and trim.':
        'Kesit indirme hız sınırını uygulayamaz. Sınırı kaldırın veya tamamını indirip kırpmayı seçin.',
    'Section downloads do not support SOCKS proxies. Select full download and trim to use this proxy.':
        'Kesit indirme SOCKS proxy desteklemiyor. Bu proxy için tamamını indirip kırpmayı seçin.',
    'Trimming could not be applied; the downloaded section was saved': 'Son kırpma uygulanamadı; indirilen kesit kaydedildi',
    'Choose playlist videos…': 'Playlist videolarını seç…', 'Choose playlist videos': 'Playlist videolarını seç',
    'Video or playlist URLs': 'Video veya playlist bağlantıları',
    'Enter one playlist URL to choose which videos to download.': 'İndirilecek videoları seçmek için tek bir playlist bağlantısı girin.',
    'No downloads start until you queue the selected videos. Output settings are captured when this window opens.':
        'Seçili videoları kuyruğa ekleyene kadar indirme başlamaz. Bu pencere açılırken geçerli olan çıktı ayarları uygulanır.',
    'Select all': 'Tümünü seç', 'Select none': 'Seçimi temizle', 'Select range': 'Aralığı seç',
    'From': 'Başlangıç', 'To': 'Bitiş', 'Reload playlist': 'Playlisti yeniden yükle',
    'First playlist position': 'İlk playlist sırası', 'Last playlist position': 'Son playlist sırası',
    'Playlist videos': 'Playlist videoları', 'Playlist position': 'Playlist sırası', 'Video title': 'Video başlığı',
    'Queue selected videos': 'Seçili videoları kuyruğa ekle', 'Loading playlist videos…': 'Playlist videoları yükleniyor…',
    '{selected} of {total} videos selected.': '{total} videodan {selected} tanesi seçili.',
    'No downloadable playlist videos were found.': 'İndirilebilir playlist videosu bulunamadı.',
    'Private video': 'Gizli video', 'Deleted video': 'Silinmiş video', 'Untitled video': 'Başlıksız video',
    'The selected videos are already queued or active.': 'Seçilen videolar zaten kuyrukta veya indiriliyor.',
    '{added} selected videos queued; {skipped} already queued or repeated.': '{added} seçili video kuyruğa eklendi; {skipped} video zaten kuyrukta veya tekrarlanıyor.',
    'The playlist preview reached its entry limit. Only the displayed entries can be selected.':
        'Playlist önizleme sınırına ulaşıldı. Yalnızca gösterilen videolar seçilebilir.',
    'The playlist could not be fully read. Only the displayed entries can be selected.':
        'Playlistin tamamı okunamadı. Yalnızca gösterilen videolar seçilebilir.',
    'Source formats…': 'Kaynak formatları…', 'Source formats': 'Kaynak formatları',
    'Filter formats…': 'Formatları filtrele…', 'Filter formats': 'Formatları filtrele',
    'Refresh formats': 'Formatları yenile', 'Available source formats': 'Kullanılabilir kaynak formatları',
    'Queue selected formats': 'Seçili formatları kuyruğa ekle',
    'Video source': 'Video kaynağı', 'Audio source': 'Ses kaynağı',
    'Loading source formats…': 'Kaynak formatları yükleniyor…',
    'Could not load source formats.': 'Kaynak formatları yüklenemedi.',
    'Enter a single video URL to inspect its formats.': 'Formatlarını görmek için tek bir video bağlantısı girin.',
    'Choose the source streams to download. Output codec, container and trim settings still apply. Manual selection overrides the resolution limit.':
        'İndirilecek kaynak akışlarını seçin. Çıktı codec, kapsayıcı ve kırpma ayarları uygulanmaya devam eder. Elle seçim, çözünürlük sınırının yerine geçer.',
    'Format ID': 'Format kimliği', 'Stream type': 'Akış türü', 'Resolution': 'Çözünürlük',
    'Bitrate (kb/s)': 'Bit hızı (kb/sn)', 'Size': 'Boyut', 'HDR': 'HDR',
    'Audio channels': 'Ses kanalları', 'Sample rate (Hz)': 'Örnekleme hızı (Hz)',
    'Protocol': 'Protokol', 'Notes': 'Notlar', 'Availability': 'Kullanılabilirlik',
    'Video + audio': 'Video + ses', 'Video only': 'Yalnızca video', 'Audio only': 'Yalnızca ses', 'Other': 'Diğer',
    'Available': 'Kullanılabilir', 'Unavailable': 'Kullanılamıyor', 'DRM': 'DRM',
    'Included audio': 'Kaynağın içerdiği ses',
    'Unknown streams': 'Akış bilgisi bilinmiyor', 'Audio checked after download': 'Ses indirme sonrası kontrol edilir',
    'This format cannot be downloaded.': 'Bu format indirilemiyor.',
    'Use the source selectors below to choose compatible video and audio streams.': 'Uyumlu video ve ses akışlarını seçmek için aşağıdaki kaynak seçimlerini kullanın.',
    'Output: {mode} · {container} · {video} / {audio}': 'Çıktı: {mode} · {container} · {video} / {audio}',
    'Source selection ready. Sizes marked ≈ are estimates.': 'Kaynak seçimi hazır. ≈ işaretli boyutlar tahminidir.',
    'This URL is already queued or active.': 'Bu bağlantı zaten kuyrukta veya indiriliyor.',
    'Selected source formats added to the queue.': 'Seçilen kaynak formatları kuyruğa eklendi.',
    'Choose a source format first.': 'Önce bir kaynak formatı seçin.',
    'The selected source format is unavailable. Inspect the source formats again.': 'Seçilen kaynak formatı kullanılamıyor. Kaynak formatlarını yeniden kontrol edin.',
    'Choose an audio source for this video-only format.': 'Bu yalnızca video içeren format için bir ses kaynağı seçin.',
    'Network': 'Ağ', 'Network settings': 'Ağ ayarları', 'Proxy mode': 'Proxy modu', 'Proxy URL': 'Proxy adresi',
    'System proxy': 'Sistem proxy ayarı', 'Direct connection': 'Doğrudan bağlantı', 'Custom proxy': 'Özel proxy',
    'Unlimited': 'Sınırsız', 'Download speed limit': 'İndirme hız sınırı', 'Speed limit per task': 'Görev başına hız sınırı',
    'Applies to new tasks. Parallel video/audio share the speed limit. Zero means unlimited.': 'Yeni görevlere uygulanır. Paralel video/ses akışları hız sınırını paylaşır. Sıfır, sınırsız demektir.',
    'Enter a proxy URL with a scheme, host and port, such as http://127.0.0.1:8080.': 'http://127.0.0.1:8080 gibi protokol, sunucu ve port içeren bir proxy adresi girin.',
    'Invalid download speed limit.': 'Geçersiz indirme hız sınırı.', 'Invalid network settings.': 'Geçersiz ağ ayarları.',
    'When queue finishes': 'Kuyruk bitince', 'Do nothing': 'İşlem yapma', 'Open download folder': 'İndirme klasörünü aç',
    'Close application': 'Uygulamayı kapat', 'Sleep': 'Uyku', 'Hibernate': 'Hazırda beklet',
    'Shut down computer': 'Bilgisayarı kapat', 'After-download action': 'İndirme sonrası işlem',
    'After-download action cancelled.': 'İndirme sonrası işlem iptal edildi.',
    'Queue complete. {action} in {seconds} seconds.': 'Kuyruk tamamlandı. {seconds} saniye sonra: {action}.',
    'The after-download action could not be completed.': 'İndirme sonrası işlem tamamlanamadı.',
    'Runs only after all new tasks succeed. Power actions have a 30-second cancel countdown. Resets each session.': 'Yalnızca yeni görevlerin tümü başarılı olduğunda çalışır. Güç işlemleri 30 saniyelik iptal edilebilir geri sayımla başlar. Her oturumda sıfırlanır.',
    'Subtitles': 'Altyazılar', 'Subtitle settings': 'Altyazı ayarları', 'Subtitle output': 'Altyazı çıktısı',
    'No subtitles': 'Altyazı kullanma', 'Separate files': 'Ayrı dosyalar',
    'Embed in video': 'Videoya göm', 'Embed and save files': 'Göm ve ayrı dosya kaydet',
    'Subtitle languages': 'Altyazı dilleri', 'Subtitle format': 'Altyazı biçimi',
    'Include automatic subtitles': 'Otomatik altyazıları da indir',
    'tr,en or en.* or all': 'tr,en veya en.* veya all',
    'Selected subtitles are saved with the task. Missing languages are reported as warnings.': 'Altyazı seçimi görevle saklanır. Bulunamayan diller için uyarı gösterilir.',
    'Embedding supports MP4, MKV and WebM. Missing languages are reported as warnings.': 'Gömme için MP4, MKV ve WebM desteklenir. Bulunamayan diller için uyarı gösterilir.',
    'Audio downloads save subtitles as separate files.': 'Ses indirmelerinde altyazılar ayrı dosya olarak kaydedilir.',
    'Audio downloads support separate subtitle files only.': 'Ses indirmelerinde yalnızca ayrı altyazı dosyaları desteklenir.',
    'Enter subtitle language codes such as tr,en, en.* or all.': 'tr,en, en.* veya all gibi altyazı dil kodları girin.',
    'Invalid subtitle languages.': 'Geçersiz altyazı dilleri.',
    'Downloading subtitles': 'Altyazılar indiriliyor', 'Embedding subtitles': 'Altyazılar gömülüyor',
    'Cookies': 'Çerezler', 'Cookie settings': 'Çerez ayarları', 'Cookie source': 'Çerez kaynağı',
    'Do not use cookies': 'Çerez kullanma', 'Cookie file': 'Çerez dosyası',
    'Browser cookies': 'Tarayıcı çerezleri', 'Browser': 'Tarayıcı', 'Cookie browser': 'Çerez tarayıcısı',
    'Browser profile': 'Tarayıcı profili', 'Choose cookie file…': 'Çerez dosyası seç…',
    'Choose cookie file': 'Çerez dosyası seç', 'Path to cookies.txt': 'cookies.txt dosyasının yolu',
    'Cookie files (*.txt);;All files (*)': 'Çerez dosyaları (*.txt);;Tüm dosyalar (*)',
    'Default profile, or enter a profile name / path': 'Varsayılan profil veya profil adı / yolu',
    'Cookie selection applies to newly added tasks.': 'Çerez seçimi yeni eklenen görevlere uygulanır.',
    'Use a Netscape cookies.txt file. The original file is kept unchanged.': 'Netscape biçiminde bir cookies.txt dosyası kullanın. Özgün dosya değiştirilmez.',
    'Leave the profile blank for the default. Close the browser if its cookies cannot be read.': 'Varsayılan profil için boş bırakın. Çerezler okunamazsa tarayıcıyı kapatın.',
    'Choose a Netscape cookies.txt file first.': 'Önce Netscape biçiminde bir cookies.txt dosyası seçin.',
    'The selected cookie file does not exist.': 'Seçilen çerez dosyası mevcut değil.',
    'Queue warning': 'Kuyruk uyarısı',
    'Saved queue restored.': 'Kaydedilen kuyruk geri yüklendi.',
    'Saved queue restored. Press Resume to restart unfinished downloads.': 'Kaydedilen kuyruk geri yüklendi. Yarım kalan indirmeleri yeniden başlatmak için Devam et’e basın.',
    'The saved queue could not be loaded. The original file will not be overwritten.': 'Kaydedilen kuyruk yüklenemedi. Özgün dosyanın üzerine yazılmayacak.',
    'The saved queue was recovered. See details for the original file.': 'Kaydedilen kuyruk kurtarıldı. Özgün dosya için ayrıntılara bakın.',
    'Your queue could not be saved. Recent changes may not be restored next time.': 'Kuyruk kaydedilemedi. Son değişiklikler bir sonraki açılışta geri yüklenemeyebilir.',
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
    'BekoDLP {version}\n\nAn independent video and audio downloader for YouTube and other sites supported by yt-dlp.\nInterface by PySide6. Media processing by FFmpeg.\nNot affiliated with the yt-dlp project.\n\nThis project was developed with AI using Claude and ChatGPT.':
        'BekoDLP {version}\n\nYouTube başta olmak üzere yt-dlp tarafından desteklenen siteler için bağımsız video ve ses indirici.\nArayüz: PySide6. Medya işleme: FFmpeg.\nyt-dlp projesiyle bağlantılı değildir.\n\nBu proje, Claude ve ChatGPT kullanılarak yapay zekâ ile geliştirilmiştir.',
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
    if combo.property('_i18n_user_text'):
        return
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
