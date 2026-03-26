#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Vojnisek
"""AppImage Manager - Registry, symlinks, and .desktop launcher management.

Scans a directory for .AppImage files, maintains a CSV registry,
creates version-agnostic symlinks, generates .desktop launchers,
and extracts icons — so AppImages appear in your application launcher.

Usage:
    appimage-manager.py                    # Scan + sync (default)
    appimage-manager.py list               # Show managed apps
    appimage-manager.py scan               # Scan only, update CSV
    appimage-manager.py sync               # Sync symlinks + .desktop from CSV
    appimage-manager.py extract-icons      # Extract missing icons
    appimage-manager.py --install-watch    # Setup systemd auto-trigger
    appimage-manager.py --uninstall-watch  # Remove systemd auto-trigger
"""

import argparse
import csv
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

try:
    import fcntl
except ImportError:
    fcntl = None  # Non-Linux: file locking disabled

__version__ = "1.0.1"

CSV_FIELDS = [
    "id", "label", "filename", "symlink", "icon",
    "categories", "startup_wm_class", "terminal", "status", "created_at", "updated_at",
]

# ─── Colors ──────────────────────────────────────────────────────────────────

GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
RED = "\033[0;31m"
CYAN = "\033[0;36m"
BOLD = "\033[1m"
NC = "\033[0m"


def color(text, c):
    return f"{c}{text}{NC}" if sys.stdout.isatty() else str(text)


# ─── Constants ───────────────────────────────────────────────────────────────

_APPIMAGE_RE = re.compile(r"\.[Aa]pp[Ii]mage$")

# ─── Name extraction ────────────────────────────────────────────────────────

_STRIP_RE = re.compile(
    r"[-_](v?\d+\.\d|[0-9]{6,}|x86|x64|amd64|arm64|aarch64|i[36]86|"
    r"linux|Linux|nightly|build|conda|Ubuntu|electron|Electron).*"
)

_VERSION_RE = re.compile(r"[-_]v?(\d+\.\d+(?:\.\d+)*)")


def extract_id(filename):
    """Extract canonical app name from AppImage filename."""
    name = _APPIMAGE_RE.sub("", filename)
    name = _STRIP_RE.sub("", name)
    name = name.strip("-_ ").lower()
    name = re.sub(r"[+_]", "-", name)
    return name if name else _APPIMAGE_RE.sub("", filename).lower()


def extract_version(filename):
    """Extract version tuple from filename for sorting. Returns (0,) if none found."""
    # Strip the extension and platform/arch suffixes first to avoid
    # matching version-like strings in platform names (e.g. Ubuntu20.04)
    name = _APPIMAGE_RE.sub("", filename)
    name = re.sub(r"[-_](Ubuntu|linux|Linux|build|conda|electron|Electron).*", "", name)
    m = _VERSION_RE.search(name)
    if m:
        return tuple(int(x) for x in m.group(1).split("."))
    return (0,)


def extract_label(filename):
    """Extract display name preserving original casing."""
    name = _APPIMAGE_RE.sub("", filename)
    name = _STRIP_RE.sub("", name)
    name = name.strip("-_ ")
    name = name.replace("+", " ").replace("_", " ")
    # Capitalize if the original was all lowercase
    if name == name.lower():
        name = name.title()
    return name


# ─── Registry I/O ───────────────────────────────────────────────────────────

def load_registry(csv_path):
    if not csv_path.exists():
        return []
    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            records = list(csv.DictReader(f))
        # Validate required fields, fill missing with defaults
        for rec in records:
            for field in CSV_FIELDS:
                rec.setdefault(field, "")
        return records
    except (csv.Error, UnicodeDecodeError) as e:
        print(f"  {color('WARN', YELLOW)} Failed to read {csv_path}: {e}", file=sys.stderr)
        print(f"  {color('WARN', YELLOW)} Starting with empty registry.", file=sys.stderr)
        return []


def save_registry(csv_path, records):
    lockfile = csv_path.with_suffix(".lock")
    lf = None
    try:
        lf = open(lockfile, "w")
        if fcntl:
            fcntl.flock(lf, fcntl.LOCK_EX)
        tmp = csv_path.with_suffix(".tmp")
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
            writer.writeheader()
            for rec in records:
                writer.writerow(rec)
        tmp.replace(csv_path)
    finally:
        if lf:
            lf.close()
        lockfile.unlink(missing_ok=True)


# ─── XDG icon management ─────────────────────────────────────────────────────

_HICOLOR_BASE = Path.home() / ".local/share/icons/hicolor"
_PNG_HEADER = b"\x89PNG\r\n\x1a\n"


def _get_png_size(path):
    """Read width from PNG header. Returns size or 256 as fallback."""
    import struct
    try:
        with open(path, "rb") as f:
            header = f.read(24)
        if header[:8] == _PNG_HEADER:
            return struct.unpack(">I", header[16:20])[0]
    except OSError:
        pass
    return 256


def _is_svg(path):
    """Check if file is SVG regardless of extension."""
    try:
        with open(path, "rb") as f:
            header = f.read(256)
        return b"<svg" in header or b"<?xml" in header
    except OSError:
        return False


def _icon_name(app_id):
    """Generate the freedesktop icon name for an app."""
    return f"appimage-{app_id}"


def _install_icon_to_hicolor(icon_file, app_id):
    """Install an icon into the XDG hicolor theme. Returns icon name or None."""
    name = _icon_name(app_id)

    if _is_svg(icon_file):
        dest_dir = _HICOLOR_BASE / "scalable/apps"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{name}.svg"
        shutil.copy2(icon_file, dest)
    else:
        size = _get_png_size(icon_file)
        dest_dir = _HICOLOR_BASE / f"{size}x{size}/apps"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{name}.png"
        shutil.copy2(icon_file, dest)

    return name


def _uninstall_icon_from_hicolor(app_id):
    """Remove all icon files for an app from the hicolor theme."""
    name = _icon_name(app_id)
    removed = False
    if _HICOLOR_BASE.exists():
        for icon_file in _HICOLOR_BASE.rglob(f"{name}.*"):
            if icon_file.is_file():
                icon_file.unlink()
                removed = True
    return removed


def _has_hicolor_icon(app_id):
    """Check if an icon is installed in the hicolor theme."""
    name = _icon_name(app_id)
    if not _HICOLOR_BASE.exists():
        return False
    return any(_HICOLOR_BASE.rglob(f"{name}.*"))


def _update_icon_cache():
    """Refresh the GTK icon cache for the hicolor theme."""
    try:
        subprocess.run(
            ["gtk-update-icon-cache", "-f", "-t", str(_HICOLOR_BASE)],
            capture_output=True,
        )
    except FileNotFoundError:
        pass


# ─── Icon extraction ─────────────────────────────────────────────────────────

def _is_safe_path(path, root):
    """Check that resolved path is inside root directory (path traversal guard)."""
    try:
        return path.resolve().is_relative_to(root.resolve())
    except (ValueError, OSError):
        return False


def _safe_copy(src, dest, root):
    """Copy src to dest only if src is safely inside root.

    Returns dest path on success, or None on failure.
    """
    if not _is_safe_path(src, root):
        return None
    try:
        shutil.copy2(src, dest)
        return dest
    except OSError:
        return None


def _appimage_extract(appimage_path, pattern, tmpdir):
    """Run --appimage-extract for a glob pattern. Returns True on success."""
    try:
        result = subprocess.run(
            [str(appimage_path), "--appimage-extract", pattern],
            cwd=tmpdir, capture_output=True, timeout=15,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _extract_best_hicolor(appimage_path, icon_name, icon_dest, tmpdir, sqroot):
    """Extract the largest hicolor icon available."""
    _appimage_extract(
        appimage_path,
        f"usr/share/icons/hicolor/*/apps/{icon_name}.*",
        tmpdir,
    )
    hicolor = sqroot / "usr/share/icons/hicolor"
    if not hicolor.exists():
        return None

    candidates = []
    for icon_file in hicolor.rglob(f"{icon_name}.*"):
        if not icon_file.is_file() or icon_file.suffix.lower() not in (".png", ".svg"):
            continue
        size = 0
        for part in icon_file.parts:
            m = re.match(r"(\d+)x(\d+)", part)
            if m:
                size = int(m.group(1))
                break
        candidates.append((size, icon_file))

    if not candidates:
        return None

    candidates.sort(key=lambda c: (c[0], c[1].suffix == ".png"), reverse=True)
    return _safe_copy(candidates[0][1], icon_dest, sqroot)


def _read_embedded_desktop(appimage_path, tmpdir, sqroot):
    """Extract and parse the embedded .desktop file."""
    result = {"icon_name": None, "categories": None, "startup_wm_class": None}
    _appimage_extract(appimage_path, "*.desktop", tmpdir)
    for desktop_file in sqroot.glob("*.desktop"):
        if desktop_file.is_symlink() and not desktop_file.exists():
            continue
        in_entry = False
        for line in desktop_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            stripped = line.strip()
            if stripped == "[Desktop Entry]":
                in_entry = True
                continue
            if stripped.startswith("[") and stripped.endswith("]"):
                in_entry = False
                continue
            if not in_entry:
                continue
            if stripped.startswith("Icon="):
                result["icon_name"] = stripped.split("=", 1)[1].strip()
            elif stripped.startswith("Categories="):
                result["categories"] = stripped.split("=", 1)[1].strip()
            elif stripped.startswith("StartupWMClass="):
                result["startup_wm_class"] = stripped.split("=", 1)[1].strip()
        if result["icon_name"]:
            break
    return result


def extract_metadata(appimage_path, app_id, icons_dir):
    """Extract icon and categories from an AppImage.

    Returns dict: {"icon": path_or_None, "categories": str_or_None}
    """
    icons_dir.mkdir(parents=True, exist_ok=True)
    icon_dest = icons_dir / f"{app_id}.png"
    tmpdir = tempfile.mkdtemp(prefix="appimage-meta-")

    try:
        sqroot = Path(tmpdir) / "squashfs-root"
        icon_result = None
        categories = None

        # Read embedded .desktop for categories (and icon name as fallback)
        desktop_info = _read_embedded_desktop(appimage_path, tmpdir, sqroot)
        categories = desktop_info["categories"]

        # Step 1-2: .DirIcon
        if _appimage_extract(appimage_path, ".DirIcon", tmpdir):
            diricon = sqroot / ".DirIcon"

            if diricon.exists() and not diricon.is_symlink():
                icon_result = _safe_copy(diricon, icon_dest, sqroot)

            elif diricon.is_symlink():
                link_target = os.readlink(diricon)
                _appimage_extract(appimage_path, link_target, tmpdir)
                resolved = (sqroot / link_target).resolve()
                if _is_safe_path(resolved, sqroot):
                    if resolved.exists() and resolved.is_file():
                        icon_result = _safe_copy(resolved, icon_dest, sqroot)
                    # Step 3: hicolor fallback
                    elif "hicolor" in link_target:
                        icon_name = Path(link_target).stem
                        icon_result = _extract_best_hicolor(
                            appimage_path, icon_name, icon_dest, tmpdir, sqroot,
                        )

        # Step 4: Icon from .desktop Icon= name
        if not icon_result and desktop_info["icon_name"]:
            icon_name = desktop_info["icon_name"]
            _appimage_extract(appimage_path, f"{icon_name}.png", tmpdir)
            root_icon = sqroot / f"{icon_name}.png"
            if root_icon.exists() and root_icon.is_file():
                icon_result = _safe_copy(root_icon, icon_dest, sqroot)
            if not icon_result:
                icon_result = _extract_best_hicolor(
                    appimage_path, icon_name, icon_dest, tmpdir, sqroot,
                )

        return {
            "icon": icon_result,
            "categories": categories,
            "startup_wm_class": desktop_info.get("startup_wm_class"),
        }
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def extract_metadata_for_records(apps_dir, records, cfg, force=False):
    """Extract icons and categories for records missing them."""
    icons_dir = apps_dir / cfg["icons_dir"]
    extracted = failed = 0
    icons_changed = False
    appimage_tag = cfg.get("appimage_tag", "X-AppImage;")

    for rec in records:
        if rec["status"] == "removed" and not force:
            continue
        has_icon = _has_hicolor_icon(rec["id"]) and not force
        has_categories = appimage_tag in rec.get("categories", "") and not force
        if has_icon and has_categories:
            continue
        appimage = apps_dir / rec["filename"]
        if not appimage.exists():
            continue

        # Ensure executable before extraction
        if not os.access(appimage, os.X_OK):
            try:
                appimage.chmod(appimage.stat().st_mode | 0o111)
            except OSError:
                pass

        sys.stdout.write(f"  Extracting {rec['id']}...")
        sys.stdout.flush()
        meta = extract_metadata(appimage, rec["id"], icons_dir)
        updated = False
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        if meta["icon"]:
            # Install icon to XDG hicolor theme
            try:
                icon_name = _install_icon_to_hicolor(meta["icon"], rec["id"])
                rec["icon"] = icon_name
                icons_changed = True
            except OSError:
                rec["icon"] = _icon_name(rec["id"])
            updated = True
        if meta.get("startup_wm_class"):
            rec["startup_wm_class"] = meta["startup_wm_class"]
            updated = True
        if meta["categories"]:
            cats = meta["categories"].rstrip(";") + ";" + appimage_tag
            rec["categories"] = cats
            updated = True
        elif appimage_tag not in rec.get("categories", ""):
            rec["categories"] = rec.get("categories", cfg["default_categories"]).rstrip(";") + ";" + appimage_tag
            updated = True

        if updated:
            rec["updated_at"] = now
            print(f" {color('OK', GREEN)}")
            extracted += 1
        else:
            print(f" {color('not found', YELLOW)}")
            failed += 1

    if icons_changed:
        _update_icon_cache()

    return extracted, failed


# ─── Scan ────────────────────────────────────────────────────────────────────

def find_appimages(apps_dir):
    """Find all real (non-symlink) AppImage files, grouped by canonical id."""
    if not apps_dir.is_dir():
        print(f"{color('Error:', RED)} Directory not found: {apps_dir}")
        sys.exit(1)

    groups = {}
    for f in sorted(apps_dir.iterdir()):
        if f.is_symlink() or not f.is_file():
            continue
        if not _APPIMAGE_RE.search(f.name):
            continue
        groups.setdefault(extract_id(f.name), []).append(f)

    for app_id in groups:
        groups[app_id].sort(
            key=lambda p: (extract_version(p.name), p.stat().st_mtime),
            reverse=True,
        )

    return groups


def scan(apps_dir, records, cfg):
    """Scan apps_dir, update registry. Returns (updated_records, changes)."""
    groups = find_appimages(apps_dir)
    existing = {r["id"]: r for r in records}
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    changes = []

    for app_id, files in groups.items():
        latest = files[0]
        if app_id in existing:
            rec = existing[app_id]
            if rec["status"] == "removed":
                rec["status"] = "active"
                rec["filename"] = latest.name
                rec["updated_at"] = now
                changes.append(("REAPPEARED", app_id, latest.name))
            elif rec["filename"] != latest.name:
                old = rec["filename"]
                rec["filename"] = latest.name
                rec["updated_at"] = now
                changes.append(("UPDATED", app_id, f"{latest.name} (was {old})"))
        else:
            existing[app_id] = {
                "id": app_id,
                "label": extract_label(latest.name),
                "filename": latest.name,
                "symlink": f"{app_id}.AppImage",
                "icon": _icon_name(app_id),
                "categories": cfg["default_categories"],
                "startup_wm_class": "",
                "terminal": cfg["default_terminal"],
                "status": "active",
                "created_at": now,
                "updated_at": now,
            }
            changes.append(("NEW", app_id, latest.name))

    for app_id, rec in existing.items():
        if rec["status"] == "active" and app_id not in groups:
            rec["status"] = "removed"
            rec["updated_at"] = now
            changes.append(("REMOVED", app_id, rec["filename"]))

    status_order = {"active": 0, "ignored": 1, "removed": 2}
    result = sorted(
        existing.values(),
        key=lambda r: (status_order.get(r["status"], 9), r["id"]),
    )
    return result, changes


# ─── Sync ────────────────────────────────────────────────────────────────────

def generate_desktop(rec, symlink_path, cfg):
    """Generate .desktop file content."""
    lines = [
        "[Desktop Entry]",
        "# Managed by appimage-manager.py",
        "Type=Application",
        f"Name={rec['label']}",
        f"Exec={symlink_path} %U",
        f"TryExec={symlink_path}",
        f"Icon={rec['icon']}",
        f"Terminal={'true' if rec['terminal'] == 'true' else 'false'}",
        f"Categories={rec['categories']}",
    ]
    wm_class = rec.get("startup_wm_class", "")
    if wm_class:
        lines.append(f"StartupWMClass={wm_class}")
    return "\n".join(lines) + "\n"


def sync(apps_dir, desktop_dir, records, cfg, dry_run=False):
    """Sync symlinks and .desktop files from registry. Returns action count."""
    desktop_dir.mkdir(parents=True, exist_ok=True)
    actions = 0
    icons_changed = False
    prefix = cfg["desktop_prefix"]

    for rec in records:
        symlink_path = apps_dir / rec["symlink"]
        desktop_path = desktop_dir / f"{prefix}{rec['id']}.desktop"

        if rec["status"] == "active":
            target = apps_dir / rec["filename"]
            if not target.exists():
                print(f"  {color('WARN', YELLOW)} {rec['id']}: file missing ({rec['filename']})")
                continue

            # Ensure executable
            if not os.access(target, os.X_OK):
                if not dry_run:
                    target.chmod(target.stat().st_mode | 0o111)
                print(f"  {color('CHMOD', CYAN)} +x {rec['filename']}")
                actions += 1

            # Symlink
            if symlink_path.is_symlink():
                if os.readlink(symlink_path) != rec["filename"]:
                    if not dry_run:
                        symlink_path.unlink()
                        symlink_path.symlink_to(rec["filename"])
                    print(f"  {color('UPDATE', YELLOW)} symlink {rec['symlink']} -> {rec['filename']}")
                    actions += 1
            elif not symlink_path.exists():
                if not dry_run:
                    symlink_path.symlink_to(rec["filename"])
                print(f"  {color('CREATE', GREEN)} symlink {rec['symlink']} -> {rec['filename']}")
                actions += 1

            # .desktop file — use absolute path of the symlink itself
            content = generate_desktop(rec, symlink_path.absolute(), cfg)
            if desktop_path.exists():
                if desktop_path.read_text(encoding="utf-8") != content:
                    if not dry_run:
                        desktop_path.write_text(content, encoding="utf-8")
                    print(f"  {color('UPDATE', YELLOW)} {desktop_path.name}")
                    actions += 1
            else:
                if not dry_run:
                    desktop_path.write_text(content, encoding="utf-8")
                print(f"  {color('CREATE', GREEN)} {desktop_path.name}")
                actions += 1

        elif rec["status"] in ("removed", "ignored"):
            if symlink_path.is_symlink():
                if not dry_run:
                    symlink_path.unlink()
                print(f"  {color('REMOVE', RED)} symlink {rec['symlink']}")
                actions += 1
            if desktop_path.exists():
                if not dry_run:
                    desktop_path.unlink()
                print(f"  {color('REMOVE', RED)} {desktop_path.name}")
                actions += 1
            # Remove icon from hicolor theme
            if _has_hicolor_icon(rec["id"]):
                if not dry_run:
                    _uninstall_icon_from_hicolor(rec["id"])
                    icons_changed = True
                print(f"  {color('REMOVE', RED)} icon {_icon_name(rec['id'])}")
                actions += 1

    if actions > 0 and not dry_run:
        if icons_changed:
            _update_icon_cache()
        try:
            subprocess.run(
                ["update-desktop-database", str(desktop_dir)],
                capture_output=True,
            )
        except FileNotFoundError:
            pass

    return actions


# ─── List ────────────────────────────────────────────────────────────────────

def list_apps(records):
    """Display managed apps as a table."""
    if not records:
        print("No apps registered. Run without arguments to scan.")
        return

    id_w = max(max(len(r["id"]) for r in records), 2)
    label_w = max(max(len(r["label"]) for r in records), 5)
    file_w = max(min(max(len(r["filename"]) for r in records), 45), 8)

    header = f"  {'ID':<{id_w}}  {'Label':<{label_w}}  {'Filename':<{file_w}}  Status"
    print(f"\n{color(header, BOLD)}")
    print(f"  {'-' * id_w}  {'-' * label_w}  {'-' * file_w}  --------")

    status_colors = {"active": GREEN, "ignored": YELLOW, "removed": RED}
    for rec in records:
        fname = rec["filename"]
        if len(fname) > file_w:
            fname = fname[: file_w - 3] + "..."
        status = rec["status"]
        print(f"  {rec['id']:<{id_w}}  {rec['label']:<{label_w}}  {fname:<{file_w}}  {color(status, status_colors.get(status, ''))}")

    counts = {}
    for r in records:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    parts = [f"{counts[s]} {s}" for s in ("active", "ignored", "removed") if counts.get(s, 0)]
    print(f"\n  {', '.join(parts)}\n")


# ─── Systemd watch ──────────────────────────────────────────────────────────

def install_watch(apps_dir, cfg):
    """Create systemd user path + service units to auto-trigger on file changes."""
    unit_dir = Path.home() / ".config/systemd/user"
    unit_dir.mkdir(parents=True, exist_ok=True)

    script_path = Path(__file__).resolve()
    python_path = shutil.which("python3") or "/usr/bin/python3"
    name = "appimage-manager"

    service_content = (
        f"[Unit]\n"
        f"Description=Update AppImage symlinks and launchers\n\n"
        f"[Service]\n"
        f"Type=oneshot\n"
        f"ExecStart={python_path} {script_path}\n"
    )
    (unit_dir / f"{name}.service").write_text(service_content, encoding="utf-8")

    path_content = (
        f"[Unit]\n"
        f"Description=Watch {apps_dir} for AppImage changes\n\n"
        f"[Path]\n"
        f"PathChanged={apps_dir}\n"
        f"Unit={name}.service\n\n"
        f"[Install]\n"
        f"WantedBy=default.target\n"
    )
    (unit_dir / f"{name}.path").write_text(path_content, encoding="utf-8")

    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", f"{name}.path"], check=True)
    print(f"{color('Done!', GREEN)} Watching {apps_dir} for changes.")


def uninstall_watch():
    """Remove systemd watch units."""
    name = "appimage-manager"
    subprocess.run(
        ["systemctl", "--user", "disable", "--now", f"{name}.path"],
        capture_output=True,
    )
    unit_dir = Path.home() / ".config/systemd/user"
    for suffix in (".path", ".service"):
        unit = unit_dir / f"{name}{suffix}"
        if unit.exists():
            unit.unlink()
            print(f"  Removed {unit}")
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    print(f"{color('Done!', GREEN)} Watch removed.")


# ═════════════════════════════════════════════════════════════════════════════
# CONFIGURATION — Edit defaults below or override via CLI flags
# ═════════════════════════════════════════════════════════════════════════════

CONFIG = {
    # Directory containing .AppImage files
    "apps_dir": Path.home() / "apps",

    # Where to write .desktop launcher files
    "desktop_dir": Path.home() / ".local/share/applications",

    # CSV registry filename (relative to apps_dir)
    "csv_file": "appimages.csv",

    # Prefix for managed .desktop files (avoids conflicts)
    "desktop_prefix": "appimage-",

    # Directory for extracted icons (relative to apps_dir)
    "icons_dir": "icons",

    # Default .desktop categories for new apps (before metadata extraction)
    "default_categories": "Utility;X-AppImage;",

    # Default terminal setting for new apps (user can edit in CSV)
    "default_terminal": "false",

    # Tag appended to categories for all managed apps
    "appimage_tag": "X-AppImage;",
}

# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="AppImage Manager - symlinks, .desktop launchers, and registry.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  appimage-manager.py              Scan + sync (default)\n"
            "  appimage-manager.py list          Show managed apps\n"
            "  appimage-manager.py extract-icons  Extract missing icons\n"
            "  appimage-manager.py extract-icons --force  Re-extract all\n"
            "  appimage-manager.py --dry-run     Preview all changes\n"
            "  appimage-manager.py --install-watch   Auto-trigger on changes\n"
        ),
    )
    parser.add_argument("command", nargs="?", default="all",
                        choices=["all", "list", "scan", "sync", "extract-icons"],
                        help="Command to run (default: all = scan + sync)")
    parser.add_argument("--force", action="store_true",
                        help="Force re-extraction (with extract-icons)")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="Preview changes without applying")
    parser.add_argument("-V", "--version", action="version",
                        version=f"%(prog)s {__version__}")
    parser.add_argument("--apps-dir", type=Path, default=None,
                        help=f"AppImage directory (default: {CONFIG['apps_dir']})")
    parser.add_argument("--desktop-dir", type=Path, default=None,
                        help=f".desktop output (default: {CONFIG['desktop_dir']})")
    parser.add_argument("--csv", type=str, default=None,
                        help=f"Registry CSV filename (default: {CONFIG['csv_file']})")
    parser.add_argument("--install-watch", action="store_true",
                        help="Setup systemd path unit for auto-trigger")
    parser.add_argument("--uninstall-watch", action="store_true",
                        help="Remove systemd path unit")
    args = parser.parse_args()

    cfg = dict(CONFIG)
    if args.apps_dir:
        cfg["apps_dir"] = args.apps_dir
    if args.desktop_dir:
        cfg["desktop_dir"] = args.desktop_dir
    if args.csv:
        cfg["csv_file"] = args.csv

    apps_dir = Path(cfg["apps_dir"])
    desktop_dir = Path(cfg["desktop_dir"])
    csv_path = apps_dir / cfg["csv_file"]

    if args.install_watch:
        install_watch(apps_dir, cfg)
        return
    if args.uninstall_watch:
        uninstall_watch()
        return

    records = load_registry(csv_path)

    if args.command == "list":
        list_apps(records)
        return

    if args.command == "extract-icons":
        print(f"\nExtracting metadata{' (force)' if args.force else ''}...")
        extracted, failed = extract_metadata_for_records(
            apps_dir, records, cfg, force=args.force,
        )
        if extracted > 0:
            save_registry(csv_path, records)
        print(f"\n  {extracted} extracted, {failed} not found\n")
        return

    if args.command in ("all", "scan"):
        tag = "[DRY RUN] " if args.dry_run else ""
        print(f"\n{tag}Scanning {apps_dir}...")
        records, changes = scan(apps_dir, records, cfg)

        for action, app_id, detail in changes:
            colors = {"NEW": GREEN, "UPDATED": YELLOW, "REMOVED": RED, "REAPPEARED": CYAN}
            print(f"  {color(action, colors.get(action, ''))} {app_id} ({detail})")
        if not changes:
            print("  No changes detected.")

        if not args.dry_run and changes:
            save_registry(csv_path, records)
            new_ids = [aid for act, aid, _ in changes if act == "NEW"]
            if new_ids:
                print(f"\nExtracting metadata for {len(new_ids)} new app(s)...")
                extract_metadata_for_records(apps_dir, records, cfg)
                save_registry(csv_path, records)

    if args.command in ("all", "sync"):
        tag = "[DRY RUN] " if args.dry_run else ""
        print(f"\n{tag}Syncing symlinks and launchers...")
        actions = sync(apps_dir, desktop_dir, records, cfg, dry_run=args.dry_run)
        if actions == 0:
            print("  Everything up to date.")

    counts = {}
    for r in records:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    parts = [f"{counts[s]} {s}" for s in ("active", "ignored", "removed") if counts.get(s, 0)]
    print(f"\n  Summary: {', '.join(parts)}\n")


if __name__ == "__main__":
    main()
