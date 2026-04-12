# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import sys
import traceback
from datetime import datetime

from PyQt6.QtCore import QLocale
from PyQt6.QtWidgets import QApplication, QMessageBox

from core.paths import APP_NAME, ensure_runtime_dirs, get_logs_dir
from core.runtime_installer import AppRuntimeInstaller
from ui.main_window import MainWindow
from ui.runtime_setup_dialog import RuntimeSetupDialog


def configure_logging() -> None:
    logs_dir = get_logs_dir()
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"ytuploader-{datetime.now():%Y%m%d}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    logging.getLogger(__name__).info("Logging initialized at %s", log_path)


def install_exception_hook() -> None:
    def handle_exception(exc_type, exc_value, exc_traceback) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return

        traceback_text = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        logging.critical("Unhandled exception\n%s", traceback_text)
        QMessageBox.critical(None, "처리되지 않은 오류", str(exc_value))

    sys.excepthook = handle_exception


def apply_korean_locale() -> None:
    locale = QLocale(QLocale.Language.Korean, QLocale.Country.SouthKorea)
    QLocale.setDefault(locale)


def main() -> int:
    ensure_runtime_dirs()
    configure_logging()

    apply_korean_locale()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    install_exception_hook()

    runtime_installer = AppRuntimeInstaller()
    if not runtime_installer.is_ready():
        dialog = RuntimeSetupDialog(runtime_installer=runtime_installer)
        if dialog.exec() != RuntimeSetupDialog.DialogCode.Accepted:
            return 0

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
