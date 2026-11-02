"""Build dist/ndvi_downloader-<version>.zip, installable via QGIS > Plugins > Install from ZIP."""

import configparser
import os
import zipfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PLUGIN = "ndvi_downloader"
SKIP_DIRS = {"__pycache__", ".pytest_cache"}
SKIP_EXT = {".pyc", ".pyo"}


def main():
    meta = configparser.ConfigParser()
    meta.read(os.path.join(ROOT, PLUGIN, "metadata.txt"), encoding="utf-8")
    version = meta["general"]["version"]
    os.makedirs(os.path.join(ROOT, "dist"), exist_ok=True)
    out = os.path.join(ROOT, "dist", f"{PLUGIN}-{version}.zip")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for folder, dirs, files in os.walk(os.path.join(ROOT, PLUGIN)):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for name in files:
                if os.path.splitext(name)[1] in SKIP_EXT:
                    continue
                path = os.path.join(folder, name)
                zf.write(path, os.path.relpath(path, ROOT))
        zf.write(os.path.join(ROOT, "LICENSE"), f"{PLUGIN}/LICENSE")
    print(out)


if __name__ == "__main__":
    main()
