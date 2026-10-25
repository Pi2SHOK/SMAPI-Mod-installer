import msvcrt
import shutil
import subprocess
import sys
from pathlib import Path


BUILDER_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BUILDER_DIR.parent
DIST_DIR = PROJECT_DIR / "dist"
WORK_DIR = BUILDER_DIR / "build"
ICON_PATH = BUILDER_DIR / "icon.ico"
INSTALLER_SCRIPT = PROJECT_DIR / "installer.py"
LICENSE_FILE = PROJECT_DIR / "LICENSE"


def get_key():
    try:
        return msvcrt.getch().decode("utf-8")
    except UnicodeDecodeError:
        return ""


def folder_name():
    print("Выберите название папки сборки:")
    print()
    print("[1] main")
    print("[2] unstable")

    while True:
        key = get_key()
        if key == "1":
            return "main"
        if key == "2":
            return "unstable"
        print("Нажмите 1 или 2.")


def build(release_name):
    if not INSTALLER_SCRIPT.is_file():
        raise FileNotFoundError(f"Файл инсталлятора не найден: {INSTALLER_SCRIPT}")

    DIST_DIR.mkdir(parents=True, exist_ok=True)
    release_dir = DIST_DIR / release_name
    archive_path = DIST_DIR / f"{release_name}.zip"
    built_executable = DIST_DIR / "installer.exe"

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--onefile",
        "--windowed",
        "--noconfirm",
        "--clean",
        "--name=installer",
        f"--specpath={BUILDER_DIR}",
        f"--distpath={DIST_DIR}",
        f"--workpath={WORK_DIR}",
        str(INSTALLER_SCRIPT),
    ]
    if ICON_PATH.is_file():
        command.append(f"--icon={ICON_PATH}")

    print("\nСборка installer.py...")
    subprocess.run(command, check=True, cwd=PROJECT_DIR)

    if not built_executable.is_file():
        raise FileNotFoundError(
            f"PyInstaller не создал исполняемый файл: {built_executable}"
        )

    if release_dir.exists():
        shutil.rmtree(release_dir)
    release_dir.mkdir(parents=True)
    shutil.copy2(built_executable, release_dir / "installer.exe")
    if LICENSE_FILE.is_file():
        shutil.copy2(LICENSE_FILE, release_dir / LICENSE_FILE.name)

    print(f"\nСоздание архива: {archive_path.name}")
    shutil.make_archive(
        str(DIST_DIR / release_name),
        "zip",
        root_dir=release_dir,
    )

    built_executable.unlink()
    if WORK_DIR.exists():
        shutil.rmtree(WORK_DIR)

    spec_file = BUILDER_DIR / "installer.spec"
    if spec_file.exists():
        spec_file.unlink()

    print("\nСборка завершена:")
    print(f"Папка: {release_dir}")
    print(f"Архив: {archive_path}")


if __name__ == "__main__":
    selected_folder = folder_name()
    release_name = f"SMAPI-mod-preloader-{selected_folder}"

    try:
        build(release_name)
    except (FileNotFoundError, subprocess.CalledProcessError, OSError) as error:
        print(f"\nОшибка сборки: {error}")
        sys.exit(1)
