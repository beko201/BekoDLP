"""Shared application colors and widget styles."""
from PySide6.QtGui import QColor, QFont, QPalette


def apply_theme(app):
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 10))
    palette = QPalette()
    colors = {
        QPalette.ColorRole.Window: "#20242c",
        QPalette.ColorRole.WindowText: "#eef1f6",
        QPalette.ColorRole.Base: "#171b22",
        QPalette.ColorRole.AlternateBase: "#242a34",
        QPalette.ColorRole.Text: "#eef1f6",
        QPalette.ColorRole.Button: "#303846",
        QPalette.ColorRole.ButtonText: "#eef1f6",
        QPalette.ColorRole.Highlight: "#356bc4",
        QPalette.ColorRole.HighlightedText: "#ffffff",
        QPalette.ColorRole.ToolTipBase: "#303846",
        QPalette.ColorRole.ToolTipText: "#eef1f6",
        QPalette.ColorRole.PlaceholderText: "#99a5b6",
    }
    for role, color in colors.items():
        palette.setColor(role, QColor(color))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText,
                 QPalette.ColorRole.WindowText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor("#778294"))
    app.setPalette(palette)
    app.setStyleSheet("""
        QWidget { font-size: 10pt; }
        QGroupBox { font-weight: bold; margin-top: 12px; padding-top: 14px; }
        QGroupBox::title { subcontrol-origin: margin; left: 10px; }
        QLineEdit, QComboBox, QSpinBox { min-height: 28px; }
        QPushButton { min-height: 30px; padding: 0 12px; }
        QToolButton { min-height: 28px; padding: 0 10px; }
        QToolButton#fieldHelpButton {
            min-height: 20px; max-height: 20px; min-width: 20px; max-width: 20px;
            padding: 0; border: 1px solid transparent; border-radius: 10px; color: #a8c4ed;
        }
        QToolButton#fieldHelpButton:hover { background: #303846; color: #ffffff; }
        QToolButton#fieldHelpButton:focus { border-color: #82acf0; }
        QPushButton#primaryButton:enabled { background: #356bc4; color: white; }
        QPushButton#primaryButton:hover { background: #407bdc; }
        QMenuBar { padding: 3px; }
        QMenuBar::item:selected, QMenu::item:selected { background: #356bc4; }
        QLabel#mutedLabel { color: #aab5c6; }
        QLabel#headingLabel { font-size: 20pt; font-weight: bold; }
        QTableView { border: 1px solid #364050; gridline-color: #303846; }
        QHeaderView::section { padding: 8px; font-weight: bold; }
        QProgressBar { min-height: 16px; text-align: center; }
    """)
