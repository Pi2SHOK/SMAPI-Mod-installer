import os
import re
import shutil
import stat
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from PySide6.QtCore import QSettings, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


def is_game_directory(path):
    return (Path(path) / "Stardew Valley.exe").is_file()


def find_mods_directories(game_directory):
    game_directory = Path(game_directory)
    return sorted(
        (
            path
            for path in game_directory.iterdir()
            if path.is_dir()
            and not path.is_symlink()
            and (
                path.name.casefold() == "mods"
                or "mods_" in path.name.casefold()
            )
        ),
        key=lambda path: path.name.casefold(),
    )


def find_game_installations():
    steam_roots = set()
    for variable in ("ProgramFiles(x86)", "ProgramFiles"):
        program_files = os.environ.get(variable)
        if program_files:
            steam_roots.add(Path(program_files) / "Steam")

    steam_roots.update(
        {
            Path("C:/Steam"),
            Path("D:/Steam"),
            Path.home() / "Steam",
        }
    )

    candidates = set()
    for steam_root in steam_roots:
        candidates.add(steam_root / "steamapps/common/Stardew Valley")
        library_file = steam_root / "steamapps/libraryfolders.vdf"
        try:
            contents = library_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue

        for library_path in re.findall(r'"path"\s+"((?:\\\\.|[^"])*)"', contents):
            library_path = library_path.replace("\\\\", "\\")
            candidates.add(
                Path(library_path) / "steamapps/common/Stardew Valley"
            )

    return sorted(
        (path for path in candidates if is_game_directory(path)),
        key=lambda path: str(path).casefold(),
    )


def find_mod_roots(directory):
    directory = Path(directory)
    if directory.is_symlink():
        raise ValueError(f"Символические ссылки не поддерживаются: {directory}")

    mod_roots = set()

    def raise_walk_error(error):
        raise error

    for current, directories, files in os.walk(
        directory, topdown=True, followlinks=False, onerror=raise_walk_error
    ):
        current_path = Path(current)
        for name in list(directories):
            child = current_path / name
            if child.is_symlink():
                raise ValueError(f"Символические ссылки не поддерживаются: {child}")
        directories[:] = sorted(directories, key=str.casefold)

        if "manifest.json" in files:
            manifest = current_path / "manifest.json"
            if manifest.is_symlink():
                raise ValueError(
                    f"Символические ссылки не поддерживаются: {manifest}"
                )
            mod_roots.add(current_path)

        for name in files:
            child = current_path / name
            if child.is_symlink():
                raise ValueError(f"Символические ссылки не поддерживаются: {child}")

    return sorted(mod_roots, key=lambda path: str(path).casefold())


def extract_zip_safely(archive_path, destination):
    destination = Path(destination)
    with zipfile.ZipFile(archive_path) as archive:
        for entry in archive.infolist():
            raw_name = entry.filename.replace("\\", "/")
            archive_path = PurePosixPath(raw_name)
            parts = archive_path.parts
            if (
                archive_path.is_absolute()
                or not parts
                or ".." in parts
                or ":" in parts[0]
            ):
                raise ValueError(
                    f"Архив содержит недопустимый путь: {entry.filename}"
                )

            mode = entry.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ValueError(
                    f"Архив содержит символическую ссылку: {entry.filename}"
                )

            target = destination.joinpath(*parts)
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(entry) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)


def safe_mod_name(name):
    invalid_characters = '<>:"|?*'
    reserved_names = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }
    if (
        not name
        or name in {".", ".."}
        or name.endswith((" ", "."))
        or any(character in invalid_characters for character in name)
        or name.split(".")[0].upper() in reserved_names
    ):
        raise ValueError(f"Недопустимое имя папки мода: {name!r}")
    return name


def list_files(directory):
    files = []

    def raise_walk_error(error):
        raise error

    for current, directories, names in os.walk(
        directory, topdown=True, followlinks=False, onerror=raise_walk_error
    ):
        current_path = Path(current)
        for name in list(directories):
            child = current_path / name
            if child.is_symlink():
                raise ValueError(f"Символические ссылки не поддерживаются: {child}")
        directories[:] = sorted(directories, key=str.casefold)
        for name in sorted(names, key=str.casefold):
            file_path = current_path / name
            if file_path.is_symlink():
                raise ValueError(
                    f"Символические ссылки не поддерживаются: {file_path}"
                )
            files.append(file_path)
    return files


def copy_mod(source, destination, files, progress_callback, completed, total):
    destination.mkdir(parents=True)
    for source_file in files:
        relative_path = source_file.relative_to(source)
        target_file = destination / relative_path
        target_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, target_file)
        completed += 1
        progress_callback(
            5 + int(90 * completed / max(total, 1)),
            f"Копирование: {source.name}",
        )


class InstallWorker(QThread):
    progress = Signal(int, str)
    succeeded = Signal(str)
    failed = Signal(str)

    def __init__(self, sources, mods_directories):
        super().__init__()
        self.sources = [Path(source) for source in sources]
        self.mods_directories = [Path(path) for path in mods_directories]

    def run(self):
        try:
            cleanup_warning = self.install_mods()
        except Exception as error:
            self.failed.emit(str(error))
        else:
            self.succeeded.emit(cleanup_warning)

    def install_mods(self):
        if not self.mods_directories:
            raise ValueError("Не выбраны папки Mods для установки.")

        normalized_directories = [
            path.resolve() for path in self.mods_directories
        ]
        if len({str(path).casefold() for path in normalized_directories}) != len(
            normalized_directories
        ):
            raise ValueError("Одна и та же папка Mods выбрана несколько раз.")
        for directory in self.mods_directories:
            if directory.is_symlink() or not directory.is_dir():
                raise ValueError(f"Папка Mods недоступна: {directory}")

        temporary_directories = []
        backup_directories = []
        installed = []
        try:
            temporary_directory_handle = tempfile.TemporaryDirectory(
                prefix=".smapi-installer-",
                dir=self.mods_directories[0],
            )
            temporary_directories.append(temporary_directory_handle)
            temporary_directory = Path(temporary_directory_handle.name)
            extracted_directory = temporary_directory / "extracted"
            extracted_directory.mkdir()

            mod_roots = []
            for index, source in enumerate(self.sources):
                if source.is_dir():
                    roots = find_mod_roots(source)
                elif source.is_file() and source.suffix.lower() == ".zip":
                    if not zipfile.is_zipfile(source):
                        raise ValueError(f"Файл не является ZIP-архивом: {source}")
                    self.progress.emit(2, f"Распаковка: {source.name}")
                    extracted_source = extracted_directory / str(index)
                    extracted_source.mkdir()
                    extract_zip_safely(source, extracted_source)
                    roots = find_mod_roots(extracted_source)
                else:
                    raise ValueError(
                        f"Выберите ZIP-архив или папку мода: {source}"
                    )

                if not roots:
                    raise ValueError(
                        f"В источнике не найден manifest.json: {source}"
                    )
                mod_roots.extend(roots)

            names = [safe_mod_name(root.name) for root in mod_roots]
            folded_names = [name.casefold() for name in names]
            if len(folded_names) != len(set(folded_names)):
                raise ValueError(
                    "Найдено несколько модов с одинаковым именем папки. "
                    "Установите их по отдельности."
                )

            files_by_mod = [list_files(root) for root in mod_roots]
            files_per_destination = sum(len(files) for files in files_by_mod)
            total_files = files_per_destination * len(self.mods_directories)
            completed = 0
            staged_mods_by_directory = []
            for directory_index, mods_directory in enumerate(
                self.mods_directories
            ):
                staged_directory = (
                    temporary_directory / f"staged-{directory_index}"
                )
                staged_directory.mkdir()
                staged_mods = []
                for root, name, files in zip(
                    mod_roots, names, files_by_mod
                ):
                    staged_mod = staged_directory / name
                    copy_mod(
                        root,
                        staged_mod,
                        files,
                        self.progress.emit,
                        completed,
                        total_files,
                    )
                    completed += len(files)
                    staged_mods.append((name, staged_mod))
                staged_mods_by_directory.append(staged_mods)

            for mods_directory in self.mods_directories:
                backup_directories.append(
                    Path(
                        tempfile.mkdtemp(
                            prefix=".smapi-installer-backup-",
                            dir=mods_directory,
                        )
                    )
                )

            for mods_directory, staged_mods, backup_directory in zip(
                self.mods_directories,
                staged_mods_by_directory,
                backup_directories,
            ):
                for name, staged_mod in staged_mods:
                    target = mods_directory / name
                    backup = backup_directory / name
                    if target.is_symlink():
                        raise ValueError(
                            "Целевая папка является символической ссылкой: "
                            f"{target}"
                        )
                    if target.exists():
                        if (
                            not target.is_dir()
                            or not (target / "manifest.json").is_file()
                        ):
                            raise ValueError(
                                "Нельзя заменить существующий объект, который "
                                f"не похож на папку мода: {target}"
                            )
                        os.replace(target, backup)
                    installed.append((target, backup))
                    os.replace(staged_mod, target)

            cleanup_warning = ""
            for backup_directory in backup_directories:
                try:
                    shutil.rmtree(backup_directory)
                except OSError as error:
                    warning = (
                        "Установка прошла успешно, но не удалось удалить "
                        f"резервную папку {backup_directory}: {error}"
                    )
                    cleanup_warning = (
                        f"{cleanup_warning}\n{warning}"
                        if cleanup_warning
                        else warning
                    )

            self.progress.emit(100, "Установка завершена")
            return cleanup_warning
        except Exception as error:
            rollback_errors = []
            for target, backup in reversed(installed):
                try:
                    if target.exists():
                        shutil.rmtree(target)
                    if backup.exists():
                        os.replace(backup, target)
                except OSError as rollback_error:
                    rollback_errors.append(f"{target}: {rollback_error}")
            if rollback_errors:
                preserved_backups = ", ".join(
                    str(directory) for directory in backup_directories
                )
                raise RuntimeError(
                    f"{error}\nНе удалось полностью отменить установку: "
                    + "; ".join(rollback_errors)
                    + f"\nРезервные копии сохранены в: {preserved_backups}"
                ) from error
            for backup_directory in backup_directories:
                shutil.rmtree(backup_directory, ignore_errors=True)
            raise
        finally:
            for temporary_directory in temporary_directories:
                temporary_directory.cleanup()


class DropArea(QLabel):
    files_dropped = Signal(list)

    def __init__(self):
        super().__init__("Перетащите сюда ZIP-архив или папку с модом")
        self.setAcceptDrops(True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumHeight(220)
        self.setStyleSheet(
            "background: #ffffff; border: 2px dashed #8aaed0; "
            "border-radius: 6px; padding: 20px; color: #34495e;"
        )

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [
            url.toLocalFile()
            for url in event.mimeData().urls()
            if url.isLocalFile()
        ]
        if paths:
            self.files_dropped.emit(paths)


class MainWindow(QMainWindow):
    def __init__(self, settings):
        super().__init__()
        self.settings = settings
        self.game_path = Path()
        self.mod_paths = []
        self.worker = None
        self.install_completed = False

        self.setWindowTitle("SMAPI Mod Installer")
        self.resize(760, 560)

        self.game_path_label = QLabel()
        self.drop_area = DropArea()
        self.status_label = QLabel("Файлы пока не выбраны")
        self.status_label.setWordWrap(True)

        self.settings_button = QPushButton("Настройки")
        self.settings_button.clicked.connect(self.open_settings)

        self.choose_mod_button = QPushButton("Выбрать файлы вручную")
        self.choose_mod_button.clicked.connect(self.choose_mod_files)

        self.edit_selection_button = QPushButton("Изменить выбор")
        self.edit_selection_button.setEnabled(False)
        self.edit_selection_button.clicked.connect(self.edit_mod_selection)

        self.continue_button = QPushButton("Продолжить")
        self.continue_button.setEnabled(False)
        self.continue_button.clicked.connect(self.handle_continue_button)
        self.continue_button.setStyleSheet(
            "QPushButton { background: #0078d4; color: white; padding: 8px; "
            "border: none; border-radius: 3px; }"
            "QPushButton:disabled { background: #b7c9d9; }"
        )

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)

        self.drop_area.files_dropped.connect(self.set_mod_paths)

        layout = QVBoxLayout()
        layout.addWidget(self.settings_button, alignment=Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self.game_path_label)
        layout.addWidget(QLabel("Перенесите мод сюда"))
        layout.addWidget(self.drop_area)
        layout.addWidget(self.status_label)
        layout.addWidget(self.choose_mod_button)
        layout.addWidget(self.edit_selection_button)
        layout.addWidget(self.continue_button)
        layout.addWidget(self.progress_bar)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

    def configure_game_path(self):
        saved_path = self.settings.value("game_path", "", type=str)
        if saved_path and is_game_directory(saved_path):
            self.set_game_path(saved_path)
            return True

        if saved_path:
            QMessageBox.warning(
                self,
                "Путь к игре недоступен",
                "Сохранённый путь к Stardew Valley больше не существует. "
                "Укажите новый путь.",
            )

        installations = find_game_installations()
        if installations:
            detected_path = installations[0]
            answer = QMessageBox.question(
                self,
                "Найдена Stardew Valley",
                "Проверьте путь к игре:\n\n"
                f"{detected_path}\n\n"
                "Это правильная папка?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self.save_game_path(detected_path)
                return True

        return self.choose_and_save_game_path()

    def choose_and_save_game_path(self):
        while True:
            selected_path = QFileDialog.getExistingDirectory(
                self,
                "Выберите папку Stardew Valley",
                str(Path.home()),
            )
            if not selected_path:
                return False
            if not is_game_directory(selected_path):
                QMessageBox.warning(
                    self,
                    "Игра не найдена",
                    "В выбранной папке не найден файл Stardew Valley.exe. "
                    "Выберите папку установки игры.",
                )
                continue

            answer = QMessageBox.question(
                self,
                "Подтвердите путь",
                f"Использовать эту папку игры?\n\n{selected_path}",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self.save_game_path(selected_path)
                return True

    def save_game_path(self, path):
        self.settings.setValue("game_path", str(path))
        self.set_game_path(path)

    def set_game_path(self, path):
        self.game_path = Path(path)
        smapi_installed = (self.game_path / "StardewModdingAPI.exe").is_file()
        smapi_status = "SMAPI обнаружен" if smapi_installed else "SMAPI не обнаружен"
        self.game_path_label.setText(
            f"Игра: {self.game_path}    |    {smapi_status}"
        )

    def open_settings(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Настройки")
        dialog.setMinimumWidth(460)

        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("Путь к игре"))

        path_label = QLabel(str(self.game_path))
        path_label.setWordWrap(True)
        path_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(path_label)

        buttons_layout = QHBoxLayout()
        buttons_layout.addStretch()
        change_button = QPushButton("Изменить")
        close_button = QPushButton("Закрыть")
        buttons_layout.addWidget(change_button)
        buttons_layout.addWidget(close_button)
        layout.addLayout(buttons_layout)

        change_button.clicked.connect(
            lambda: self.change_game_path(path_label)
        )
        close_button.clicked.connect(dialog.accept)
        dialog.exec()

    def change_game_path(self, path_label):
        selected_path = QFileDialog.getExistingDirectory(
            self,
            "Выберите папку Stardew Valley",
            str(self.game_path),
        )
        if not selected_path:
            return
        if not is_game_directory(selected_path):
            QMessageBox.warning(
                self,
                "Игра не найдена",
                "В выбранной папке не найден файл Stardew Valley.exe.",
            )
            return

        answer = QMessageBox.question(
            self,
            "Подтвердите путь",
            f"Использовать эту папку игры?\n\n{selected_path}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.save_game_path(selected_path)
            path_label.setText(str(self.game_path))

    def choose_mod_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Выберите ZIP-архивы модов",
            "",
            "ZIP-архивы (*.zip)",
        )
        if paths:
            self.set_mod_paths(paths)

    def set_mod_paths(self, paths):
        self.mod_paths = list(dict.fromkeys(paths))
        if not self.mod_paths:
            self.status_label.setText("Файлы пока не выбраны")
            self.status_label.setStyleSheet("")
            self.progress_bar.setValue(0)
            self.install_completed = False
            self.edit_selection_button.setEnabled(False)
            self.continue_button.setText("Продолжить")
            self.continue_button.setEnabled(False)
            return

        self.install_completed = False
        self.continue_button.setText("Продолжить")
        self.status_label.setText(
            "Выбрано:\n" + "\n".join(self.mod_paths)
        )
        self.status_label.setStyleSheet("")
        self.progress_bar.setValue(0)
        self.edit_selection_button.setEnabled(True)
        self.continue_button.setEnabled(True)

    def edit_mod_selection(self):
        if not self.mod_paths:
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("Изменить выбор модов")
        dialog.setMinimumWidth(520)
        dialog.setMinimumHeight(300)

        layout = QVBoxLayout(dialog)
        layout.addWidget(
            QLabel("Выберите файлы, которые нужно убрать из списка")
        )

        files_list = QListWidget()
        files_list.setSelectionMode(
            QListWidget.SelectionMode.ExtendedSelection
        )
        for path in self.mod_paths:
            item = QListWidgetItem(Path(path).name)
            item.setToolTip(path)
            item.setData(Qt.ItemDataRole.UserRole, path)
            files_list.addItem(item)
        layout.addWidget(files_list)

        buttons_layout = QHBoxLayout()
        remove_button = QPushButton("Убрать выбранные")
        apply_button = QPushButton("Применить")
        cancel_button = QPushButton("Отмена")
        remove_button.setEnabled(False)
        buttons_layout.addWidget(remove_button)
        buttons_layout.addStretch()
        buttons_layout.addWidget(apply_button)
        buttons_layout.addWidget(cancel_button)
        layout.addLayout(buttons_layout)

        files_list.itemSelectionChanged.connect(
            lambda: remove_button.setEnabled(
                bool(files_list.selectedItems())
            )
        )

        def remove_selected():
            for item in files_list.selectedItems():
                files_list.takeItem(files_list.row(item))
            remove_button.setEnabled(False)

        remove_button.clicked.connect(remove_selected)
        apply_button.clicked.connect(dialog.accept)
        cancel_button.clicked.connect(dialog.reject)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        remaining_paths = [
            files_list.item(index).data(Qt.ItemDataRole.UserRole)
            for index in range(files_list.count())
        ]
        self.set_mod_paths(remaining_paths)

    def handle_continue_button(self):
        if self.install_completed:
            self.reset_install_state()
            return
        self.continue_install()

    def reset_install_state(self):
        self.install_completed = False
        self.mod_paths.clear()
        self.status_label.setText("Файлы пока не выбраны")
        self.status_label.setStyleSheet("")
        self.progress_bar.setValue(0)
        self.edit_selection_button.setEnabled(False)
        self.continue_button.setText("Продолжить")
        self.continue_button.setEnabled(False)

    def continue_install(self):
        mods_directories = find_mods_directories(self.game_path)
        if not mods_directories:
            QMessageBox.warning(
                self,
                "Папки с модами не найдены",
                "В папке игры не найдены каталоги Mods_ или стандартная папка "
                f"Mods:\n\n{self.game_path}",
            )
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("Выбор папки с модами")
        dialog.setMinimumWidth(460)

        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("Выберите папку с модами"))

        folders_list = QListWidget()
        preferred_paths = self.settings.value(
            "mods_directories", [], type=list
        )
        if not preferred_paths:
            preferred_path = self.settings.value(
                "mods_directory", "", type=str
            )
            preferred_paths = [preferred_path] if preferred_path else []

        for directory in mods_directories:
            item = QListWidgetItem(directory.name)
            item.setToolTip(str(directory))
            item.setData(Qt.ItemDataRole.UserRole, str(directory))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked
                if str(directory) in preferred_paths
                else Qt.CheckState.Unchecked
            )
            folders_list.addItem(item)
        layout.addWidget(folders_list)

        buttons_layout = QHBoxLayout()
        buttons_layout.addStretch()
        select_button = QPushButton("Выбрать")
        cancel_button = QPushButton("Отмена")
        select_button.setEnabled(False)
        buttons_layout.addWidget(select_button)
        buttons_layout.addWidget(cancel_button)
        layout.addLayout(buttons_layout)

        folders_list.itemChanged.connect(
            lambda: select_button.setEnabled(
                any(
                    folders_list.item(index).checkState()
                    == Qt.CheckState.Checked
                    for index in range(folders_list.count())
                )
            )
        )
        select_button.clicked.connect(dialog.accept)
        cancel_button.clicked.connect(dialog.reject)

        select_button.setEnabled(
            any(
                folders_list.item(index).checkState()
                == Qt.CheckState.Checked
                for index in range(folders_list.count())
            )
        )

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        mods_directories = [
            Path(folders_list.item(index).data(Qt.ItemDataRole.UserRole))
            for index in range(folders_list.count())
            if folders_list.item(index).checkState() == Qt.CheckState.Checked
        ]
        if not mods_directories:
            return
        self.settings.setValue(
            "mods_directories",
            [str(directory) for directory in mods_directories],
        )
        self.settings.setValue(
            "mods_directory", str(mods_directories[0])
        )

        self.progress_bar.setValue(0)
        self.status_label.setText("Подготовка установки...")
        self.status_label.setStyleSheet("")
        self.set_busy(True)

        self.worker = InstallWorker(self.mod_paths, mods_directories)
        self.worker.progress.connect(self.update_progress)
        self.worker.succeeded.connect(self.install_succeeded)
        self.worker.failed.connect(self.install_failed)
        self.worker.finished.connect(self.install_finished)
        self.worker.start()

    def update_progress(self, value, message):
        self.progress_bar.setValue(value)
        self.status_label.setText(message)

    def install_succeeded(self, cleanup_warning):
        self.progress_bar.setValue(100)
        message = "Все моды установлены"
        if cleanup_warning:
            message += f"\n{cleanup_warning}"
        self.status_label.setText(message)
        self.status_label.setStyleSheet("color: #107c10;")
        self.mod_paths.clear()
        self.edit_selection_button.setEnabled(False)
        self.install_completed = True
        self.continue_button.setText("Завершить")

    def install_failed(self, message):
        self.status_label.setText(f"Ошибка установки: {message}")
        self.status_label.setStyleSheet("color: #c42b1c;")
        dialog = QDialog(self)
        dialog.setWindowTitle("Ошибка установки")
        dialog.setMinimumWidth(420)

        layout = QVBoxLayout(dialog)
        error_label = QLabel(message)
        error_label.setWordWrap(True)
        error_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(error_label)

        buttons_layout = QHBoxLayout()
        buttons_layout.addStretch()
        close_button = QPushButton("Закрыть")
        close_button.clicked.connect(dialog.accept)
        buttons_layout.addWidget(close_button)
        layout.addLayout(buttons_layout)
        dialog.exec()

    def install_finished(self):
        self.set_busy(False)
        self.worker = None

    def set_busy(self, busy):
        self.settings_button.setEnabled(not busy)
        self.choose_mod_button.setEnabled(not busy)
        self.edit_selection_button.setEnabled(
            not busy and bool(self.mod_paths)
        )
        self.continue_button.setEnabled(
            not busy and (bool(self.mod_paths) or self.install_completed)
        )
        self.drop_area.setEnabled(not busy)

    def closeEvent(self, event):
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.information(
                self,
                "Установка выполняется",
                "Дождитесь завершения установки, прежде чем закрывать окно.",
            )
            event.ignore()
            return
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setOrganizationName("SMAPI-Mod-Installer")
    app.setApplicationName("SMAPI-Mod-Installer")
    settings = QSettings()

    window = MainWindow(settings)
    if not window.configure_game_path():
        return 0
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
