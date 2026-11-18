"""Collect desktop image support without unused PDF/virtual-keyboard runtimes."""
from pathlib import Path
from PyInstaller.utils.hooks.qt import add_qt6_dependencies

hiddenimports, binaries, datas = add_qt6_dependencies(__file__)
# These plugins are not used by this desktop downloader. They otherwise pull
# Qt PDF/Quick/QML/VirtualKeyboard and their separate third-party code into EXEs.
excluded = {'qpdf.dll', 'qtvirtualkeyboardplugin.dll'}
binaries = [(source, target) for source, target in binaries if Path(source).name.lower() not in excluded]
