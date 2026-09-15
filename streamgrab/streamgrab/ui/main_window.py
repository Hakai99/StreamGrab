"""
StreamGrab main window: two fully independent tabs — Video (MP4) and
Audio (MP3) — each with its own URL input, Fetch button, quality
dropdown, and Download button. Keeping them independent means pasting
a link in one tab can never interfere with a fetch/download in the
other, and each tab's own error/progress state stays local to it.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QUrl
from PyQt6.QtGui import QIcon, QPixmap
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkRequest
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QComboBox, QProgressBar, QFileDialog, QMessageBox,
    QStatusBar, QTabWidget,
)

from streamgrab.core.downloader import (
    fetch_video_info, download, VideoInfo, is_ffmpeg_available,
)

LOGO_PATH = Path(__file__).resolve().parent.parent / "resources" / "logo.png"

DARK_STYLESHEET = """
QMainWindow, QWidget { background-color: #1e1f26; color: #e8e8ec; font-family: 'Segoe UI', sans-serif; }
QLineEdit, QComboBox { background-color: #262832; border: 1px solid #34364a; border-radius: 6px; padding: 8px; font-size: 13px; }
QPushButton { background-color: #34364a; border: 1px solid #454864; border-radius: 6px; padding: 8px 16px; font-weight: 600; }
QPushButton:hover { background-color: #454864; }
QPushButton:disabled { background-color: #2a2b34; color: #666; }
QPushButton#fetchBtn { background-color: #2255aa; border: 1px solid #3366cc; }
QPushButton#fetchBtn:hover { background-color: #3366cc; }
QPushButton#downloadBtn { background-color: #1f7a4d; border: 1px solid #2ea86b; }
QPushButton#downloadBtn:hover { background-color: #2ea86b; }
QPushButton#downloadBtn:disabled { background-color: #2a2b34; color: #666; }
QLabel#titleLabel { font-size: 20px; font-weight: 700; }
QLabel#videoTitle { font-size: 14px; color: #9aa0c0; }
QLabel#warningLabel { color: #ffb84d; font-size: 12px; }
QProgressBar { background-color: #262832; border: 1px solid #34364a; border-radius: 6px; text-align: center; height: 24px; }
QProgressBar::chunk { background-color: #2ea86b; border-radius: 6px; }
QStatusBar { background-color: #262832; }
QTabWidget::pane { border: 1px solid #34364a; border-radius: 6px; top: -1px; }
QTabBar::tab { background-color: #262832; padding: 8px 20px; border: 1px solid #34364a; border-bottom: none; border-top-left-radius: 6px; border-top-right-radius: 6px; margin-right: 2px; }
QTabBar::tab:selected { background-color: #34364a; font-weight: 600; }
"""


class FetchInfoThread(QThread):
    """Runs the (potentially slow) yt-dlp metadata fetch off the UI
    thread so the window can never appear frozen — fetch_video_info()
    itself now also enforces its own hard timeout internally, so even
    a stuck extractor eventually reports back rather than hanging."""
    finished_ok = pyqtSignal(object)  # VideoInfo
    failed = pyqtSignal(str)

    def __init__(self, url: str):
        super().__init__()
        self.url = url

    def run(self):
        try:
            info = fetch_video_info(self.url)
            self.finished_ok.emit(info)
        except Exception as e:
            self.failed.emit(str(e))


class DownloadThread(QThread):
    """Runs the actual download off the UI thread, forwarding yt-dlp's
    progress dicts back to the GUI via a signal."""
    progress = pyqtSignal(dict)
    finished_ok = pyqtSignal(str)  # resulting file path
    failed = pyqtSignal(str)

    def __init__(self, url, format_id, output_dir, filename, audio_only):
        super().__init__()
        self.url = url
        self.format_id = format_id
        self.output_dir = output_dir
        self.filename = filename
        self.audio_only = audio_only

    def run(self):
        try:
            result_path = download(
                self.url,
                self.format_id,
                self.output_dir,
                self.filename,
                audio_only=self.audio_only,
                progress_callback=lambda d: self.progress.emit(d),
            )
            self.finished_ok.emit(str(result_path))
        except Exception as e:
            self.failed.emit(str(e))


class DownloadTab(QWidget):
    """One self-contained tab: its own URL bar, Fetch button, quality
    dropdown, folder picker, Download button, and progress bar. Used
    twice (once for video, once for audio) with completely separate
    state, so nothing in one tab can affect the other."""

    def __init__(self, *, audio_only: bool, ffmpeg_available: bool, parent=None):
        super().__init__(parent)
        self.audio_only = audio_only
        self.ffmpeg_available = ffmpeg_available

        self.video_info: VideoInfo | None = None
        self.output_dir: Path = Path.home() / "Downloads"
        self.fetch_thread: FetchInfoThread | None = None
        self.download_thread: DownloadThread | None = None

        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # URL row — this tab's own independent input, never shared.
        url_row = QHBoxLayout()
        self.url_input = QLineEdit()
        placeholder = (
            "Paste a video URL to extract audio from..."
            if self.audio_only else
            "Paste a video URL here..."
        )
        self.url_input.setPlaceholderText(placeholder)
        url_row.addWidget(self.url_input, stretch=1)
        self.fetch_btn = QPushButton("Fetch")
        self.fetch_btn.setObjectName("fetchBtn")
        self.fetch_btn.clicked.connect(self._on_fetch_clicked)
        url_row.addWidget(self.fetch_btn)
        layout.addLayout(url_row)

        self.title_label = QLabel("")
        self.title_label.setObjectName("videoTitle")
        self.title_label.setWordWrap(True)
        layout.addWidget(self.title_label)

        # Thumbnail preview so the user can visually confirm this is
        # the right video before downloading. Loaded async via
        # QNetworkAccessManager — works for any site's thumbnail URL,
        # since it's just an image fetch, not site-specific code.
        self.thumbnail_label = QLabel("")
        self.thumbnail_label.setFixedHeight(140)
        self.thumbnail_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumbnail_label.setVisible(False)
        layout.addWidget(self.thumbnail_label)
        self._network_manager = QNetworkAccessManager(self)

        # Quality row
        quality_row = QHBoxLayout()
        quality_row.addWidget(QLabel("Audio quality:" if self.audio_only else "Resolution:"))
        self.format_combo = QComboBox()
        self.format_combo.setEnabled(False)
        quality_row.addWidget(self.format_combo, stretch=1)
        layout.addLayout(quality_row)

        self.ffmpeg_warning_label = QLabel("")
        self.ffmpeg_warning_label.setObjectName("warningLabel")
        self.ffmpeg_warning_label.setWordWrap(True)
        layout.addWidget(self.ffmpeg_warning_label)

        if self.audio_only and not self.ffmpeg_available:
            self.ffmpeg_warning_label.setText(
                "⚠ ffmpeg was not found on this system. MP3 conversion "
                "requires it — install ffmpeg and add it to your PATH "
                "to enable audio downloads."
            )
            self.url_input.setEnabled(False)
            self.fetch_btn.setEnabled(False)

        # Output folder row
        folder_row = QHBoxLayout()
        self.folder_label = QLabel(f"Save to: {self.output_dir}")
        self.folder_label.setWordWrap(True)
        folder_row.addWidget(self.folder_label, stretch=1)
        choose_folder_btn = QPushButton("Choose Folder")
        choose_folder_btn.clicked.connect(self._choose_folder)
        folder_row.addWidget(choose_folder_btn)
        layout.addLayout(folder_row)

        # Download button
        self.download_btn = QPushButton("Download")
        self.download_btn.setObjectName("downloadBtn")
        self.download_btn.setEnabled(False)
        self.download_btn.clicked.connect(self._on_download_clicked)
        layout.addWidget(self.download_btn)

        # Progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        layout.addStretch()

    # ---- fetch flow -----------------------------------------------------

    def _on_fetch_clicked(self):
        url = self.url_input.text().strip()
        if not url:
            QMessageBox.warning(self, "No URL", "Please paste a video URL first.")
            return

        self.fetch_btn.setEnabled(False)
        self.fetch_btn.setText("Fetching...")
        self.format_combo.clear()
        self.format_combo.setEnabled(False)
        self.download_btn.setEnabled(False)
        self.title_label.setText("")
        self.thumbnail_label.setVisible(False)
        self.window().statusBar().showMessage("Looking up video info...")

        self.fetch_thread = FetchInfoThread(url)
        self.fetch_thread.finished_ok.connect(self._on_fetch_success)
        self.fetch_thread.failed.connect(self._on_fetch_failed)
        self.fetch_thread.start()

    def _on_fetch_success(self, info: VideoInfo):
        self.video_info = info
        icon = "🎵" if self.audio_only else "🎬"
        self.title_label.setText(f"{icon} {info.title}")

        if info.thumbnail_url:
            reply = self._network_manager.get(QNetworkRequest(QUrl(info.thumbnail_url)))
            reply.finished.connect(lambda r=reply: self._on_thumbnail_loaded(r))
        else:
            self.thumbnail_label.setVisible(False)

        formats = info.audio_formats if self.audio_only else info.video_formats
        self.format_combo.clear()
        for fmt in formats:
            self.format_combo.addItem(fmt.label, userData=fmt)
        self.format_combo.setEnabled(bool(formats))
        self.download_btn.setEnabled(bool(formats))

        self.fetch_btn.setEnabled(True)
        self.fetch_btn.setText("Fetch")

        if not formats:
            kind = "audio" if self.audio_only else "video"
            self.window().statusBar().showMessage(f"No {kind} formats found for this link.")
        else:
            self.window().statusBar().showMessage(f"Found {len(formats)} format(s).")

    def _on_fetch_failed(self, error_msg: str):
        self.fetch_btn.setEnabled(True)
        self.fetch_btn.setText("Fetch")
        self.window().statusBar().showMessage("Failed to fetch video info.")
        QMessageBox.critical(self, "Fetch failed", f"Could not read this URL:\n\n{error_msg}")

    def _on_thumbnail_loaded(self, reply):
        # Silently skip on failure (no thumbnail, wrong format, network
        # hiccup) — the thumbnail is a nice-to-have, never a blocker.
        data = reply.readAll()
        reply.deleteLater()
        pixmap = QPixmap()
        if pixmap.loadFromData(bytes(data)):
            scaled = pixmap.scaledToHeight(140, Qt.TransformationMode.SmoothTransformation)
            self.thumbnail_label.setPixmap(scaled)
            self.thumbnail_label.setVisible(True)

    # ---- folder selection -------------------------------------------------

    def _choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Choose download folder", str(self.output_dir))
        if folder:
            self.output_dir = Path(folder)
            self.folder_label.setText(f"Save to: {self.output_dir}")

    # ---- download flow ----------------------------------------------------

    def _on_download_clicked(self):
        if self.video_info is None:
            return
        fmt = self.format_combo.currentData()
        if fmt is None:
            QMessageBox.warning(
                self, "No format selected",
                "Please fetch a video first and choose a quality option."
            )
            return

        if self.audio_only and not self.ffmpeg_available:
            QMessageBox.warning(
                self, "ffmpeg required",
                "MP3 conversion requires ffmpeg, which isn't installed. "
                "Install ffmpeg and add it to your PATH, then try again."
            )
            return

        url = self.url_input.text().strip()
        self.download_btn.setEnabled(False)
        self.download_btn.setText("Downloading...")
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)

        self.download_thread = DownloadThread(
            url=url,
            format_id=fmt.download_format_string,
            output_dir=self.output_dir,
            filename=self.video_info.title,
            audio_only=self.audio_only,
        )
        self.download_thread.progress.connect(self._on_download_progress)
        self.download_thread.finished_ok.connect(self._on_download_success)
        self.download_thread.failed.connect(self._on_download_failed)
        self.download_thread.start()

    def _on_download_progress(self, progress_dict: dict):
        status = progress_dict.get("status")
        if status == "downloading":
            total = progress_dict.get("total_bytes") or progress_dict.get("total_bytes_estimate")
            downloaded = progress_dict.get("downloaded_bytes", 0)
            if total:
                percent = int((downloaded / total) * 100)
                self.progress_bar.setValue(percent)
        elif status == "finished":
            self.progress_bar.setValue(100)

    def _on_download_success(self, file_path: str):
        self.download_btn.setEnabled(True)
        self.download_btn.setText("Download")
        self.window().statusBar().showMessage(f"✓ Saved to {file_path}")
        QMessageBox.information(self, "Download complete", f"Saved to:\n{file_path}")

    def _on_download_failed(self, error_msg: str):
        self.download_btn.setEnabled(True)
        self.download_btn.setText("Download")
        self.progress_bar.setVisible(False)
        self.window().statusBar().showMessage("Download failed.")
        QMessageBox.critical(self, "Download failed", error_msg)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("StreamGrab — video & audio downloader")
        self.resize(760, 520)
        self.setStyleSheet(DARK_STYLESHEET)
        if LOGO_PATH.exists():
            self.setWindowIcon(QIcon(str(LOGO_PATH)))

        self.ffmpeg_available = is_ffmpeg_available()

        self._build_ui()

    def _build_ui(self):
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setSpacing(14)
        layout.setContentsMargins(24, 24, 24, 24)

        title = QLabel("StreamGrab")
        title.setObjectName("titleLabel")
        layout.addWidget(title)

        subtitle = QLabel(
            "Paste a link from YouTube, Instagram, Facebook, or almost "
            "any other site. Each tab has its own URL box, so video and "
            "audio downloads never interfere with each other."
        )
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        self.tabs = QTabWidget()
        self.video_tab = DownloadTab(audio_only=False, ffmpeg_available=self.ffmpeg_available)
        self.audio_tab = DownloadTab(audio_only=True, ffmpeg_available=self.ffmpeg_available)
        self.tabs.addTab(self.video_tab, "🎬 Video (MP4)")
        self.tabs.addTab(self.audio_tab, "🎵 Audio (MP3)")
        layout.addWidget(self.tabs)

        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())
