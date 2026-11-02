"""Copy the plugin into the default QGIS profile for local testing.

    python scripts/install_dev.py            # copy
    python scripts/install_dev.py --link     # directory junction/symlink, edits show up after reload
"""

import os
import platform
import shutil
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "ndvi_downloader")


def plugins_dir(profile="default"):
    system = platform.system()
    if system == "Windows":
        base = os.path.join(os.environ["APPDATA"], "QGIS", "QGIS3")
    elif system == "Darwin":
        base = os.path.expanduser("~/Library/Application Support/QGIS/QGIS3")
    else:
        base = os.path.expanduser("~/.local/share/QGIS/QGIS3")
    return os.path.join(base, "profiles", profile, "python", "plugins")


def main():
    dest_root = plugins_dir()
    os.makedirs(dest_root, exist_ok=True)
    dest = os.path.join(dest_root, "ndvi_downloader")
    # A broken junction is invisible to os.path.lexists, so look at the folder listing
    if "ndvi_downloader" in os.listdir(dest_root):
        try:
            if os.path.islink(dest):
                os.unlink(dest)
            else:
                try:  # removes a Windows junction without touching its target
                    os.rmdir(dest)
                except OSError:
                    shutil.rmtree(dest)
        except OSError as exc:
            sys.exit(
                f"Tidak bisa menghapus {dest}: {exc}\n"
                "Kemungkinan junction lama rusak. Hapus manual dari Command Prompt:\n"
                f'  fsutil reparsepoint delete "{dest}"\n'
                f'  rmdir "{dest}"'
            )

    linked = False
    if "--link" in sys.argv:
        if platform.system() == "Windows":
            subprocess.call(["cmd", "/c", "mklink", "/J", dest, SRC])
        else:
            os.symlink(SRC, dest)
        try:  # a link made from a sandboxed shell can point at a volume QGIS cannot see
            linked = "metadata.txt" in os.listdir(dest)
        except OSError:
            linked = False
        if not linked:
            print("Link tidak bisa dibaca, beralih ke salinan biasa.")
            try:
                os.unlink(dest) if os.path.islink(dest) else os.rmdir(dest)
            except OSError:
                pass
    if not linked:
        shutil.copytree(SRC, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    print("Installed to", dest, "(link)" if linked else "(copy)")
    print("Restart QGIS, then enable it in Plugins > Manage and Install Plugins > Installed.")


if __name__ == "__main__":
    main()
