"""Field explanations must be accessible without changing download options."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import unittest
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWhatsThis

from ui.i18n import set_language
from ui.output_controls import OutputControls


class FieldHelpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        set_language('en')
        self.controls = OutputControls()
        self.controls.resize(1100, 500)
        self.controls.advanced_toggle.setChecked(True)
        self.controls.show()
        self.app.processEvents()

    def tearDown(self):
        QWhatsThis.hideText()
        self.controls.close()
        set_language('en')
        self.app.processEvents()

    def test_mouse_and_keyboard_help_are_localized_and_do_not_change_options(self):
        controls = self.controls
        before = controls.values()
        changes = []
        controls.changed.connect(lambda: changes.append(True))
        button = controls.help_buttons['Conversion quality']
        self.assertFalse(controls.video_quality_combo.isEnabled())
        self.assertTrue(button.isEnabled())
        button.setFocus()
        QTest.keyClick(button, Qt.Key.Key_Space)
        popup = self.app.activePopupWidget()
        self.assertIsNotNone(popup)
        QTest.keyClick(popup, Qt.Key.Key_Escape)
        self.assertIsNone(self.app.activePopupWidget())
        set_language('tr')
        button = controls.help_buttons['FFmpeg threads']
        self.assertEqual(button.accessibleName(), 'FFmpeg iş parçacıkları hakkında')
        self.assertIn('Otomatik', button.toolTip())
        with patch('ui.field_help.QWhatsThis.showText') as show:
            QTest.mouseClick(button, Qt.MouseButton.LeftButton)
            self.assertIn('FFmpeg iş parçacıkları', show.call_args.args[1])
            self.assertIn('işlemci', show.call_args.args[1])
        self.assertEqual(controls.values(), before)
        self.assertEqual(changes, [])

    def test_audio_mode_help_and_lossless_header_follow_the_visible_field(self):
        controls = self.controls
        controls.format_combo.setCurrentIndex(controls.format_combo.findData('MP3'))
        set_language('tr')
        button = controls.help_buttons['Resolution limit']
        self.assertEqual(button.topic, 'Audio format')
        self.assertEqual(button.accessibleName(), 'Ses biçimi hakkında')
        self.assertIn('kaynak ses', controls.audio_format_combo.accessibleDescription())
        controls.audio_format_combo.setCurrentIndex(controls.audio_format_combo.findData('flac'))
        self.assertTrue(controls.help_buttons['Bit depth'].isVisible())
        controls.audio_format_combo.setCurrentIndex(controls.audio_format_combo.findData('mp3'))
        self.assertFalse(controls.help_buttons['Bit depth'].isVisible())
        set_language('en')
        self.assertEqual(button.accessibleName(), 'About Audio format')
