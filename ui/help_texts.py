"""Plain-language field help, paired as English and Turkish."""

FIELD_HELP = {
    'Mode': (
        'Video saves the picture and sound. Audio saves only the sound.\n\nChoose Audio for music or speech without a video track.',
        'Video, görüntü ve sesi kaydeder. Ses, yalnızca sesi kaydeder.\n\nGörüntü olmadan müzik veya konuşma indirmek için Ses seçin.'),
    'Resolution limit': (
        'The highest video resolution to select, such as 1080p.\n\nA lower resolution usually needs less data and disk space. The source must offer the chosen resolution; this setting does not upscale a video.',
        'Seçilecek en yüksek video çözünürlüğüdür; örneğin 1080p.\n\nDaha düşük çözünürlük genellikle daha az internet ve disk alanı kullanır. Kaynakta bu çözünürlük bulunmalıdır; bu ayar görüntüyü büyütmez.'),
    'Audio format': (
        'Select the type of audio file to save. The codec and container are set together.\n\nOriginal keeps the source audio codec and bitrate. MP3, AAC or Opus may require conversion. WAV is uncompressed; FLAC uses lossless compression.',
        'Kaydedilecek ses dosyasının türüdür. Codec ve kapsayıcı birlikte ayarlanır.\n\nOrijinal, kaynak ses codec’ini ve bit hızını korur. MP3, AAC veya Opus dönüştürme gerektirebilir. WAV sıkıştırılmamıştır; FLAC kayıpsız sıkıştırma kullanır.'),
    'Container': (
        'The file type that holds the video and audio streams, such as MP4 or MKV.\n\nChanging the container alone does not improve quality. Some codecs do not fit every container. Automatic chooses a compatible one.',
        'Video ve ses akışlarını bir arada tutan dosya türüdür; örneğin MP4 veya MKV.\n\nYalnızca kapsayıcıyı değiştirmek kaliteyi artırmaz. Her codec her kapsayıcıya uymaz. Otomatik, uyumlu olanı seçer.'),
    'Video codec': (
        'The method used to compress the picture, such as H.264 or AV1.\n\nOriginal copies the source without re-encoding. A different codec can take more time and CPU and may lose quality. Matching codecs are copied.',
        'Görüntüyü sıkıştırmak için kullanılan yöntemdir; örneğin H.264 veya AV1.\n\nOrijinal, kaynağı yeniden kodlamadan kopyalar. Farklı bir codec daha fazla süre ve işlemci gerektirebilir, kalite kaybına yol açabilir. Eşleşen codec’ler kopyalanır.'),
    'Audio codec': (
        'The method used to store or compress the sound.\n\nOriginal keeps the source codec and bitrate. Choosing a different codec can require conversion. No audio saves the video without sound.',
        'Sesi kaydetmek veya sıkıştırmak için kullanılan yöntemdir.\n\nOrijinal, kaynak codec’i ve bit hızını korur. Farklı bir codec seçmek dönüştürme gerektirebilir. Ses yok seçimi, videoyu sessiz kaydeder.'),
    'Conversion quality': (
        'The balance between picture quality and file size when video is re-encoded.\n\nHigh quality usually creates a larger file. Balanced is a useful starting point. This setting has no effect when video is copied.',
        'Video yeniden kodlanırken görüntü kalitesi ile dosya boyutu arasındaki dengedir.\n\nYüksek kalite genellikle daha büyük dosya üretir. Başlangıç için Dengeli kullanılabilir. Video kopyalanıyorsa bu ayar uygulanmaz.'),
    'Conversion bitrate': (
        'The target amount of audio data per second when sound is converted.\n\nAutomatic uses 192k for MP3/AAC and 128k for Opus. Higher values usually make larger files but cannot restore lost quality. Copied audio keeps its source bitrate.',
        'Ses dönüştürülürken saniye başına hedeflenen ses verisi miktarıdır.\n\nOtomatik, MP3/AAC için 192k ve Opus için 128k kullanır. Yüksek değerler genellikle dosyayı büyütür; kaybolan kaliteyi geri getirmez. Kopyalanan ses, kaynak bit hızını korur.'),
    'Bit depth': (
        'The precision of each audio sample, for FLAC and WAV/LPCM output.\n\nOriginal preserves known source precision; if it is unknown, 24-bit is used. Increasing bit depth can increase file size but does not recover missing detail.',
        'FLAC ve WAV/LPCM çıktılarında her ses örneğinin hassasiyetidir.\n\nOrijinal, bilinen kaynak bit derinliğini korur; bilinmiyorsa 24 bit kullanılır. Bit derinliğini artırmak dosyayı büyütebilir, eksik ayrıntıları geri getirmez.'),
    'Parallel downloads': (
        'How many download tasks can run at the same time. Each task also includes processing and saving.\n\nMore tasks can use more RAM and share your connection. Increasing this number does not always make the whole queue finish sooner.',
        'Aynı anda kaç indirme görevinin çalışabileceğidir. Her görev işleme ve kaydetme aşamalarını da kapsar.\n\nDaha fazla görev RAM kullanımını artırabilir ve internet bağlantısını paylaşır. Sayıyı artırmak kuyruğu her zaman daha hızlı bitirmez.'),
    'Parallel video conversions': (
        'How many videos can be re-encoded at once, within the overall task limit.\n\nKeeping this at 1 limits simultaneous heavy CPU/RAM use. Stream copying and audio-only conversion are managed separately.',
        'Genel görev sınırı içinde aynı anda kaç videonun yeniden kodlanabileceğidir.\n\n1 olarak bırakmak, ağır işlemci ve RAM kullanımının aynı anda artmasını sınırlar. Akış kopyalama ve yalnızca ses dönüştürme ayrı yönetilir.'),
    'FFmpeg threads': (
        'How much work the CPU can do at the same time while processing video or audio.\n\nAutomatic adjusts the thread setting. Larger values can use more CPU and memory. This is not a CPU percentage limit or a strict limit on all threads in a process.',
        'Video ve ses işlenirken işlemcinin ne kadar işi aynı anda yürütebileceğini belirler.\n\nOtomatik, iş parçacığı ayarını düzenler. Yüksek değerler daha fazla işlemci ve bellek kullanabilir. Bu ayar bir işlemci yüzdesi veya tüm iş parçacıkları için kesin sınır değildir.'),
    'Parallel fragments': (
        'How many parts of a segmented download can be fetched at once. It applies to sources such as DASH/HLS, not every video.\n\nSome sources benefit; others do not. The shared media budget can reduce the effective number.',
        'Parçalı bir indirmede aynı anda kaç parçanın alınabileceğidir. DASH/HLS gibi kaynaklarda geçerlidir; her videoda kullanılmaz.\n\nBazı kaynaklarda hız kazandırır, bazılarında kazandırmaz. Ortak medya bütçesi kullanılan sayıyı azaltabilir.'),
    'Media download budget': (
        'The connection capacity shared by media downloads and their fragment requests. When it is used up, new downloads wait.\n\nLarger values can increase network use. Video information and previews have separate limits; this is not a hard cap on every network connection.',
        'Medya indirmeleri ve parça isteklerinin paylaşacağı bağlantı kapasitesidir. Kapasite dolunca yeni indirmeler bekler.\n\nYüksek değerler ağ kullanımını artırabilir. Video bilgileri ve önizlemeler ayrı sınırlanır; bu ayar tüm ağ bağlantıları için kesin bir üst sınır değildir.'),
    'Trim start / end': (
        'Save a selected time range. Enter seconds, mm:ss or hh:mm:ss; leave blank for the full range. Section mode requests the range with FFmpeg and never falls back to a full download. Actual traffic depends on source seeking support. Full mode downloads the source first; it also supports speed limits and SOCKS proxies.\n\nWith Original video, the start may move to an earlier keyframe to keep sound and picture together. Exact cuts require video re-encoding.',
        'Seçilen zaman aralığını kaydeder. Saniye, mm:ss veya hh:mm:ss girin; tüm aralık için boş bırakın. Kesit modu aralığı FFmpeg ile ister ve tam indirmeye kendiliğinden geçmez. Veri kullanımı kaynağın ileri sarma desteğine bağlıdır. Tam mod önce kaynağı indirir; hız sınırı ve SOCKS proxy de desteklenir.\n\nOrijinal videoda ses ve görüntünün birlikte başlaması için başlangıç daha önceki anahtar kareye alınabilir. Kesin kesim için video yeniden kodlanmalıdır.'),
}
