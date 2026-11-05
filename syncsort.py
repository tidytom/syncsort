import os
import sys
import subprocess
from pathlib import Path

# =============================================================================
#  SELF-BOOTSTRAPPING VIRTUAL ENVIRONMENT
# -----------------------------------------------------------------------------
#  On first launch this script builds its own private virtual environment
#  (".venv", next to this file), installs the packages it needs into
#  it, and then re-launches itself using that venv's Python. This keeps the
#  app fully self-contained and never touches your system / global Python
#  (which also avoids "externally-managed-environment" pip errors on modern
#  Linux).
#
#  Set the environment variable SYNCSORT_NO_BOOTSTRAP=1 to skip all of this
#  (e.g. if you already manage the dependencies yourself).
# =============================================================================

# Third-party packages required at runtime (installed into the private venv).
REQUIREMENTS = [
    "send2trash>=1.5.0",
    "Pillow",
    "pymupdf",
]

_VENV_DIR = Path(__file__).resolve().parent / ".venv"
_VENV_ACTIVE_FLAG = "SYNCSORT_VENV_ACTIVE"


def _venv_python(venv_dir):
    """Path to the Python interpreter inside a venv (platform-aware)."""
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _running_inside(venv_dir):
    try:
        return Path(sys.executable).resolve() == _venv_python(venv_dir).resolve()
    except Exception:
        return False


def _install_requirements(python_exe):
    """Install/upgrade REQUIREMENTS into the venv, but only when they change."""
    stamp = _VENV_DIR / ".requirements.lock"
    wanted = "\n".join(REQUIREMENTS)
    if stamp.exists() and stamp.read_text(encoding="utf-8") == wanted:
        return  # already satisfied — skip the slow pip run on every launch
    print("[syncsort] Installing dependencies into the virtual environment...")
    subprocess.check_call([str(python_exe), "-m", "pip", "install", "--upgrade", "pip"])
    subprocess.check_call([str(python_exe), "-m", "pip", "install", "--upgrade", *REQUIREMENTS])
    stamp.write_text(wanted, encoding="utf-8")


def _bootstrap_venv():
    # Explicit opt-out, or we've already re-executed inside the venv.
    if os.environ.get("SYNCSORT_NO_BOOTSTRAP") == "1":
        return
    if os.environ.get(_VENV_ACTIVE_FLAG) == "1":
        return

    venv_py = _venv_python(_VENV_DIR)

    # Create the venv on first run.
    if not venv_py.exists():
        print(f"[syncsort] Creating virtual environment at {_VENV_DIR} ...")
        try:
            import venv
            venv.EnvBuilder(with_pip=True).create(str(_VENV_DIR))
        except Exception as e:
            print(f"[syncsort] Could not create virtual environment: {e}")
            print("[syncsort] Falling back to the current Python interpreter.")
            return

    # Make sure the dependencies are present inside the venv.
    try:
        _install_requirements(venv_py)
    except Exception as e:
        print(f"[syncsort] Dependency install failed: {e}")
        print("[syncsort] Continuing anyway; some features may be unavailable.")

    # If we aren't already running under the venv's Python, re-launch there.
    if not _running_inside(_VENV_DIR):
        env = dict(os.environ)
        env[_VENV_ACTIVE_FLAG] = "1"
        try:
            os.execve(str(venv_py),
                      [str(venv_py), os.path.abspath(__file__), *sys.argv[1:]],
                      env)
        except Exception as e:
            print(f"[syncsort] Could not re-launch inside the venv: {e}")
    else:
        os.environ[_VENV_ACTIVE_FLAG] = "1"


_bootstrap_venv()

# --- Standard library (safe to import after the venv is active) ---
import hashlib
import importlib.util
import threading
import platform
import shutil
import pickle
import logging
try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:
    # tkinter is part of the standard library but ships with the OS Python
    # rather than pip, so a venv cannot install it — the *base* interpreter
    # must provide it. Give the user an actionable message per platform.
    print(
        "\n[syncsort] Tkinter is not available in this Python installation.\n"
        "Tkinter cannot be installed with pip; it comes with the base Python:\n"
        "  - Debian/Ubuntu : sudo apt install python3-tk\n"
        "  - Fedora        : sudo dnf install python3-tkinter\n"
        "  - Windows       : reinstall Python from python.org with the "
        "'tcl/tk' option enabled (it is on by default)\n"
        "Then delete the '.venv' folder and run this script again.\n",
        file=sys.stderr,
    )
    sys.exit(1)
import concurrent.futures
import re
import json
import tempfile
from datetime import datetime

# --- LOGGING ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger(__name__)

class TextHandler(logging.Handler):
    def __init__(self, text_widget, root):
        super().__init__()
        self.text_widget = text_widget
        self.root = root

    def emit(self, record):
        msg = self.format(record) + "\n"
        self.root.after(0, self._append, msg)

    def _append(self, msg):
        self.text_widget.config(state=tk.NORMAL)
        self.text_widget.insert(tk.END, msg)
        self.text_widget.see(tk.END)
        self.text_widget.config(state=tk.DISABLED)

# --- 1. SETUP & UTILS ---
def _version_tuple(v):
    parts = []
    for chunk in str(v).split('.'):
        num = ''.join(c for c in chunk if c.isdigit())
        parts.append(int(num) if num else 0)
    return tuple(parts)

def install_and_import(package, pip_name=None, min_version=None):
    pip_name = pip_name or package
    spec = importlib.util.find_spec(package)

    needs_install = spec is None
    needs_upgrade = False
    if spec is not None and min_version:
        try:
            from importlib.metadata import version as _pkg_version
            installed = _pkg_version(pip_name)
            if _version_tuple(installed) < _version_tuple(min_version):
                needs_upgrade = True
                log.info(f"{pip_name} {installed} is older than {min_version}; upgrading.")
        except Exception as e:
            log.warning(f"Could not determine {pip_name} version: {e}")

    if (needs_install or needs_upgrade) and os.environ.get("SYNCSORT_NO_BOOTSTRAP") == "1":
        # The user chose to manage packages themselves: never run pip.
        log.warning(f"{pip_name} is missing or too old. Install it with: "
                    f"pip install -r requirements.txt")
    elif needs_install or needs_upgrade:
        cmd = [sys.executable, "-m", "pip", "install"]
        if needs_upgrade:
            cmd.append("--upgrade")
        target = f"{pip_name}>={min_version}" if min_version else pip_name
        cmd.append(target)
        try:
            subprocess.check_call(cmd)
            importlib.invalidate_caches()
        except Exception as e:
            log.warning(f"Could not install/upgrade {pip_name}: {e}")

    try:
        return importlib.import_module(package)
    except ImportError as e:
        log.warning(f"Could not import {package}: {e}")
        return None

# Dependencies
send2trash = install_and_import('send2trash', min_version='1.5.0')
PIL = install_and_import('PIL', pip_name='Pillow')
if PIL:
    # Pillow is installed into the private venv at runtime, so static
    # analyzers (Pylance) may not resolve it — silence that false positive.
    from PIL import Image, ImageTk  # type: ignore
fitz = install_and_import('pymupdf')
if fitz is None:
    # Older PyMuPDF releases are imported as "fitz". Never pip-install a
    # package called "fitz": that is an unrelated project.
    try:
        import fitz  # type: ignore
    except ImportError:
        fitz = None

def prepare_path(path):
    path = os.path.normpath(path)
    path = os.path.abspath(path)
    if platform.system() == 'Windows' and len(path) > 259 and not path.startswith('\\\\?\\'):
        path = '\\\\?\\' + path
    return path

def long_path(path):
    path = os.path.abspath(os.path.normpath(str(path)))
    if platform.system() != 'Windows' or path.startswith('\\\\?\\'):
        return path
    if path.startswith('\\\\'):
        return '\\\\?\\UNC\\' + path[2:]
    return '\\\\?\\' + path

def force_rmdir(path):
    lp = long_path(path)
    try:
        os.rmdir(lp)
        return
    except PermissionError:
        pass

    if platform.system() == 'Windows':
        try:
            import ctypes
            FILE_ATTRIBUTE_NORMAL = 0x80
            ctypes.windll.kernel32.SetFileAttributesW(str(lp), FILE_ATTRIBUTE_NORMAL)
        except Exception:
            pass
    else:
        try:
            os.chmod(lp, 0o777)
        except Exception:
            pass

    os.rmdir(lp)

# Hidden/system files that don't count as real content when deciding whether a
# folder is "empty". These are invisible in Explorer, so a folder holding only
# these looks empty to the user even though os.listdir() still reports them.
JUNK_FILENAMES = {
    'thumbs.db', 'ehthumbs.db', 'ehthumbs_vista.db', 'desktop.ini',
    '.ds_store', '.localized', '.apdisk', '.picasa.ini', 'picasa.ini',
    'zbthumbnail.info', 'album.dat', 'albumart.pamp',
}

def _is_junk(name):
    return name.lower() in JUNK_FILENAMES

def dir_is_effectively_empty(path):
    """True if the directory has no subdirectories and no files other than
    ignorable junk (Thumbs.db, desktop.ini, .DS_Store, ...)."""
    try:
        with os.scandir(long_path(path)) as it:
            for entry in it:
                if entry.is_dir(follow_symlinks=False):
                    return False
                if not _is_junk(entry.name):
                    return False
        return True
    except Exception:
        return False

def delete_junk_files(path):
    """Remove ignorable junk files from a directory so it can be rmdir'd.
    Only ever called on directories already deemed effectively empty."""
    try:
        with os.scandir(long_path(path)) as it:
            for entry in it:
                if entry.is_file(follow_symlinks=False) and _is_junk(entry.name):
                    try:
                        os.remove(long_path(entry.path))
                    except Exception as e:
                        log.warning(f"Could not remove junk file {entry.path}: {e}")
    except Exception:
        pass

def _display_path(p):
    """Strip the Windows extended-length prefix for user-facing display."""
    p = str(p)
    if p.startswith('\\\\?\\UNC\\'):
        return '\\\\' + p[8:]
    if p.startswith('\\\\?\\'):
        return p[4:]
    return p

FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def is_link_or_junction(path):
    """True for symlinks, NTFS junctions and other reparse points (OneDrive
    placeholders excluded only when they are not directories). These are
    never walked into, so a shortcut folder that points back into the master
    or into another part of the same tree cannot make a file look like its
    own duplicate."""
    try:
        if os.path.islink(path):
            return True
        isjunction = getattr(os.path, "isjunction", None)
        if isjunction and isjunction(path):
            return True
        st = os.lstat(path)
        attrs = getattr(st, "st_file_attributes", 0)
        return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT) and os.path.isdir(path) \
            and not _is_cloud_placeholder_dir(st)
    except Exception:
        return False


def _is_cloud_placeholder_dir(st):
    # OneDrive / Dropbox "Files On-Demand" folders are reparse points with the
    # cloud tag; they hold real files and must still be scanned.
    tag = getattr(st, "st_reparse_tag", 0)
    return tag != 0 and (tag & 0xFFFF0FFF) == 0x9000001A


def safe_walk(root, topdown=True, onerror=None):
    """os.walk that never descends into symlinked or junction folders.
    Bottom-up order is produced by collecting the top-down walk first, so the
    same pruning applies in both directions."""
    def _walk():
        for dirpath, dirnames, filenames in os.walk(root, topdown=True, onerror=onerror):
            kept = []
            for d in dirnames:
                full = os.path.join(dirpath, d)
                if is_link_or_junction(full):
                    log.info(f"Skipped linked folder (not followed): {_display_path(full)}")
                else:
                    kept.append(d)
            dirnames[:] = kept
            yield dirpath, dirnames, filenames
    if topdown:
        return _walk()
    return reversed(list(_walk()))


def _walk_bottom_up(root):
    """Walk `root` bottom-up, using an extended-length path so deep
    (>260 char) trees on Windows are traversed, never following linked
    folders, and logging — rather than silently swallowing — any directory
    that can't be listed."""
    def _onerror(err):
        log.warning(f"Could not access during scan: "
                    f"{getattr(err, 'filename', '')} ({err})")
    return safe_walk(long_path(root), topdown=False, onerror=_onerror)

def find_empty_dirs(root, protect=None):
    """Return a list of directories under `root` that are effectively empty,
    bottom-up. A directory qualifies when it holds no real files (junk files
    such as Thumbs.db/desktop.ini are ignored) and every one of its
    subdirectories is itself empty — so parents of empty folders are reported
    too. `root` itself is never included. Paths are returned display-friendly
    (without the Windows extended-length prefix)."""
    empty_norm = set()
    empty_paths = []
    root_norm = _norm_for_compare(root)
    for dirpath, dirnames, filenames in _walk_bottom_up(root):
        if _norm_for_compare(dirpath) == root_norm:
            continue
        if protect and protect(dirpath):
            continue
        if any(not _is_junk(f) for f in filenames):
            continue
        if all(_norm_for_compare(os.path.join(dirpath, d)) in empty_norm
               for d in dirnames):
            empty_norm.add(_norm_for_compare(dirpath))
            empty_paths.append(_display_path(dirpath))
    return empty_paths

def remove_empty_dirs_recursive(root, protect=None):
    """Delete every effectively-empty directory under `root` (bottom-up),
    ignoring junk files and repeating until nothing more can be removed so that
    parents which become empty are cleaned too. Never deletes `root` itself.
    Returns the number of folders removed."""
    total = 0
    root_norm = _norm_for_compare(root)
    while True:
        removed_this_pass = 0
        for dirpath, _dirnames, _filenames in _walk_bottom_up(root):
            if _norm_for_compare(dirpath) == root_norm:
                continue
            if protect and protect(dirpath):
                continue
            if not dir_is_effectively_empty(dirpath):
                continue
            try:
                delete_junk_files(dirpath)
                force_rmdir(dirpath)
                removed_this_pass += 1
            except Exception as e:
                log.warning(f"Could not remove empty folder {dirpath}: {e}")
        total += removed_this_pass
        if removed_this_pass == 0:
            break
    return total

def _norm_for_compare(p):
    p = _display_path(p)  # strips both \\?\ and \\?\UNC\ prefixes
    try:
        p = os.path.normpath(os.path.abspath(p))
    except Exception:
        pass
    if platform.system() == 'Windows':
        p = p.lower()
    p = p.rstrip(os.sep).rstrip('/')
    return p

def is_subpath(path, parent):
    if not parent:
        return False
    try:
        path = _norm_for_compare(path)
        parent = _norm_for_compare(parent)
        if not parent:
            return False
        if path == parent:
            return True
        return path.startswith(parent + os.sep)
    except Exception:
        return False

def reveal_in_explorer(path):
    path = prepare_path(path)
    try:
        if platform.system() == 'Windows':
            subprocess.Popen(f'explorer /select,"{path}"')
        else:
            subprocess.call(['xdg-open', os.path.dirname(path)])
    except Exception as e:
        log.warning(f"Could not open explorer for {path}: {e}")

# --- LAPTOP MODE ---
# Laptop mode trades a little speed for less heat and fan noise: fewer worker
# threads and larger read blocks (256 KB), which suit laptop SSDs better and
# reduce system-call overhead. It is switched on with the "Laptop mode"
# checkbox. The quick "Smart" fingerprint always reads 64 KB at each end of a
# file in both modes, so saved scan caches stay compatible either way.
LAPTOP_MODE = threading.Event()
DESKTOP_READ_CHUNK = 65536
LAPTOP_READ_CHUNK = 262144
PARTIAL_HASH_CHUNK = 65536


def read_chunk_size():
    return LAPTOP_READ_CHUNK if LAPTOP_MODE.is_set() else DESKTOP_READ_CHUNK


def scan_thread_count():
    cores = os.cpu_count() or 4
    return cores if LAPTOP_MODE.is_set() else cores + 2


def recommended_copy_threads(cpu_cores, drive_type, laptop):
    if laptop:
        if drive_type == "NVMe SSD":
            return min(24, cpu_cores * 3)
        if drive_type == "SATA SSD":
            return min(16, cpu_cores * 2)
        if drive_type == "HDD":
            return min(8, cpu_cores)
        return min(16, cpu_cores * 2)
    if drive_type == "NVMe SSD":
        return 128
    if drive_type == "SATA SSD":
        return 64
    if drive_type == "HDD":
        return min(16, cpu_cores * 2)
    return cpu_cores * 4


def thread_label(hw, laptop):
    rec = recommended_copy_threads(hw["cpu_cores"], hw["drive_type"], laptop)
    mode = "laptop" if laptop else "desktop"
    return f"Auto ({rec}) — {hw['cpu_cores']} cores, {hw['drive_type']}, {mode}"


def detect_optimal_threads():
    cpu_cores = os.cpu_count() or 4
    drive_type = "Unknown"
    recommended = cpu_cores * 4

    if platform.system() == "Windows":
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-PhysicalDisk | Select-Object MediaType,BusType | ConvertTo-Json"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                disks = json.loads(result.stdout)
                if not isinstance(disks, list):
                    disks = [disks]
                for disk in disks:
                    media = str(disk.get("MediaType", "")).lower()
                    bus = str(disk.get("BusType", "")).lower()
                    if "nvme" in bus or "nvme" in media:
                        drive_type = "NVMe SSD"
                        recommended = 128
                        break
                    elif "ssd" in media or "solid" in media:
                        drive_type = "SATA SSD"
                        recommended = 64
                        break
                    elif "hdd" in media or "unspecified" in media:
                        drive_type = "HDD"
                        recommended = min(16, cpu_cores * 2)
                        break
        except Exception:
            pass

    return {
        "cpu_cores": cpu_cores,
        "drive_type": drive_type,
        "recommended": recommended,
        "label": f"Auto ({recommended}) — {cpu_cores} cores, {drive_type}"
    }

# Google Drive for desktop shows Google Docs, Sheets, Slides and similar as
# small shortcut files with these extensions. They hold a link, not the
# document itself.
GDRIVE_SHORTCUT_EXTENSIONS = ('.gdoc', '.gsheet', '.gslides', '.gdraw', '.gform',
                              '.gmap', '.gsite', '.gjam', '.glink')


def unique_dest_path(path, word):
    """Return `path`, or "name (word N).ext" if a file with that name already
    exists, so nothing at the destination is ever replaced."""
    if not os.path.exists(prepare_path(path)):
        return path
    base, ext = os.path.splitext(path)
    n = 1
    while os.path.exists(prepare_path(f"{base} ({word} {n}){ext}")):
        n += 1
    return f"{base} ({word} {n}){ext}"


IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff', '.ico'}
VIDEO_EXTENSIONS = {'.mp4', '.avi', '.mkv', '.mov', '.wmv', '.flv', '.webm', '.m4v', '.mpg', '.mpeg'}
TEXT_EXTENSIONS = {'.txt', '.csv', '.log', '.json', '.xml', '.html', '.htm', '.css', '.js',
                   '.py', '.md', '.yml', '.yaml', '.ini', '.cfg', '.toml', '.php', '.tsx', '.ts'}

HAS_FFPROBE = shutil.which("ffprobe") is not None
_RESOLUTION_CACHE = {}

def get_resolution(filepath):
    cached = _RESOLUTION_CACHE.get(filepath)
    if cached is not None:
        return cached

    ext = os.path.splitext(filepath)[1].lower()
    res = ""
    if ext in IMAGE_EXTENSIONS and PIL:
        try:
            with Image.open(filepath) as img:
                res = f"{img.width}x{img.height}"
        except Exception:
            res = ""
    elif ext in VIDEO_EXTENSIONS and HAS_FFPROBE:
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", filepath],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                res = result.stdout.strip()
        except Exception:
            res = ""

    _RESOLUTION_CACHE[filepath] = res
    return res

def format_size(size_bytes):
    if size_bytes >= 1024**3:
        return f"{size_bytes / (1024**3):.1f} GB"
    elif size_bytes >= 1024**2:
        return f"{size_bytes / (1024**2):.1f} MB"
    elif size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes} B"

def hash_file_full(filepath, stop_event=None):
    hasher = hashlib.md5()
    chunk = read_chunk_size()
    with open(filepath, 'rb') as f:
        while True:
            if stop_event and stop_event.is_set():
                return None
            data = f.read(chunk)
            if not data:
                break
            hasher.update(data)
    return hasher.hexdigest()

def hash_file_partial(filepath):
    CHUNK = PARTIAL_HASH_CHUNK
    size = os.path.getsize(filepath)
    hasher = hashlib.md5()
    hasher.update(str(size).encode())
    with open(filepath, 'rb') as f:
        hasher.update(f.read(CHUNK))
        if size > CHUNK * 2:
            f.seek(-CHUNK, 2)
            hasher.update(f.read(CHUNK))
    return hasher.hexdigest()

def _same_physical_file(a, b):
    """True if `a` and `b` are the same path, or the same file on disk reached
    by another route (junction, symlink, hard link)."""
    if _norm_for_compare(a) == _norm_for_compare(b):
        return True
    try:
        return os.path.samefile(prepare_path(a), prepare_path(b))
    except Exception:
        return False


def files_are_identical(a, b, chunk=1024 * 1024):
    """True byte-for-byte comparison (no hashing). Returns True only if both
    files are the same size and every byte matches. Used as the final,
    collision-proof gate before deleting or skipping a file."""
    pa, pb = prepare_path(a), prepare_path(b)
    try:
        if os.path.getsize(pa) != os.path.getsize(pb):
            return False
    except Exception:
        return False
    with open(pa, 'rb') as fa, open(pb, 'rb') as fb:
        while True:
            ba = fa.read(chunk)
            bb = fb.read(chunk)
            if ba != bb:
                return False
            if not ba:
                return True

def get_text_content_hash(filepath):
    """Extracts text from PDFs or text files and returns an MD5 hash of the text.
    Returns None if no text is found, or if the file type is not supported."""
    ext = os.path.splitext(filepath)[1].lower()
    text = ""
    try:
        if ext == '.pdf' and fitz:
            with fitz.open(filepath) as doc:
                for page in doc:
                    text += page.get_text()
        elif ext in TEXT_EXTENSIONS:
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                text = f.read()
        else:
            return None # Not a text/pdf file
            
        # Normalize text: strip whitespace and fix line endings so minor 
        # formatting differences don't break the match
        text = text.replace('\r\n', '\n').strip()
        
        if not text:
            return None # No text found (e.g. image-only PDF)
            
        return hashlib.md5(text.encode('utf-8', errors='ignore')).hexdigest()
    except Exception as e:
        log.warning(f"Could not extract text from {filepath}: {e}")
        return None

# --- SAFE CACHE LOADING ---
class _RestrictedUnpickler(pickle.Unpickler):
    """Unpickler that refuses to import any class/function. Scan caches only
    contain plain dicts/lists/tuples/str/int, none of which use GLOBAL
    opcodes, so legitimate caches still load — but a tampered file cannot use
    pickle to execute arbitrary code on load."""
    def find_class(self, module, name):
        raise pickle.UnpicklingError(
            f"Blocked disallowed object '{module}.{name}' in cache file")

def safe_pickle_load(fileobj):
    return _RestrictedUnpickler(fileobj).load()

# --- 2. SCAN MODES ---
SCAN_SMART = 0
SCAN_FULL = 1

# --- MATCH STRATEGIES ---
MATCH_CONTENT = "Exact File Bytes (Hash)"
MATCH_NAME_SIZE = "Same Name & Size"
MATCH_TEXT = "Same Text Content (PDFs/Text)"

# --- 3. LOGIC ENGINE ---
class Comparator(threading.Thread):
    def __init__(self, master_path, target_path, scan_mode, match_strategy, callback_update, callback_finish, callback_alert):
        super().__init__()
        self.master = master_path
        self.target = target_path
        self.scan_mode = scan_mode
        self.match_strategy = match_strategy
        self.update_ui = callback_update
        self.finish = callback_finish
        self.alert = callback_alert
        self.daemon = True
        self.stop_event = threading.Event()

        # For exact-byte matching, the set of file sizes that appear more than
        # once across the folders being compared. Files with a unique size can
        # never have a byte-identical duplicate, so they are not hashed at all.
        # None means "hash everything" (used by the non-byte strategies).
        self.hot_sizes = None

        # Files or folders that could not be read during the scan, as
        # (label, path, error). Shown to the user at the end so a locked file
        # is never silently missing from "Only in Target".
        self.unreadable = []

        try:
            self.max_threads = scan_thread_count()
        except Exception:
            self.max_threads = 4

    def get_identifier(self, filepath, size=None):
        if self.stop_event.is_set():
            return (filepath, None, 0)
        try:
            if size is None:
                size = os.stat(filepath).st_size

            # Strategy 1: Fast Name & Size matching
            if self.match_strategy == MATCH_NAME_SIZE:
                filename = os.path.basename(filepath).lower()
                return (filepath, ('namesize', size, filename), size)

            # Strategy 2: Text Content matching (extracts words from PDF/TXT)
            elif self.match_strategy == MATCH_TEXT:
                text_hash = get_text_content_hash(filepath)
                if text_hash:
                    # Ignore file byte-size in the grouping key so a 10KB txt and 500KB pdf can match
                    return (filepath, ('textcontent', text_hash), size)
                else:
                    # If no text found (image PDF, video, etc), force a unique ID so it never matches anything
                    return (filepath, ('unique', filepath), size)

            # Strategy 3: Exact Byte Content (Default)
            # Skip hashing files whose size is unique across the compared set —
            # a byte-identical duplicate must have the same size, so a
            # unique-size file cannot have one and never needs to be read.
            if self.hot_sizes is not None and size not in self.hot_sizes:
                return (filepath, ('nodupe', filepath), size)

            if self.scan_mode == SCAN_SMART:
                phash = hash_file_partial(filepath)
                return (filepath, ('partial', size, phash), size)
            else:
                fhash = hash_file_full(filepath, self.stop_event)
                if fhash is None:
                    return (filepath, None, size)
                return (filepath, ('full', size, fhash), size)

        except Exception as e:
            log.warning(f"Error processing {filepath}: {e}")
            self.unreadable.append(("", _display_path(filepath), str(e)))
            return (filepath, None, 0)

    def gather_files(self, folder, label):
        """List every file under `folder` with its size (a cheap stat, no
        hashing). Returns a list of (path, size)."""
        self.update_ui(f"Listing files in {label}...", 0)
        files = []

        def _onerror(err):
            self.unreadable.append((label, _display_path(getattr(err, 'filename', '') or ''), str(err)))

        for root, _, names in safe_walk(folder, onerror=_onerror):
            if self.stop_event.is_set():
                return files
            for name in names:
                p = prepare_path(os.path.join(root, name))
                try:
                    files.append((p, os.path.getsize(p)))
                except Exception as e:
                    log.warning(f"Could not stat {p}: {e}")
                    self.unreadable.append((label, _display_path(p), str(e)))
        return files

    def process_files(self, files, label):
        """Build the duplicate index for a pre-listed set of (path, size)."""
        if self.stop_event.is_set():
            return {}
        total = len(files)
        if total == 0:
            return {}

        index = {}
        completed = 0
        self.update_ui(f"Processing {label}: 0/{total}", 0)

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_threads) as executor:
            future_to_file = {executor.submit(self.get_identifier, p, sz): p
                              for (p, sz) in files}

            for future in concurrent.futures.as_completed(future_to_file):
                if self.stop_event.is_set():
                    break

                path, ident, size = future.result()
                if ident:
                    if ident not in index:
                        index[ident] = []
                    index[ident].append((path, size))

                completed += 1
                if completed % 100 == 0 or completed == total:
                    self.update_ui(f"Processing {label}: {completed}/{total}", (completed / total) * 100)

        return index

    def verify_with_full_hash(self, all_indices):
        combined = {}
        for idx in all_indices:
            for ident, entries in idx.items():
                if ident not in combined:
                    combined[ident] = []
                combined[ident].extend(entries)

        needs_verify = {k: v for k, v in combined.items() if len(v) > 1}
        all_files_to_hash = []
        for entries in needs_verify.values():
            all_files_to_hash.extend(entries)

        total = len(all_files_to_hash)
        if total == 0:
            return all_indices

        self.update_ui(f"Verifying {total} candidates with full hash...", 0)

        full_hashes = {}
        completed = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_threads) as executor:
            future_map = {}
            for path, size in all_files_to_hash:
                future_map[executor.submit(hash_file_full, path, self.stop_event)] = (path, size)

            for future in concurrent.futures.as_completed(future_map):
                if self.stop_event.is_set():
                    return all_indices
                path, size = future_map[future]
                try:
                    fhash = future.result()
                    if fhash:
                        full_hashes[path] = fhash
                except Exception as e:
                    log.warning(f"Verify hash failed for {path}: {e}")

                completed += 1
                if completed % 20 == 0 or completed == total:
                    self.update_ui(f"Verifying: {completed}/{total}", (completed / total) * 100)

        new_indices = []
        for idx in all_indices:
            new_idx = {}
            for ident, entries in idx.items():
                if ident in needs_verify:
                    for path, size in entries:
                        if path in full_hashes:
                            new_key = ('full', size, full_hashes[path])
                            if new_key not in new_idx:
                                new_idx[new_key] = []
                            new_idx[new_key].append((path, size))
                        else:
                            # Could not hash this file (locked/unreadable). Keep
                            # it as its own singleton group so it is never
                            # treated as a duplicate, and record why.
                            log.warning(f"Skipping unverifiable file (kept, not "
                                        f"treated as a duplicate): {path}")
                            new_idx[('unverified', path)] = [(path, size)]
                else:
                    new_idx[ident] = entries
            new_indices.append(new_idx)

        return new_indices

    def run(self):
        try:
            # Pass 1: list files with sizes (cheap stat, no hashing).
            target_files = self.gather_files(self.target, "Target")
            if self.stop_event.is_set():
                self.finish({}, {})
                return

            master_files = []
            if self.master:
                master_files = self.gather_files(self.master, "Master")
                if self.stop_event.is_set():
                    self.finish({}, {})
                    return

            # For exact-byte matching, only files whose size is shared by at
            # least one other file (across both folders) can be duplicates, so
            # restrict hashing to those. This skips reading every unique-size
            # file entirely.
            if self.match_strategy == MATCH_CONTENT:
                size_counts = {}
                for _, sz in target_files:
                    size_counts[sz] = size_counts.get(sz, 0) + 1
                for _, sz in master_files:
                    size_counts[sz] = size_counts.get(sz, 0) + 1
                self.hot_sizes = {sz for sz, c in size_counts.items() if c > 1}

            # Pass 2: hash only what is needed and build the indexes.
            target_idx = self.process_files(target_files, "Target")
            if self.stop_event.is_set():
                self.finish({}, {})
                return

            master_idx = {}
            if self.master:
                master_idx = self.process_files(master_files, "Master")
                if self.stop_event.is_set():
                    self.finish({}, {})
                    return

            if self.match_strategy == MATCH_CONTENT and self.scan_mode == SCAN_SMART:
                self.update_ui("Verifying matches with full hash...", 0)
                if self.master:
                    master_idx, target_idx = self.verify_with_full_hash([master_idx, target_idx])
                else:
                    target_idx, = self.verify_with_full_hash([target_idx])

            self.update_ui("Finishing...", 100)
            self.finish(master_idx, target_idx)
        except Exception as e:
            log.error(f"Scan failed: {e}", exc_info=True)
            try:
                self.alert("Scan Error", f"Scan failed:\n{e}")
            except Exception:
                pass


# --- 4. GUI APPLICATION ---
class App:
    def __init__(self, root):
        self.root = root
        self.root.title("SyncSort: Smart Duplicate Finder")
        self.root.geometry("1200x850")

        self.master_path = None
        self.target_path = None
        self.protected_paths = []
        self.cross_dupes = []
        self.internal_dupes = []
        self.only_in_target = []
        self.only_in_master = []
        # Paths (normalized) the user picked with "Keep This Copy" in tab 4.
        self.chosen_keepers = set()

        self.last_master_idx = {}
        self.last_target_idx = {}

        self.mode = tk.IntVar(value=2)

        # --- MENU BAR ---
        menubar = tk.Menu(root)
        cache_menu = tk.Menu(menubar, tearoff=0)
        cache_menu.add_command(label="Save Scan Results to File...", command=self.save_cache)
        cache_menu.add_command(label="Load Scan Results from File...", command=self.load_cache)
        cache_menu.add_separator()
        cache_menu.add_command(label="Clear Current Results", command=self.clear_results)
        menubar.add_cascade(label="Cache / Save", menu=cache_menu)
        root.config(menu=menubar)

        # Header
        tk.Label(root, text="SYNCSORT: SMART DUPLICATE FINDER", font=("Arial", 16, "bold"), pady=10).pack()

        # MODE
        frame_mode = tk.Frame(root)
        frame_mode.pack(pady=5)
        tk.Label(frame_mode, text="Mode:", font=("Arial", 10, "bold")).pack(side=tk.LEFT)
        tk.Radiobutton(frame_mode, text="Compare Two Folders", variable=self.mode, value=1, command=self.toggle_mode).pack(side=tk.LEFT, padx=10)
        tk.Radiobutton(frame_mode, text="Single Folder Cleanup", variable=self.mode, value=2, command=self.toggle_mode).pack(side=tk.LEFT, padx=10)

        # CONFIG
        frame_in = tk.LabelFrame(root, text="Configuration", padx=10, pady=10)
        frame_in.pack(fill=tk.X, padx=10)

        self.lbl_master = tk.Label(frame_in, text="1. PROTECTED Folder:", fg="#d32f2f", font=("Arial", 9, "bold"))
        self.lbl_master.grid(row=0, column=0, sticky="w")
        self.ent_master = tk.Entry(frame_in, width=70, bg="#ffebee")
        self.ent_master.grid(row=0, column=1, padx=5)
        self.btn_master = tk.Button(frame_in, text="Browse...", command=lambda: self.browse(self.ent_master))
        self.btn_master.grid(row=0, column=2)

        self.lbl_target = tk.Label(frame_in, text="2. CLEANUP Folder:", fg="#1976d2", font=("Arial", 9, "bold"))
        self.lbl_target.grid(row=1, column=0, sticky="w", pady=5)
        self.ent_target = tk.Entry(frame_in, width=70, bg="#e3f2fd")
        self.ent_target.grid(row=1, column=1, padx=5, pady=5)
        tk.Button(frame_in, text="Browse...", command=lambda: self.browse(self.ent_target)).grid(row=1, column=2, pady=5)

        # Protected folders panel
        frame_prot = tk.LabelFrame(frame_in, text="Protected Folders (never deleted)", fg="#388e3c", padx=5, pady=5)
        frame_prot.grid(row=2, column=0, columnspan=3, sticky="ew", pady=5)

        self.lst_protected = tk.Listbox(frame_prot, height=3, bg="#e8f5e9", selectmode=tk.EXTENDED)
        self.lst_protected.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 5))

        btn_frame = tk.Frame(frame_prot)
        btn_frame.pack(side=tk.RIGHT)
        tk.Button(btn_frame, text="Add...", width=8, command=self.add_protected).pack(pady=2)
        tk.Button(btn_frame, text="Remove", width=8, command=self.remove_protected).pack(pady=2)

        # SCAN OPTIONS
        frame_act = tk.Frame(root, pady=10)
        frame_act.pack(fill=tk.X, padx=10)

        # Match Strategy Selection
        self.match_strategy = tk.StringVar(value=MATCH_CONTENT)
        strat_frame = tk.LabelFrame(frame_act, text="Match Criteria", padx=8, pady=4)
        strat_frame.pack(side=tk.LEFT, padx=(0, 10))
        self.opt_strat = ttk.Combobox(strat_frame, textvariable=self.match_strategy, values=[MATCH_CONTENT, MATCH_NAME_SIZE, MATCH_TEXT], state="readonly", width=25)
        self.opt_strat.pack(side=tk.LEFT, padx=4)
        self.opt_strat.bind("<<ComboboxSelected>>", self._on_strategy_change)

        self.scan_mode = tk.IntVar(value=SCAN_SMART)
        self.scan_frame = tk.LabelFrame(frame_act, text="Scan Depth", padx=8, pady=4)
        self.scan_frame.pack(side=tk.LEFT)
        tk.Radiobutton(self.scan_frame, text="Smart (recommended)", variable=self.scan_mode, value=SCAN_SMART).pack(side=tk.LEFT, padx=4)
        tk.Radiobutton(self.scan_frame, text="Full (hash everything)", variable=self.scan_mode, value=SCAN_FULL).pack(side=tk.LEFT, padx=4)

        self.permanent_delete = tk.BooleanVar(value=False)
        tk.Checkbutton(frame_act, text="Permanently delete (skip Recycle Bin — no undo)",
                       variable=self.permanent_delete, fg="#d32f2f").pack(side=tk.LEFT, padx=15)

        self.laptop_mode = tk.BooleanVar(value=False)
        tk.Checkbutton(frame_act, text="Laptop mode (fewer threads, less heat)",
                       variable=self.laptop_mode, command=self._on_laptop_mode).pack(side=tk.LEFT, padx=5)

        self.btn_scan = tk.Button(frame_act, text="START SCAN", bg="#4caf50", fg="white", font=("Arial", 10, "bold"), height=2, command=self.start_scan)
        self.btn_scan.pack(side=tk.RIGHT)

        # STATUS
        self.progress = ttk.Progressbar(root, mode='determinate')
        self.progress.pack(fill=tk.X, padx=10, pady=5)
        self.lbl_stat = tk.Label(root, text="Select folders or Load Cache.", fg="gray")
        self.lbl_stat.pack()

        # SUMMARY BAR
        self.lbl_summary = tk.Label(root, text="", font=("Arial", 10, "bold"), fg="#d32f2f")
        self.lbl_summary.pack()

        # TABS
        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        # Tab 1: Cross duplicates
        self.tab_cross = tk.Frame(self.notebook)
        self.notebook.add(self.tab_cross, text="1. Safe to Delete (Matches Master)")
        self.tree_cross = self.create_tree(self.tab_cross, ["File Name", "Size", "Folder", "Status", "Full Path"])

        f_cross_act = tk.Frame(self.tab_cross, pady=5, bg="#eeeeee")
        f_cross_act.pack(fill=tk.X)
        tk.Button(f_cross_act, text="Trash ALL listed here", bg="#ffcdd2", command=self.trash_cross).pack(side=tk.RIGHT, padx=5)
        tk.Button(f_cross_act, text="Move Selected to Folder...", bg="#fff9c4",
                  command=lambda: self.move_selected(self.tree_cross)).pack(side=tk.RIGHT, padx=5)

        # Tab 2: Only in Target
        self.tab_only = tk.Frame(self.notebook)
        self.notebook.add(self.tab_only, text="2. Only in Target (Not in Master)")

        f_only_top = tk.Frame(self.tab_only, pady=5, bg="#fff8e1")
        f_only_top.pack(fill=tk.X)
        tk.Label(f_only_top, text="Files present in the TARGET folder with no matching content in MASTER (grouped by folder).",
                 bg="#fff8e1", fg="#555", font=("Arial", 9)).pack(side=tk.LEFT, padx=8)
        self.btn_sync_master = tk.Button(f_only_top, text="Copy These to Master", bg="#bbdefb",
                                         command=self.sync_to_master)
        self.btn_sync_master.pack(side=tk.RIGHT, padx=5)
        tk.Button(f_only_top, text="Export List...", bg="#c8e6c9", command=self.export_only_in_target).pack(side=tk.RIGHT, padx=5)
        tk.Button(f_only_top, text="Move Selected to Folder...", bg="#fff9c4",
                  command=lambda: self.move_selected(self.tree_only)).pack(side=tk.RIGHT, padx=5)

        only_frame = tk.Frame(self.tab_only)
        only_frame.pack(fill=tk.BOTH, expand=True)
        self.tree_only = ttk.Treeview(
            only_frame,
            columns=("name", "size", "resolution", "folder", "path"),
            show="tree headings",
            selectmode="extended"
        )
        self.tree_only.heading("name", text="File Name")
        self.tree_only.heading("size", text="Size")
        self.tree_only.heading("resolution", text="Resolution")
        self.tree_only.heading("folder", text="Folder")
        self.tree_only.heading("path", text="Full Path")
        self.tree_only.column("#0", width=30)
        self.tree_only.column("name", width=250)
        self.tree_only.column("size", width=80)
        self.tree_only.column("resolution", width=100)
        self.tree_only.column("folder", width=250)
        self.tree_only.column("path", width=0, stretch=False)
        scroll_only = ttk.Scrollbar(only_frame, orient="vertical", command=self.tree_only.yview)
        self.tree_only.configure(yscrollcommand=scroll_only.set)
        self.tree_only.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll_only.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree_only.bind("<Double-1>", self.on_double_click)
        self.tree_only.tag_configure("folderhdr", foreground="#e65100", font=("Arial", 9, "bold"))

        # Tab 3: Only in Master (read-only list)
        self.tab_only_master = tk.Frame(self.notebook)
        self.notebook.add(self.tab_only_master, text="3. Only in Master")

        f_om_top = tk.Frame(self.tab_only_master, pady=5, bg="#e8f5e9")
        f_om_top.pack(fill=tk.X)
        tk.Label(f_om_top, text="Files present in the MASTER folder with no matching content in TARGET (grouped by folder). "
                 "This list is read-only.",
                 bg="#e8f5e9", fg="#555", font=("Arial", 9)).pack(side=tk.LEFT, padx=8)
        tk.Button(f_om_top, text="Export List...", bg="#c8e6c9",
                  command=self.export_only_in_master).pack(side=tk.RIGHT, padx=5)

        om_frame = tk.Frame(self.tab_only_master)
        om_frame.pack(fill=tk.BOTH, expand=True)
        self.tree_only_master = ttk.Treeview(
            om_frame,
            columns=("name", "size", "resolution", "folder", "path"),
            show="tree headings",
            selectmode="extended"
        )
        self.tree_only_master.heading("name", text="File Name")
        self.tree_only_master.heading("size", text="Size")
        self.tree_only_master.heading("resolution", text="Resolution")
        self.tree_only_master.heading("folder", text="Folder")
        self.tree_only_master.heading("path", text="Full Path")
        self.tree_only_master.column("#0", width=30)
        self.tree_only_master.column("name", width=250)
        self.tree_only_master.column("size", width=80)
        self.tree_only_master.column("resolution", width=100)
        self.tree_only_master.column("folder", width=250)
        self.tree_only_master.column("path", width=0, stretch=False)
        scroll_om = ttk.Scrollbar(om_frame, orient="vertical", command=self.tree_only_master.yview)
        self.tree_only_master.configure(yscrollcommand=scroll_om.set)
        self.tree_only_master.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll_om.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree_only_master.bind("<Double-1>", self.on_double_click)
        self.tree_only_master.tag_configure("folderhdr", foreground="#2e7d32", font=("Arial", 9, "bold"))

        # Tab 4: Internal duplicates
        self.tab_internal = tk.Frame(self.notebook)
        self.notebook.add(self.tab_internal, text="4. Internal Duplicates (Clean Single Folder)")

        f_int_act = tk.Frame(self.tab_internal, pady=5, bg="#e3f2fd")
        f_int_act.pack(fill=tk.X, side=tk.BOTTOM)
        tk.Button(f_int_act, text="Trash Selected", bg="#ffcdd2", command=self.delete_selected_internal).pack(side=tk.RIGHT, padx=5)
        tk.Button(f_int_act, text="Auto-Keep Best Path", bg="#bbdefb", command=self.auto_cull_internal).pack(side=tk.RIGHT, padx=5)
        tk.Button(f_int_act, text="Select All Except Best", bg="#c8e6c9", command=self.select_all_except_best).pack(side=tk.RIGHT, padx=5)
        tk.Button(f_int_act, text="Move Selected to Folder...", bg="#fff9c4",
                  command=lambda: self.move_selected(self.tree_internal)).pack(side=tk.LEFT, padx=5)
        tk.Button(f_int_act, text="Keep This Copy", bg="#c8e6c9",
                  command=self.keep_this_copy).pack(side=tk.LEFT, padx=5)
        tk.Button(f_int_act, text="Select Duplicates in This Folder...", bg="#e1bee7",
                  command=self.open_select_folder_dialog).pack(side=tk.LEFT, padx=5)

        paned = ttk.PanedWindow(self.tab_internal, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True)

        tree_frame = tk.Frame(paned)
        self.tree_internal = ttk.Treeview(
            tree_frame,
            columns=("name", "size", "resolution", "folder", "path"),
            show="tree headings",
            selectmode="extended"
        )
        self.tree_internal.heading("name", text="File Name")
        self.tree_internal.heading("size", text="Size")
        self.tree_internal.heading("resolution", text="Resolution")
        self.tree_internal.heading("folder", text="Folder")
        self.tree_internal.heading("path", text="Full Path")
        self.tree_internal.column("#0", width=30)
        self.tree_internal.column("name", width=250)
        self.tree_internal.column("size", width=80)
        self.tree_internal.column("resolution", width=100)
        self.tree_internal.column("folder", width=200)
        self.tree_internal.column("path", width=0, stretch=False)

        scroll_y = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree_internal.yview)
        self.tree_internal.configure(yscrollcommand=scroll_y.set)
        self.tree_internal.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll_y.pack(side=tk.RIGHT, fill=tk.Y)

        self.tree_internal.bind("<Double-1>", self.on_double_click)
        self.tree_internal.bind("<<TreeviewSelect>>", self._on_preview_select)

        paned.add(tree_frame, weight=3)

        # Preview Frame
        self.preview_frame = tk.LabelFrame(paned, text="Preview", padx=5, pady=5)
        self.preview_image_label = tk.Label(self.preview_frame, text="Select a file to preview", fg="gray", bg="#f5f5f5")
        self.preview_image_label.pack(fill=tk.BOTH, expand=True)

        self.preview_info = tk.Label(self.preview_frame, text="", font=("Arial", 8), fg="#555", wraplength=350, justify=tk.LEFT)
        self.preview_info.pack(fill=tk.X, pady=(5, 0))

        self.preview_text = tk.Text(self.preview_frame, height=8, state=tk.DISABLED, bg="#fafafa", font=("Consolas", 9), wrap=tk.WORD)
        self.preview_text.pack(fill=tk.BOTH, expand=True, pady=(5, 0))
        self.preview_text.pack_forget()

        self.btn_preview_open = tk.Button(self.preview_frame, text="Open File", command=self._open_preview_file, state=tk.DISABLED)
        self.btn_preview_open.pack(pady=(5, 0))

        self._preview_photo = None
        self._preview_path = None

        paned.add(self.preview_frame, weight=1)

        # Tab 5: Copy / Move
        self.tab_copy = tk.Frame(self.notebook)
        self.notebook.add(self.tab_copy, text="5. Copy / Move Folder")

        frame_copy = tk.LabelFrame(self.tab_copy, text="Robocopy Folder Transfer", padx=15, pady=15)
        frame_copy.pack(fill=tk.X, padx=10, pady=10)

        tk.Label(frame_copy, text="Source Folder:", font=("Arial", 9, "bold")).grid(row=0, column=0, sticky="w")
        self.ent_copy_src = tk.Entry(frame_copy, width=70)
        self.ent_copy_src.grid(row=0, column=1, padx=5, pady=5)
        tk.Button(frame_copy, text="Browse...", command=lambda: self.browse(self.ent_copy_src)).grid(row=0, column=2, pady=5)

        tk.Label(frame_copy, text="Destination Folder:", font=("Arial", 9, "bold")).grid(row=1, column=0, sticky="w")
        self.ent_copy_dst = tk.Entry(frame_copy, width=70)
        self.ent_copy_dst.grid(row=1, column=1, padx=5, pady=5)
        tk.Button(frame_copy, text="Browse...", command=lambda: self.browse(self.ent_copy_dst)).grid(row=1, column=2, pady=5)

        self.copy_mode = tk.IntVar(value=0)
        mode_frame = tk.Frame(frame_copy)
        mode_frame.grid(row=2, column=0, columnspan=3, pady=5)
        tk.Radiobutton(mode_frame, text="Copy (keep source)", variable=self.copy_mode, value=0).pack(side=tk.LEFT, padx=10)
        tk.Radiobutton(mode_frame, text="Move (delete source after)", variable=self.copy_mode, value=1).pack(side=tk.LEFT, padx=10)
        # Off by default: files that already exist in the destination are left
        # alone, so a newer edited copy there is never replaced by an older one.
        self.copy_overwrite = tk.BooleanVar(value=False)
        tk.Checkbutton(mode_frame, text="Replace files that already exist in the destination",
                       variable=self.copy_overwrite, fg="#d32f2f").pack(side=tk.LEFT, padx=10)

        options_frame = tk.Frame(frame_copy)
        options_frame.grid(row=3, column=0, columnspan=3, pady=5)

        tk.Label(options_frame, text="Engine:", font=("Arial", 9, "bold")).pack(side=tk.LEFT, padx=(0, 5))
        self.copy_engine = ttk.Combobox(options_frame, values=["Robocopy (fast, Windows)", "Python (cross-platform)"],
                                        state="readonly", width=30)
        self.copy_engine.current(0)
        self.copy_engine.pack(side=tk.LEFT)

        tk.Label(options_frame, text="   Threads:", font=("Arial", 9, "bold")).pack(side=tk.LEFT, padx=(10, 5))
        self.hw_info = detect_optimal_threads()
        thread_values = [thread_label(self.hw_info, False), "8", "16", "32", "64", "128"]
        self.copy_threads = ttk.Combobox(options_frame, values=thread_values, state="readonly", width=35)
        self.copy_threads.current(0)
        self.copy_threads.pack(side=tk.LEFT)

        btn_frame_copy = tk.Frame(frame_copy)
        btn_frame_copy.grid(row=4, column=0, columnspan=3, pady=10)
        self.btn_copy_start = tk.Button(btn_frame_copy, text="START", bg="#4caf50", fg="white",
                                        font=("Arial", 10, "bold"), width=15, height=2, command=self.start_copy)
        self.btn_copy_start.pack(side=tk.LEFT, padx=5)
        self.btn_copy_cancel = tk.Button(btn_frame_copy, text="CANCEL", bg="#f44336", fg="white",
                                          font=("Arial", 10, "bold"), width=15, height=2,
                                          command=self.cancel_copy, state=tk.DISABLED)
        self.btn_copy_cancel.pack(side=tk.LEFT, padx=5)

        self.copy_stop_event = threading.Event()
        self.copy_process = None

        self.lbl_copy_stat = tk.Label(self.tab_copy, text="", fg="gray")
        self.lbl_copy_stat.pack(pady=5)

        self.txt_copy_log = tk.Text(self.tab_copy, height=15, state=tk.DISABLED, bg="#f5f5f5")
        scroll_copy = ttk.Scrollbar(self.tab_copy, orient="vertical", command=self.txt_copy_log.yview)
        self.txt_copy_log.configure(yscrollcommand=scroll_copy.set)
        self.txt_copy_log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10, 0), pady=5)
        scroll_copy.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 10), pady=5)

        # Tab 6: Unzip
        self.tab_unzip = tk.Frame(self.notebook)
        self.notebook.add(self.tab_unzip, text="6. Unzip")

        frame_unzip_top = tk.LabelFrame(self.tab_unzip, text="Select Zip Files", padx=10, pady=10)
        frame_unzip_top.pack(fill=tk.X, padx=10, pady=10)

        browse_frame = tk.Frame(frame_unzip_top)
        browse_frame.pack(fill=tk.X)
        tk.Button(browse_frame, text="Add Zip Files...", command=self.unzip_add_files).pack(side=tk.LEFT, padx=5)
        tk.Button(browse_frame, text="Add Folder of Zips...", command=self.unzip_add_folder).pack(side=tk.LEFT, padx=5)
        tk.Button(browse_frame, text="Remove Selected", command=self.unzip_remove_selected).pack(side=tk.LEFT, padx=5)
        tk.Button(browse_frame, text="Clear All", command=self.unzip_clear).pack(side=tk.LEFT, padx=5)

        self.lst_unzip = tk.Listbox(frame_unzip_top, height=8, selectmode=tk.EXTENDED, bg="#fff3e0")
        scroll_unzip_list = ttk.Scrollbar(frame_unzip_top, orient="vertical", command=self.lst_unzip.yview)
        self.lst_unzip.configure(yscrollcommand=scroll_unzip_list.set)
        self.lst_unzip.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, pady=(5, 0))
        scroll_unzip_list.pack(side=tk.RIGHT, fill=tk.Y, pady=(5, 0))

        frame_unzip_opts = tk.Frame(self.tab_unzip)
        frame_unzip_opts.pack(fill=tk.X, padx=10, pady=5)

        tk.Label(frame_unzip_opts, text="Engine:", font=("Arial", 9, "bold")).pack(side=tk.LEFT, padx=(0, 5))
        self.unzip_engine = ttk.Combobox(frame_unzip_opts,
                                          values=["Windows tar (recommended)", "Explorer (small zips only)"],
                                          state="readonly", width=30)
        self.unzip_engine.current(0)
        self.unzip_engine.pack(side=tk.LEFT, padx=(0, 15))

        self.unzip_overwrite = tk.BooleanVar(value=False)
        tk.Checkbutton(frame_unzip_opts, text="Overwrite existing files", variable=self.unzip_overwrite).pack(side=tk.LEFT, padx=5)

        self.unzip_subfolder = tk.BooleanVar(value=True)
        self.chk_subfolder = tk.Checkbutton(frame_unzip_opts, text="Extract into subfolder (zip name)",
                                            variable=self.unzip_subfolder)
        self.chk_subfolder.pack(side=tk.LEFT, padx=5)

        self.unzip_merge = tk.BooleanVar(value=False)
        self.chk_merge = tk.Checkbutton(frame_unzip_opts, text="Merge all into one folder (Takeout mode)",
                                        variable=self.unzip_merge, fg="#1976d2",
                                        command=self._toggle_merge_mode)
        self.chk_merge.pack(side=tk.LEFT, padx=5)

        btn_unzip_frame = tk.Frame(self.tab_unzip)
        btn_unzip_frame.pack(pady=5)
        self.btn_unzip_start = tk.Button(btn_unzip_frame, text="UNZIP ALL", bg="#ff9800", fg="white",
                                          font=("Arial", 10, "bold"), width=15, height=2, command=self.start_unzip)
        self.btn_unzip_start.pack(side=tk.LEFT, padx=5)

        self.lbl_unzip_stat = tk.Label(self.tab_unzip, text="", fg="gray")
        self.lbl_unzip_stat.pack(pady=3)

        self.txt_unzip_log = tk.Text(self.tab_unzip, height=10, state=tk.DISABLED, bg="#f5f5f5")
        scroll_unzip_log = ttk.Scrollbar(self.tab_unzip, orient="vertical", command=self.txt_unzip_log.yview)
        self.txt_unzip_log.configure(yscrollcommand=scroll_unzip_log.set)
        self.txt_unzip_log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10, 0), pady=5)
        scroll_unzip_log.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 10), pady=5)

        # Tab 7: Empty Folder Remover
        self.tab_empty = tk.Frame(self.notebook)
        self.notebook.add(self.tab_empty, text="7. Empty Folders")

        frame_empty_top = tk.LabelFrame(self.tab_empty, text="Scan for Empty Folders", padx=15, pady=10)
        frame_empty_top.pack(fill=tk.X, padx=10, pady=10)

        tk.Label(frame_empty_top, text="Folder to scan:", font=("Arial", 9, "bold")).grid(row=0, column=0, sticky="w")
        self.ent_empty_path = tk.Entry(frame_empty_top, width=70)
        self.ent_empty_path.grid(row=0, column=1, padx=5, pady=5)
        tk.Button(frame_empty_top, text="Browse...", command=lambda: self.browse(self.ent_empty_path)).grid(row=0, column=2, pady=5)

        btn_empty_frame = tk.Frame(frame_empty_top)
        btn_empty_frame.grid(row=1, column=0, columnspan=3, pady=5)
        self.btn_empty_scan = tk.Button(btn_empty_frame, text="SCAN", bg="#4caf50", fg="white",
                                         font=("Arial", 10, "bold"), width=12, command=self.scan_empty_folders)
        self.btn_empty_scan.pack(side=tk.LEFT, padx=5)
        self.btn_empty_delete = tk.Button(btn_empty_frame, text="DELETE SELECTED", bg="#f44336", fg="white",
                                           font=("Arial", 10, "bold"), width=15, command=self.delete_empty_folders,
                                           state=tk.DISABLED)
        self.btn_empty_delete.pack(side=tk.LEFT, padx=5)
        self.btn_empty_select_all = tk.Button(btn_empty_frame, text="SELECT ALL", bg="#bbdefb",
                                               font=("Arial", 10, "bold"), width=12, command=self.select_all_empty,
                                               state=tk.DISABLED)
        self.btn_empty_select_all.pack(side=tk.LEFT, padx=5)

        self.lbl_empty_stat = tk.Label(self.tab_empty, text="", fg="gray")
        self.lbl_empty_stat.pack(pady=3)

        self.tree_empty = ttk.Treeview(self.tab_empty, columns=("path",), show="headings", selectmode="extended")
        self.tree_empty.heading("path", text="Empty Folder Path")
        self.tree_empty.column("path", width=800)
        scroll_empty = ttk.Scrollbar(self.tab_empty, orient="vertical", command=self.tree_empty.yview)
        self.tree_empty.configure(yscrollcommand=scroll_empty.set)
        self.tree_empty.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10, 0), pady=5)
        scroll_empty.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 10), pady=5)

        self._build_gdrive_tab()

        # Tab 9: Log
        self.tab_log = tk.Frame(self.notebook)
        self.notebook.add(self.tab_log, text="9. Log")

        log_btn_frame = tk.Frame(self.tab_log)
        log_btn_frame.pack(fill=tk.X, padx=10, pady=5)
        tk.Button(log_btn_frame, text="Clear Log", command=self.clear_log).pack(side=tk.RIGHT, padx=5)

        self.txt_log = tk.Text(self.tab_log, state=tk.DISABLED, bg="#fafafa", font=("Consolas", 9))
        scroll_log = ttk.Scrollbar(self.tab_log, orient="vertical", command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=scroll_log.set)
        self.txt_log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10, 0), pady=5)
        scroll_log.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 10), pady=5)

        handler = TextHandler(self.txt_log, root)
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s: %(message)s', datefmt='%H:%M:%S'))
        handler.setLevel(logging.INFO)
        log.addHandler(handler)
        log.setLevel(logging.INFO)

        log.info("Application started.")
        self.toggle_mode()

    def _on_strategy_change(self, event=None):
        """Grays out or reveals Scan Depth options based on selected strategy."""
        if self.match_strategy.get() in (MATCH_NAME_SIZE, MATCH_TEXT):
            for child in self.scan_frame.winfo_children():
                child.configure(state='disabled')
        else:
            for child in self.scan_frame.winfo_children():
                child.configure(state='normal')

    def toggle_mode(self):
        m = self.mode.get()
        if m == 1:
            self.ent_master.config(state='normal')
            self.btn_master.config(state='normal')
            self.lbl_master.config(fg="#d32f2f")
            self.notebook.tab(self.tab_cross, state='normal')
            self.notebook.tab(self.tab_only, state='normal')
            self.notebook.tab(self.tab_only_master, state='normal')
        else:
            self.ent_master.delete(0, tk.END)
            self.ent_master.config(state='disabled')
            self.btn_master.config(state='disabled')
            self.lbl_master.config(fg="gray")
            self.notebook.tab(self.tab_cross, state='disabled')
            self.notebook.tab(self.tab_only, state='disabled')
            self.notebook.tab(self.tab_only_master, state='disabled')
            self.notebook.select(self.tab_internal)

    def create_tree(self, parent, cols):
        tree = ttk.Treeview(parent, columns=cols, show="headings")
        for c in cols:
            tree.heading(c, text=c)
        tree.column(cols[0], width=250)
        tree.column(cols[1], width=80)
        tree.column(cols[-1], width=0, stretch=False)
        scroll = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        tree.bind("<Double-1>", self.on_double_click)
        return tree

    def browse(self, entry):
        d = filedialog.askdirectory()
        if d:
            entry.delete(0, tk.END)
            entry.insert(0, prepare_path(d))

    def _on_laptop_mode(self):
        """Switch laptop mode on or off. It applies to the next scan and copy:
        scans use fewer threads and 256 KB reads, and the Copy tab's "Auto"
        thread count is lowered."""
        laptop = bool(self.laptop_mode.get())
        if laptop:
            LAPTOP_MODE.set()
        else:
            LAPTOP_MODE.clear()
        label = thread_label(self.hw_info, laptop)
        values = list(self.copy_threads.cget("values"))
        was_auto = self.copy_threads.get().startswith("Auto")
        values[0] = label
        self.copy_threads.configure(values=values)
        if was_auto:
            self.copy_threads.current(0)
        log.info(f"Laptop mode {'on' if laptop else 'off'}: scan threads {scan_thread_count()}, "
                 f"read block {read_chunk_size() // 1024} KB, copy {label}")

    def _get_thread_count(self):
        val = self.copy_threads.get()
        if val.startswith("Auto"):
            return recommended_copy_threads(self.hw_info["cpu_cores"], self.hw_info["drive_type"],
                                            LAPTOP_MODE.is_set())
        try:
            return int(val)
        except ValueError:
            return 64

    def start_copy(self):
        src = self.ent_copy_src.get().strip()
        dst = self.ent_copy_dst.get().strip()
        if not src or not os.path.isdir(src):
            return messagebox.showerror("Error", "Select a valid source folder.")
        if not dst:
            return messagebox.showerror("Error", "Select a destination folder.")

        move = self.copy_mode.get() == 1
        action = "Move" if move else "Copy"
        engine = self.copy_engine.get()
        threads = self._get_thread_count()
        overwrite = bool(self.copy_overwrite.get())
        existing = ("Files that already exist in the destination WILL BE REPLACED."
                    if overwrite else
                    "Files that already exist in the destination are skipped (kept as they are).")

        if not messagebox.askyesno("Confirm", f"{action} everything from:\n{src}\n\nTo:\n{dst}\n\nEngine: {engine}\nThreads: {threads}\n\n{existing}"):
            return

        self.copy_stop_event.clear()
        self.btn_copy_start.config(state=tk.DISABLED)
        self.btn_copy_cancel.config(state=tk.NORMAL)
        self.lbl_copy_stat.config(text=f"{action} in progress...", fg="blue")
        self.txt_copy_log.config(state=tk.NORMAL)
        self.txt_copy_log.delete("1.0", tk.END)
        self.txt_copy_log.config(state=tk.DISABLED)

        if engine.startswith("Robocopy"):
            threading.Thread(target=self._run_robocopy, args=(src, dst, move, action, threads, overwrite), daemon=True).start()
        else:
            threading.Thread(target=self._run_python_copy, args=(src, dst, move, action, threads, overwrite), daemon=True).start()

    def cancel_copy(self):
        self.copy_stop_event.set()
        if self.copy_process:
            try:
                self.copy_process.terminate()
            except Exception:
                pass
        self.lbl_copy_stat.config(text="Cancelled.", fg="red")
        self.btn_copy_start.config(state=tk.NORMAL)
        self.btn_copy_cancel.config(state=tk.DISABLED)

    def _copy_finished(self, msg, color):
        self.lbl_copy_stat.config(text=msg, fg=color)
        self.btn_copy_start.config(state=tk.NORMAL)
        self.btn_copy_cancel.config(state=tk.DISABLED)
        self.copy_process = None

    def _run_robocopy(self, src, dst, move, action, threads, overwrite=False):
        log.info(f"Robocopy {action}: {src} → {dst} ({threads} threads, replace existing: {overwrite})")
        cmd = ["robocopy", src, dst, "/E", f"/MT:{threads}", "/R:1", "/W:1", "/NP"]
        if not overwrite:
            # Skip files that exist in the destination whether they are
            # changed, newer or older. With /MOVE, skipped files stay in the
            # source, so nothing is lost.
            cmd += ["/XC", "/XN", "/XO"]
        if move:
            cmd.append("/MOVE")
        try:
            self.copy_process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                                  text=True, encoding="utf-8", errors="replace")
            for line in self.copy_process.stdout:
                if self.copy_stop_event.is_set():
                    self.copy_process.terminate()
                    self.root.after(0, self._append_copy_log, "\n--- CANCELLED ---\n")
                    return
                self.root.after(0, self._append_copy_log, line)
            self.copy_process.wait()
            code = self.copy_process.returncode
            if self.copy_stop_event.is_set():
                # Cancel terminates robocopy, which then exits with a code that
                # looks like success. Report it as cancelled instead.
                self.root.after(0, self._append_copy_log, "\n--- CANCELLED ---\n")
                self.root.after(0, self._copy_finished, f"{action} cancelled. Some files may already have been copied.", "red")
                return
            if code <= 3:
                msg = f"{action} completed successfully."
                color = "green"
            elif code <= 7:
                msg = f"{action} completed with some mismatches or extra files (code {code})."
                color = "orange"
            else:
                msg = f"{action} had errors (code {code})."
                color = "red"
        except Exception as e:
            msg = f"Error: {e}"
            color = "red"
        self.root.after(0, self._copy_finished, msg, color)

    def _copy_single_file(self, args):
        src_file, dst_file, move, overwrite = args
        if self.copy_stop_event.is_set():
            return (src_file, False, "cancelled")
        try:
            if not overwrite and os.path.exists(prepare_path(dst_file)):
                return (src_file, None, "exists")
            os.makedirs(os.path.dirname(dst_file), exist_ok=True)
            if move:
                shutil.move(src_file, dst_file)
            else:
                shutil.copy2(src_file, dst_file)
            return (src_file, True, None)
        except Exception as e:
            return (src_file, False, str(e))

    def _run_python_copy(self, src, dst, move, action, threads, overwrite=False):
        log.info(f"Python {action}: {src} → {dst} ({threads} threads, replace existing: {overwrite})")
        copied = 0
        failed = 0
        skipped = 0
        total = 0
        cancelled = False
        msg = ""
        color = "red"
        try:
            self.root.after(0, self._append_copy_log, "Listing files...\n")
            file_pairs = []
            for root, _dirs, files in safe_walk(src):
                rel = os.path.relpath(root, src)
                dest_dir = os.path.join(dst, rel)
                for f in files:
                    file_pairs.append((os.path.join(root, f), os.path.join(dest_dir, f), move, overwrite))

            total = len(file_pairs)
            workers = min(threads, total) if total > 0 else 1
            self.root.after(0, self._append_copy_log, f"Found {total} files. {action} with {workers} threads...\n\n")
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {executor.submit(self._copy_single_file, pair): pair for pair in file_pairs}
                for future in concurrent.futures.as_completed(futures):
                    if self.copy_stop_event.is_set():
                        executor.shutdown(wait=False, cancel_futures=True)
                        cancelled = True
                        break

                    path, success, err = future.result()
                    if success:
                        copied += 1
                    elif success is None:
                        skipped += 1
                    else:
                        failed += 1
                        self.root.after(0, self._append_copy_log, f"FAILED: {path} — {err}\n")

                    if copied % 100 == 0 and copied > 0:
                        self.root.after(0, self._append_copy_log, f"{action}: {copied}/{total} files...\n")

            if cancelled:
                self.root.after(0, self._append_copy_log, f"\n--- CANCELLED after {copied} files ---\n")
                msg = f"Cancelled. {copied} files copied before stop."
                color = "red"
            else:
                if move:
                    for root, _dirs, _files in safe_walk(src, topdown=False):
                        try:
                            if not os.listdir(root):
                                force_rmdir(root)
                        except Exception:
                            pass
                msg = f"{action} complete. {copied}/{total} files."
                if skipped:
                    msg += f" {skipped} skipped (already in destination)."
                if failed:
                    msg += f" {failed} failed."
                color = "green" if failed == 0 else "orange"
                self.root.after(0, self._append_copy_log, f"\n{msg}\n")
        except Exception as e:
            log.error(f"Python copy failed: {e}", exc_info=True)
            msg = f"Error: {e}"
            color = "red"
            self.root.after(0, self._append_copy_log, f"\nERROR: {e}\n")
        finally:
            self.root.after(0, self._copy_finished, msg or "Done.", color)

    def _append_copy_log(self, line):
        self.txt_copy_log.config(state=tk.NORMAL)
        self.txt_copy_log.insert(tk.END, line)
        self.txt_copy_log.see(tk.END)
        self.txt_copy_log.config(state=tk.DISABLED)

    # --- UNZIP ---
    def unzip_add_files(self):
        files = filedialog.askopenfilenames(title="Select Zip Files",
                                            filetypes=[("Zip files", "*.zip"), ("All files", "*.*")])
        for f in files:
            p = prepare_path(f)
            current = list(self.lst_unzip.get(0, tk.END))
            if p not in current:
                self.lst_unzip.insert(tk.END, p)

    def unzip_add_folder(self):
        d = filedialog.askdirectory(title="Select folder containing zip files")
        if d:
            current = list(self.lst_unzip.get(0, tk.END))
            for z in sorted(Path(d).glob("*.zip")):
                p = prepare_path(str(z))
                if p not in current:
                    self.lst_unzip.insert(tk.END, p)

    def unzip_remove_selected(self):
        for i in reversed(self.lst_unzip.curselection()):
            self.lst_unzip.delete(i)

    def unzip_clear(self):
        self.lst_unzip.delete(0, tk.END)

    def clear_log(self):
        self.txt_log.config(state=tk.NORMAL)
        self.txt_log.delete("1.0", tk.END)
        self.txt_log.config(state=tk.DISABLED)

    # --- PREVIEW ---
    def _on_preview_select(self, event=None):
        sel = self.tree_internal.selection()
        if not sel:
            return
        vals = self.tree_internal.item(sel[0], "values")
        path = vals[-1] if vals else ""
        if not path or not os.path.isfile(path):
            self._clear_preview()
            return
        self._preview_path = path
        self.btn_preview_open.config(state=tk.NORMAL)

        ext = os.path.splitext(path)[1].lower()
        size = os.path.getsize(path)
        modified = os.path.getmtime(path)
        mod_str = datetime.fromtimestamp(modified).strftime('%Y-%m-%d %H:%M')
        self.preview_info.config(text=f"{os.path.basename(path)}\n{format_size(size)}  |  {mod_str}\n{path}")

        if ext in IMAGE_EXTENSIONS and PIL:
            self._show_image_preview(path)
        elif ext == '.pdf':
            self._show_pdf_preview(path)
        elif ext in VIDEO_EXTENSIONS:
            self._show_video_preview(path)
        elif ext in TEXT_EXTENSIONS:
            self._show_text_preview(path)
        else:
            self._show_generic_preview(ext)

    def _clear_preview(self):
        self._preview_photo = None
        self._preview_path = None
        self.preview_text.pack_forget()
        self.preview_image_label.pack(fill=tk.BOTH, expand=True)
        self.preview_image_label.config(image='', text="Select a file to preview", fg="gray")
        self.preview_info.config(text="")
        self.btn_preview_open.config(state=tk.DISABLED)

    def _show_image_preview(self, path):
        self.preview_text.pack_forget()
        self.preview_image_label.pack(fill=tk.BOTH, expand=True)
        try:
            img = Image.open(path)
            img.thumbnail((400, 400), Image.LANCZOS)
            self._preview_photo = ImageTk.PhotoImage(img)
            self.preview_image_label.config(image=self._preview_photo, text="")
        except Exception as e:
            self.preview_image_label.config(image='', text=f"Could not load image:\n{e}", fg="red")

    def _show_pdf_preview(self, path):
        self.preview_text.pack_forget()
        self.preview_image_label.pack(fill=tk.BOTH, expand=True)
        if not fitz or not PIL:
            self.preview_image_label.config(image='', text="PDF preview requires\npymupdf + Pillow\n\nUse 'Open File' to view", fg="gray")
            return
        doc = None
        try:
            doc = fitz.open(path)
            page = doc[0]
            mat = fitz.Matrix(2, 2)
            pix = page.get_pixmap(matrix=mat)
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            img.thumbnail((400, 500), Image.LANCZOS)
            self._preview_photo = ImageTk.PhotoImage(img)
            self.preview_image_label.config(image=self._preview_photo, text="")
        except Exception as e:
            self.preview_image_label.config(image='', text=f"Could not render PDF:\n{e}", fg="red")
        finally:
            if doc is not None:
                try:
                    doc.close()
                except Exception:
                    pass

    def _show_video_preview(self, path):
        self.preview_text.pack_forget()
        self.preview_image_label.pack(fill=tk.BOTH, expand=True)
        self.preview_image_label.config(image='', text="VIDEO FILE\n\nDouble-click or use\n'Open File' to play", fg="#1976d2")

    def _show_text_preview(self, path):
        self.preview_image_label.config(image='', text="")
        self.preview_text.pack(fill=tk.BOTH, expand=True, pady=(5, 0))
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read(4096)
            self.preview_text.config(state=tk.NORMAL)
            self.preview_text.delete("1.0", tk.END)
            self.preview_text.insert("1.0", content)
            self.preview_text.config(state=tk.DISABLED)
            self.preview_image_label.pack_forget()
        except Exception:
            self.preview_image_label.config(text="Could not read file", fg="red")
            self.preview_image_label.pack(fill=tk.BOTH, expand=True)
            self.preview_text.pack_forget()

    def _show_generic_preview(self, ext):
        self.preview_text.pack_forget()
        self.preview_image_label.pack(fill=tk.BOTH, expand=True)
        self.preview_image_label.config(image='', text=f"No preview available\nfor {ext} files\n\nDouble-click or use\n'Open File' to view", fg="gray")

    def _open_preview_file(self):
        if self._preview_path and os.path.exists(self._preview_path):
            if platform.system() == 'Windows':
                os.startfile(self._preview_path)
            else:
                subprocess.call(['xdg-open', self._preview_path])

    # --- EMPTY FOLDER REMOVER ---
    def scan_empty_folders(self):
        folder = self.ent_empty_path.get().strip()
        if not folder or not os.path.isdir(folder):
            return messagebox.showerror("Error", "Select a valid folder to scan.")

        for item in self.tree_empty.get_children():
            self.tree_empty.delete(item)

        self.btn_empty_scan.config(state=tk.DISABLED)
        self.lbl_empty_stat.config(text="Scanning...", fg="blue")

        def run():
            empty_dirs = []
            error = None
            try:
                # Protect the configured protected folders from being reported.
                empty_dirs = find_empty_dirs(folder, protect=self.is_protected)
            except Exception as e:
                error = e
                log.error(f"Empty folder scan crashed: {e}", exc_info=True)

            def populate():
                for d in sorted(empty_dirs):
                    self.tree_empty.insert("", "end", values=(d,))
                count = len(empty_dirs)
                if error:
                    self.lbl_empty_stat.config(text=f"Scan error: {error}", fg="red")
                else:
                    self.lbl_empty_stat.config(text=f"Found {count} empty folder(s).",
                                               fg="green" if count == 0 else "#d32f2f")
                self.btn_empty_scan.config(state=tk.NORMAL)
                self.btn_empty_delete.config(state=tk.NORMAL if count > 0 else tk.DISABLED)
                self.btn_empty_select_all.config(state=tk.NORMAL if count > 0 else tk.DISABLED)
                log.info(f"Empty folder scan: {count} found in {folder}")

            self.root.after(0, populate)

        threading.Thread(target=run, daemon=True).start()

    def select_all_empty(self):
        items = self.tree_empty.get_children()
        if items:
            self.tree_empty.selection_set(*items)

    def delete_empty_folders(self):
        sel = self.tree_empty.selection()
        if not sel:
            return messagebox.showinfo("Info", "Select folders to delete first.")

        if not messagebox.askyesno("Confirm", f"Permanently delete {len(sel)} empty folder(s)?"):
            return

        entries = []
        for item_id in sel:
            path = self.tree_empty.item(item_id, "values")[0]
            entries.append((item_id, path))
        entries.sort(key=lambda e: e[1].count(os.sep), reverse=True)

        removed = 0
        failed = 0
        locked = 0
        changed = 0
        for item_id, path in entries:
            # Re-check: something may have been saved into the folder since
            # the scan. Never remove junk files from a folder that now holds
            # real content.
            if not dir_is_effectively_empty(path):
                log.warning(f"Skipped (no longer empty): {path}")
                changed += 1
                continue
            try:
                delete_junk_files(path)
                force_rmdir(path)
                self.tree_empty.delete(item_id)
                removed += 1
            except PermissionError as e:
                log.warning(f"Could not remove (locked / access denied) {path}: {e}")
                failed += 1
                locked += 1
            except Exception as e:
                log.warning(f"Could not remove {path}: {e}")
                failed += 1

        msg = f"Removed {removed} folder(s)."
        if changed:
            msg += f" {changed} skipped because they are no longer empty."
        if failed:
            msg += f" {failed} failed."
            if locked:
                msg += f" {locked} appear locked — close any Explorer window in them and pause Google Drive sync, then re-scan."
        self.lbl_empty_stat.config(text=msg, fg="green" if failed == 0 else "orange")
        log.info(f"Empty folder delete: {removed} removed, {failed} failed")

        remaining = len(self.tree_empty.get_children())
        self.btn_empty_delete.config(state=tk.NORMAL if remaining > 0 else tk.DISABLED)
        self.btn_empty_select_all.config(state=tk.NORMAL if remaining > 0 else tk.DISABLED)

    def _append_unzip_log(self, line):
        self.txt_unzip_log.config(state=tk.NORMAL)
        self.txt_unzip_log.insert(tk.END, line)
        self.txt_unzip_log.see(tk.END)
        self.txt_unzip_log.config(state=tk.DISABLED)

    def _toggle_merge_mode(self):
        if self.unzip_merge.get():
            self.unzip_subfolder.set(False)
            self.chk_subfolder.config(state=tk.DISABLED)
        else:
            self.chk_subfolder.config(state=tk.NORMAL)

    def start_unzip(self):
        zips = list(self.lst_unzip.get(0, tk.END))
        if not zips:
            return messagebox.showerror("Error", "Add zip files first.")

        engine = self.unzip_engine.get()
        merge = self.unzip_merge.get()
        if merge:
            dest_desc = f"merged into one folder under:\n{Path(zips[0]).parent / 'Takeout_Merged'}"
        else:
            dest_desc = "to their same folder"
        if not messagebox.askyesno("Confirm", f"Unzip {len(zips)} file(s) {dest_desc}\n\nEngine: {engine}"):
            return

        self.btn_unzip_start.config(state=tk.DISABLED)
        self.lbl_unzip_stat.config(text="Unzipping...", fg="blue")
        self.txt_unzip_log.config(state=tk.NORMAL)
        self.txt_unzip_log.delete("1.0", tk.END)
        self.txt_unzip_log.config(state=tk.DISABLED)

        overwrite = self.unzip_overwrite.get()
        subfolder = self.unzip_subfolder.get()
        threading.Thread(target=self._run_unzip, args=(zips, overwrite, subfolder, engine, merge), daemon=True).start()

    def _unzip_tar(self, zp, extract_to, overwrite):
        tar_path = shutil.which("tar")
        if not tar_path:
            raise RuntimeError("Windows tar.exe not found on this computer.")
        os.makedirs(long_path(extract_to), exist_ok=True)
        cmd = [tar_path, "-xf", str(zp), "-C", str(extract_to)]
        if not overwrite:
            cmd.insert(1, "-k")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=None)
        # Windows tar.exe (bsdtar) exits with 1 on errors such as long or
        # invalid names, so any non-zero code means some files were not
        # extracted. The fallback below skips files that already exist when
        # Overwrite is off, so re-running it is safe.
        if result.returncode != 0:
            err = result.stderr.strip() or result.stdout.strip()
            self.root.after(0, self._append_unzip_log, f"  tar failed ({err}); retrying long-path-safe...\n")
            log.warning(f"tar failed for {Path(zp).name}: {err}; using long-path fallback")
            self._extract_zip_longpath(zp, extract_to, overwrite)

    def _extract_zip_longpath(self, zp, extract_to, overwrite):
        import zipfile
        base = str(extract_to)
        os.makedirs(long_path(base), exist_ok=True)
        base_norm = _norm_for_compare(base)
        with zipfile.ZipFile(str(zp)) as zf:
            for member in zf.infolist():
                name = member.filename.replace('\\', '/')
                # Drop empty, '.', '..' and drive-letter parts (e.g. "C:") so a
                # crafted entry can never be written outside the destination.
                parts = [p for p in name.split('/') if p not in ('', '.', '..') and ':' not in p]
                if not parts:
                    continue
                target = long_path(os.path.join(base, *parts))
                if not is_subpath(target, base_norm):
                    log.warning(f"Skipped unsafe zip entry: {member.filename}")
                    continue
                if member.is_dir() or member.filename.endswith('/'):
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                if not overwrite and os.path.exists(target):
                    continue
                with zf.open(member) as src, open(target, 'wb') as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 1024)

    def _unzip_explorer(self, zp, extract_to, overwrite):
        # Explorer's CopyHere has no "skip existing files" option: flag 16
        # ("Yes to All") replaces them. So this engine is only allowed when
        # the user has chosen to overwrite.
        if not overwrite:
            raise RuntimeError("The Explorer engine always replaces existing files. "
                               "Turn on 'Overwrite existing files', or use the Windows tar engine.")
        extract_to.mkdir(parents=True, exist_ok=True)
        # CopyHere runs in the background, so wait until every top-level item
        # of the zip exists in the destination and the folder stops changing,
        # instead of a fixed 2-second sleep that cut large zips short.
        vbs_code = f'''Set shell = CreateObject("Shell.Application")
Set source = shell.NameSpace("{str(zp)}")
Set destination = shell.NameSpace("{str(extract_to)}")
If source Is Nothing Then
    WScript.Echo "Could not open ZIP file."
    WScript.Quit 1
End If
If destination Is Nothing Then
    WScript.Echo "Could not open destination folder."
    WScript.Quit 1
End If
expected = source.Items.Count
destination.CopyHere source.Items, 16
waited = 0
Do
    WScript.Sleep 1000
    waited = waited + 1
    found = 0
    For Each it In source.Items
        If Not destination.ParseName(it.Name) Is Nothing Then found = found + 1
    Next
    If waited > 10800 Then
        WScript.Echo "Timed out waiting for Explorer to finish."
        WScript.Quit 2
    End If
Loop Until found >= expected
Set fso = CreateObject("Scripting.FileSystemObject")
lastSize = -1
Do
    WScript.Sleep 2000
    curSize = fso.GetFolder("{str(extract_to)}").Size
    If curSize = lastSize Then Exit Do
    lastSize = curSize
Loop
'''
        temp_vbs = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".vbs", delete=False, encoding="utf-8") as f:
                temp_vbs = f.name
                f.write(vbs_code)
            result = subprocess.run(["cscript.exe", "//NoLogo", temp_vbs], capture_output=True, text=True, timeout=None)
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        finally:
            if temp_vbs:
                try:
                    os.unlink(temp_vbs)
                except Exception:
                    pass

    def _run_unzip(self, zips, overwrite, subfolder, engine, merge=False):
        log.info(f"Unzip started — {len(zips)} files, engine={engine}, overwrite={overwrite}, subfolder={subfolder}, merge={merge}")
        success = 0
        failed = 0
        msg = "Done."
        color = "gray"
        merge_dest = (Path(zips[0]).parent / "Takeout_Merged") if (merge and zips) else None
        try:
            if engine.startswith("Windows tar"):
                unzip_fn = self._unzip_tar
            else:
                unzip_fn = self._unzip_explorer

            for zip_path in zips:
                zp = Path(zip_path)
                if merge_dest is not None:
                    extract_to = merge_dest
                elif subfolder:
                    extract_to = zp.parent / zp.stem
                else:
                    extract_to = zp.parent

                self.root.after(0, self._append_unzip_log, f"Unzipping: {zp.name} → {extract_to}\n")

                try:
                    unzip_fn(zp, extract_to, overwrite)
                    self.root.after(0, self._append_unzip_log, "  OK\n")
                    success += 1
                except Exception as e:
                    self.root.after(0, self._append_unzip_log, f"  FAILED: {e}\n")
                    log.warning(f"Unzip failed for {zp.name}: {e}")
                    failed += 1

            msg = f"Done. {success} extracted."
            if failed:
                msg += f" {failed} failed."
            color = "green" if failed == 0 else "orange"
            log.info(f"Unzip complete — {success} OK, {failed} failed")
            self.root.after(0, self._append_unzip_log, f"\n{msg}\n")
        except Exception as e:
            log.error(f"Unzip job failed: {e}", exc_info=True)
            msg = f"Unzip job error: {e}"
            color = "red"
            self.root.after(0, self._append_unzip_log, f"\nERROR: {e}\n")
        finally:
            self.root.after(0, self.lbl_unzip_stat.config, {"text": msg, "fg": color})
            self.root.after(0, self.btn_unzip_start.config, {"state": tk.NORMAL})

    # --- GOOGLE DRIVE SHORTCUTS (tab 8) ---
    def _build_gdrive_tab(self):
        self.tab_gdrive = tk.Frame(self.notebook)
        self.notebook.add(self.tab_gdrive, text="8. Google Drive Shortcuts")

        tk.Label(self.tab_gdrive,
                 text="WARNING: these shortcut files (" + ", ".join(GDRIVE_SHORTCUT_EXTENSIONS) + ") stand for "
                      "documents stored in Google Drive.\nMoving them out of a folder that Google Drive syncs "
                      "can remove the documents from Google Drive. Copy is the safe choice.",
                 fg="#b71c1c", bg="#ffebee", font=("Arial", 9, "bold"), justify=tk.LEFT,
                 padx=8, pady=6).pack(fill=tk.X, padx=10, pady=(10, 0))

        frame_top = tk.LabelFrame(self.tab_gdrive, text="Find Google Drive Shortcut Files", padx=15, pady=10)
        frame_top.pack(fill=tk.X, padx=10, pady=10)

        tk.Label(frame_top, text="Folder to scan:", font=("Arial", 9, "bold")).grid(row=0, column=0, sticky="w")
        self.ent_gdrive_path = tk.Entry(frame_top, width=70)
        self.ent_gdrive_path.grid(row=0, column=1, padx=5, pady=5)
        tk.Button(frame_top, text="Browse...", command=lambda: self.browse(self.ent_gdrive_path)).grid(row=0, column=2, pady=5)

        opts = tk.Frame(frame_top)
        opts.grid(row=1, column=0, columnspan=3, pady=5, sticky="w")
        self.gdrive_mode = tk.StringVar(value="copy")
        tk.Radiobutton(opts, text="Copy (safe, originals stay)", variable=self.gdrive_mode,
                       value="copy").pack(side=tk.LEFT, padx=5)
        tk.Radiobutton(opts, text="Move (removes them from the scanned folder)", variable=self.gdrive_mode,
                       value="move", fg="#d32f2f").pack(side=tk.LEFT, padx=5)
        self.gdrive_keep_structure = tk.BooleanVar(value=True)
        tk.Checkbutton(opts, text="Keep folder structure", variable=self.gdrive_keep_structure).pack(side=tk.LEFT, padx=15)

        btns = tk.Frame(frame_top)
        btns.grid(row=2, column=0, columnspan=3, pady=5)
        self.btn_gdrive_scan = tk.Button(btns, text="SCAN", bg="#4caf50", fg="white",
                                         font=("Arial", 10, "bold"), width=12, command=self.scan_gdrive_shortcuts)
        self.btn_gdrive_scan.pack(side=tk.LEFT, padx=5)
        self.btn_gdrive_transfer = tk.Button(btns, text="Copy / Move to Folder...", bg="#fff9c4",
                                             font=("Arial", 10, "bold"), command=self.transfer_gdrive_shortcuts,
                                             state=tk.DISABLED)
        self.btn_gdrive_transfer.pack(side=tk.LEFT, padx=5)
        tk.Label(btns, text="(uses the selected rows, or every listed file when none are selected)",
                 fg="#555", font=("Arial", 8)).pack(side=tk.LEFT, padx=5)

        self.lbl_gdrive_stat = tk.Label(self.tab_gdrive, text="", fg="gray")
        self.lbl_gdrive_stat.pack(pady=3)

        tree_frame = tk.Frame(self.tab_gdrive)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        self.tree_gdrive = ttk.Treeview(tree_frame, columns=("name", "type", "size", "folder", "path"),
                                        show="headings", selectmode="extended")
        for col, text, width in (("name", "File Name", 280), ("type", "Type", 80), ("size", "Size", 80),
                                 ("folder", "Folder", 400), ("path", "Full Path", 0)):
            self.tree_gdrive.heading(col, text=text)
            self.tree_gdrive.column(col, width=width)
        self.tree_gdrive.column("path", width=0, stretch=False)
        scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree_gdrive.yview)
        self.tree_gdrive.configure(yscrollcommand=scroll.set)
        self.tree_gdrive.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree_gdrive.bind("<Double-1>", self.on_double_click)
        self.gdrive_root = None

    def scan_gdrive_shortcuts(self):
        folder = self.ent_gdrive_path.get().strip()
        if not folder or not os.path.isdir(prepare_path(folder)):
            return messagebox.showerror("Error", "Select a valid folder to scan.")
        for item in self.tree_gdrive.get_children():
            self.tree_gdrive.delete(item)
        self.btn_gdrive_scan.config(state=tk.DISABLED)
        self.btn_gdrive_transfer.config(state=tk.DISABLED)
        self.lbl_gdrive_stat.config(text="Scanning...", fg="blue")
        root_disp = _display_path(prepare_path(folder))

        def _worker():
            found, unreadable, error = [], [], None

            def _onerror(err):
                unreadable.append(getattr(err, 'filename', '') or str(err))

            try:
                for dirpath, _dirs, names in safe_walk(prepare_path(folder), onerror=_onerror):
                    for name in names:
                        if not name.lower().endswith(GDRIVE_SHORTCUT_EXTENSIONS):
                            continue
                        full = os.path.join(dirpath, name)
                        try:
                            size = os.path.getsize(full)
                        except OSError:
                            size = 0
                        found.append((_display_path(full), size))
            except Exception as e:
                error = e
                log.error(f"Google Drive shortcut scan crashed: {e}", exc_info=True)
            self.root.after(0, self._gdrive_scan_done, root_disp, found, unreadable, error)

        threading.Thread(target=_worker, daemon=True).start()

    def _gdrive_scan_done(self, root_disp, found, unreadable, error):
        self.gdrive_root = root_disp
        for path, size in sorted(found, key=lambda e: e[0].lower()):
            rel_folder = os.path.relpath(os.path.dirname(path), root_disp)
            self.tree_gdrive.insert("", "end", values=(
                os.path.basename(path), os.path.splitext(path)[1].lower(), format_size(size),
                "" if rel_folder == "." else rel_folder, path))
        if error:
            self.lbl_gdrive_stat.config(text=f"Scan error: {error}", fg="red")
        else:
            msg = f"Found {len(found)} Google Drive shortcut file(s)."
            if unreadable:
                msg += f" {len(unreadable)} folder(s) could not be read (see Log)."
                for u in unreadable:
                    log.warning(f"Not scanned (Google Drive tab): {u}")
            self.lbl_gdrive_stat.config(text=msg, fg="green" if found else "gray")
        log.info(f"Google Drive shortcut scan: {len(found)} found in {root_disp}")
        self.btn_gdrive_scan.config(state=tk.NORMAL)
        self.btn_gdrive_transfer.config(state=tk.NORMAL if found else tk.DISABLED)

    def transfer_gdrive_shortcuts(self):
        tree = self.tree_gdrive
        rows = list(tree.selection()) or list(tree.get_children())
        items = []
        for rid in rows:
            vals = tree.item(rid, "values")
            if vals and vals[-1]:
                items.append((rid, vals[-1]))
        if not items:
            return messagebox.showinfo("Google Drive Shortcuts", "Nothing listed. Run a scan first.")
        move = self.gdrive_mode.get() == "move"
        keep = bool(self.gdrive_keep_structure.get())
        scan_root = self.gdrive_root

        skipped_protected = []
        if move:
            skipped_protected = [p for _r, p in items if self.is_protected(p)]
            items = [(r, p) for r, p in items if not self.is_protected(p)]
            if not items:
                return messagebox.showinfo("Google Drive Shortcuts",
                                           "The chosen files are all in the master or a protected folder, "
                                           "so none can be moved.")

        action = "Move" if move else "Copy"
        dest = filedialog.askdirectory(title=f"{action} Google Drive shortcut files to...")
        if not dest:
            return
        dest = os.path.abspath(dest)
        if scan_root and _norm_for_compare(dest) == _norm_for_compare(scan_root):
            return messagebox.showerror(action, "Choose a different folder from the scanned folder itself.")

        which = "selected" if tree.selection() else "listed"
        msg = (f"{action} {len(items)} {which} Google Drive shortcut file(s) to:\n{dest}\n\n"
               f"Folder structure: {'kept' if keep else 'not kept (all files go directly in the destination)'}\n"
               f"Existing files at the destination are never replaced; a clashing file gets a new name.")
        if move:
            msg += ("\n\nWARNING: moving these files out of a folder that Google Drive syncs can remove "
                    "the documents from Google Drive. Choose Copy if you are not sure.")
            if skipped_protected:
                msg += f"\n\n{len(skipped_protected)} file(s) in protected folders will be skipped."
            ok = messagebox.askyesno(f"{action} Google Drive shortcuts", msg, icon="warning")
        else:
            ok = messagebox.askyesno(f"{action} Google Drive shortcuts", msg)
        if not ok:
            return

        self.btn_gdrive_scan.config(state=tk.DISABLED)
        self.btn_gdrive_transfer.config(state=tk.DISABLED)
        self.lbl_gdrive_stat.config(text=f"{action} in progress...", fg="blue")

        def _worker():
            done_rows, failed = [], 0
            word = "moved" if move else "copied"
            for i, (rid, src) in enumerate(items):
                try:
                    if keep and scan_root and is_subpath(src, scan_root):
                        rel = os.path.relpath(src, scan_root)
                    else:
                        rel = os.path.basename(src)
                    dst = unique_dest_path(os.path.join(dest, rel), word)
                    dst_full = prepare_path(dst)
                    os.makedirs(os.path.dirname(dst_full), exist_ok=True)
                    if os.path.exists(dst_full):
                        raise FileExistsError(f"{dst} already exists")
                    if move:
                        shutil.move(prepare_path(src), dst_full)
                    else:
                        shutil.copy2(prepare_path(src), dst_full)
                    done_rows.append(rid)
                    log.info(f"{action} {src} -> {dst}")
                except Exception as e:
                    failed += 1
                    log.warning(f"Could not {action.lower()} {src}: {e}")
                if (i + 1) % 25 == 0:
                    self.root.after(0, lambda d=i + 1: self.lbl_gdrive_stat.config(
                        text=f"{action}... {d}/{len(items)}"))
            self.root.after(0, _done, done_rows, failed)

        def _done(done_rows, failed):
            if move:
                for rid in done_rows:
                    try:
                        tree.delete(rid)
                    except Exception:
                        pass
            past = "Moved" if move else "Copied"
            text = f"{past} {len(done_rows)} file(s) to {dest}."
            if failed:
                text += f" {failed} failed (see Log)."
            self.lbl_gdrive_stat.config(text=text, fg="green" if not failed else "orange")
            self.btn_gdrive_scan.config(state=tk.NORMAL)
            self.btn_gdrive_transfer.config(state=tk.NORMAL if tree.get_children() else tk.DISABLED)
            messagebox.showinfo(f"{action} complete", text)

        threading.Thread(target=_worker, daemon=True).start()

    def add_protected(self):
        d = filedialog.askdirectory(title="Select folder to protect")
        if d:
            p = prepare_path(d)
            current = list(self.lst_protected.get(0, tk.END))
            if p not in current:
                self.lst_protected.insert(tk.END, p)
        self._refresh_protected()

    def remove_protected(self):
        sel = self.lst_protected.curselection()
        for i in reversed(sel):
            self.lst_protected.delete(i)
        self._refresh_protected()

    def _refresh_protected(self):
        """Keep the protected list used by every action in step with the
        listbox, so folders added after a scan (or after loading a saved scan)
        are protected straight away. Called on the UI thread only."""
        self.protected_paths = list(self.lst_protected.get(0, tk.END))

    def is_protected(self, path):
        # The master folder is always protected in Compare mode: its files are
        # the copies being kept and are never deleted.
        if self.master_path and is_subpath(path, self.master_path):
            return True
        for p in self.protected_paths:
            if is_subpath(path, p):
                return True
        return False

    def cleanup_empty_folders(self):
        """After a delete, offer to remove folders left empty in the CLEANUP
        folder only. The master and protected folders are never touched, and
        the work runs in a background thread so the window stays responsive."""
        target = self.target_path
        if not target or not os.path.isdir(target):
            return
        if not messagebox.askyesno("Empty folders",
                "Remove folders that are now empty in the cleanup folder?\n\n"
                f"{_display_path(target)}\n\n"
                "Folders holding only hidden system files (Thumbs.db, desktop.ini) "
                "count as empty. The master and protected folders are not touched."):
            return

        def _worker():
            removed = 0
            try:
                removed = remove_empty_dirs_recursive(target, protect=self.is_protected)
            except Exception as e:
                log.warning(f"Empty folder cleanup failed: {e}")

            def _report():
                if removed:
                    log.info(f"Cleaned up {removed} empty folder(s)")
                    self.lbl_stat.config(text=f"{self.lbl_stat.cget('text')} Removed {removed} empty folder(s).")
            self.root.after(0, _report)

        threading.Thread(target=_worker, daemon=True).start()

    def alert_user(self, title, msg):
        self.root.after(0, lambda: messagebox.showwarning(title, msg))
        self.root.after(0, lambda: self.btn_scan.config(state=tk.NORMAL))

    # --- SCANNING ---
    def start_scan(self):
        m = self.ent_master.get().strip() if self.mode.get() == 1 else None
        t = self.ent_target.get().strip()
        if not t:
            return messagebox.showerror("Error", "Select Target/Cleanup folder.")
        if not os.path.isdir(prepare_path(t)):
            return messagebox.showerror("Error", f"The cleanup folder does not exist:\n{t}")
        if self.mode.get() == 1:
            if not m:
                return messagebox.showerror("Error", "Select the Protected/Master folder.")
            if not os.path.isdir(prepare_path(m)):
                return messagebox.showerror("Error", f"The master folder does not exist:\n{m}")
            # One folder inside the other would let a master file be compared
            # with itself, so refuse the scan.
            if is_subpath(t, m) or is_subpath(m, t):
                return messagebox.showerror("Error",
                    "The master and cleanup folders overlap (one is inside the other).\n\n"
                    "Choose two separate folders.")

        self.master_path, self.target_path = m, t
        self._refresh_protected()
        self.btn_scan.config(state=tk.DISABLED)
        self.clear_results()

        mode_name = "Smart" if self.scan_mode.get() == SCAN_SMART else "Full"
        strategy_name = self.match_strategy.get()
        log.info(f"Scan started — strategy: {strategy_name}, depth: {mode_name}, target: {t}" + (f", master: {m}" if m else ""))
        self.comparator = Comparator(m, t, self.scan_mode.get(), strategy_name,
                                     self.update_ui, self.scan_finished, self.alert_user)
        self.comparator.start()

    def update_ui(self, msg, pct):
        self.root.after(0, lambda: [self.lbl_stat.config(text=msg), self.progress.configure(value=pct)])

    def scan_finished(self, master_idx, target_idx):
        self.last_master_idx = master_idx
        self.last_target_idx = target_idx
        self.root.after(0, self.calculate_and_populate)
        self.root.after(0, self._report_unreadable)

    def _report_unreadable(self):
        """Tell the user about files or folders the scan could not read, so a
        locked file is never silently missing from the results."""
        comp = getattr(self, "comparator", None)
        items = list(comp.unreadable) if comp else []
        if not items:
            return
        for label, path, err in items:
            log.warning(f"Not scanned{' (' + label + ')' if label else ''}: {path} — {err}")
        messagebox.showwarning("Some files were not scanned",
            f"{len(items)} file(s) or folder(s) could not be read (locked, in use, "
            f"or access denied), so they are not in any result list.\n\n"
            f"They are listed in the Log tab. Close programs using them and scan again.")

    # --- LOGIC & CACHE ---
    def calculate_and_populate(self):
        self.lbl_stat.config(text="Processing Results...")

        cross_dupes = []
        internal_dupes = []
        only_in_target = []

        for ident, t_entries in self.last_target_idx.items():
            if self.mode.get() == 1 and self.last_master_idx and ident in self.last_master_idx:
                cross_dupes.append({
                    'master_file': self.last_master_idx[ident][0][0],
                    'target_files': t_entries
                })
            else:
                if len(t_entries) > 1:
                    internal_dupes.append(t_entries)
                if self.mode.get() == 1:
                    only_in_target.extend(t_entries)

        # Mirror of "Only in Target": master files whose content has no match
        # anywhere in the cleanup folder. Shown read-only in tab 3.
        only_in_master = []
        if self.mode.get() == 1 and self.last_master_idx:
            for ident, m_entries in self.last_master_idx.items():
                if ident not in self.last_target_idx:
                    only_in_master.extend(m_entries)

        self.cross_dupes = cross_dupes
        self.internal_dupes = internal_dupes
        self.only_in_target = only_in_target
        self.only_in_master = only_in_master

        self.populate_trees()
        self.btn_scan.config(state=tk.NORMAL)
        self.progress['value'] = 100

        total_waste = 0
        total_extra = 0
        for group in internal_dupes:
            # Recoverable space assumes we keep the same file the cleanup keeps
            # (best by _path_score, or the one picked with Keep This Copy) and
            # reclaim the rest.
            scored = self._ordered_group(group)
            total_extra += len(group) - 1
            total_waste += sum(s for _, s in scored[1:])
        for d in cross_dupes:
            for e in d['target_files']:
                total_extra += 1
                total_waste += e[1]

        only_size = sum(s for _, s in only_in_target)
        self.lbl_stat.config(text=f"Done. {len(internal_dupes)} internal groups, {len(cross_dupes)} cross-folder matches, "
                                  f"{len(only_in_target)} unique to target"
                                  + (f", {len(only_in_master)} unique to master." if self.mode.get() == 1 else "."))
        self.lbl_summary.config(text=f"Recoverable: {format_size(total_waste)} from {total_extra} duplicate files")
        log.info(f"Scan complete — {len(internal_dupes)} internal groups, {len(cross_dupes)} cross matches, "
                 f"{len(only_in_target)} unique to target ({format_size(only_size)}), "
                 f"{len(only_in_master)} unique to master, {format_size(total_waste)} recoverable")

    def populate_trees(self):
        self.clear_trees()
        pending = []

        if self.mode.get() == 1:
            for d in self.cross_dupes:
                for path, size in d['target_files']:
                    self.tree_cross.insert("", "end", values=(
                        os.path.basename(path),
                        format_size(size),
                        os.path.basename(os.path.dirname(path)),
                        "Safe to Delete",
                        path
                    ))

        for group in self.internal_dupes:
            group_size = format_size(group[0][1])
            grp_id = self.tree_internal.insert("", "end", values=(
                f"[GROUP] {len(group)} copies",
                group_size,
                "",
                "",
                ""
            ), open=True)

            scored = self._ordered_group(group)
            for i, (path, size) in enumerate(scored):
                tag, prefix = self._internal_row_style(path, i == 0)
                res = _RESOLUTION_CACHE.get(path, "")
                item_id = self.tree_internal.insert(grp_id, "end", values=(
                    prefix + os.path.basename(path),
                    format_size(size),
                    res,
                    os.path.basename(os.path.dirname(path)),
                    path
                ), tags=(tag,))
                if not res:
                    pending.append((self.tree_internal, item_id, path))

        self.tree_internal.tag_configure("protected", foreground="#d32f2f", font=("Arial", 9, "bold"))
        self.tree_internal.tag_configure("keeper", foreground="#2e7d32")
        self.tree_internal.tag_configure("dupe", foreground="#666666")

        pending.extend(self.populate_only_tree())
        pending.extend(self._populate_folder_tree(self.tree_only_master, self.only_in_master))

        if pending:
            self._start_resolution_lookup(pending)

    def _start_resolution_lookup(self, items):
        self._resolution_token = getattr(self, "_resolution_token", 0) + 1
        my_token = self._resolution_token

        def _worker():
            try:
                for tree, item_id, path in items:
                    if my_token != self._resolution_token:
                        return
                    res = get_resolution(path)
                    if res:
                        self.root.after(0, self._apply_resolution, tree, item_id, res, my_token)
            except Exception as e:
                log.warning(f"Resolution lookup worker error: {e}")
        threading.Thread(target=_worker, daemon=True).start()

    def _apply_resolution(self, tree, item_id, res, token):
        if token != getattr(self, "_resolution_token", 0):
            return
        try:
            if not tree.exists(item_id):
                return
            vals = list(tree.item(item_id, "values"))
            if len(vals) >= 3:
                vals[2] = res
                tree.item(item_id, values=vals)
        except Exception:
            pass

    def populate_only_tree(self):
        return self._populate_folder_tree(self.tree_only, self.only_in_target)

    def _populate_folder_tree(self, tree, entries):
        """Fill a folder-grouped list (tabs 2 and 3) from (path, size) entries.
        Returns the rows still waiting for a resolution lookup."""
        for x in tree.get_children():
            tree.delete(x)

        if not entries:
            return []

        pending = []
        by_folder = {}
        for path, size in entries:
            folder = os.path.dirname(path)
            by_folder.setdefault(folder, []).append((path, size))

        for folder in sorted(by_folder.keys()):
            files = sorted(by_folder[folder], key=lambda e: os.path.basename(e[0]).lower())
            folder_total = sum(s for _, s in files)
            grp_id = tree.insert("", "end", values=(
                f"[FOLDER] {folder}  ({len(files)} files)",
                format_size(folder_total),
                "",
                "",
                ""
            ), open=True, tags=("folderhdr",))
            for path, size in files:
                res = _RESOLUTION_CACHE.get(path, "")
                item_id = tree.insert(grp_id, "end", values=(
                    os.path.basename(path),
                    format_size(size),
                    res,
                    os.path.basename(os.path.dirname(path)),
                    path
                ))
                if not res:
                    pending.append((tree, item_id, path))
        return pending

    def export_only_in_target(self):
        if not self.only_in_target:
            return messagebox.showinfo("Empty", "Nothing unique to target. Run a Compare-Two-Folders scan first.")
        self._export_folder_list(self.only_in_target, "only_in_target.txt",
                                 "FILES ONLY IN TARGET (no matching content in master)",
                                 "unique-to-target")

    def export_only_in_master(self):
        if not self.only_in_master:
            return messagebox.showinfo("Empty", "Nothing unique to master. Run a Compare-Two-Folders scan first.")
        self._export_folder_list(self.only_in_master, "only_in_master.txt",
                                 "FILES ONLY IN MASTER (no matching content in target)",
                                 "unique-to-master")

    def _export_folder_list(self, entries, initialfile, heading, what):
        f = filedialog.asksaveasfilename(defaultextension=".txt", filetypes=[("Text File", "*.txt")], initialfile=initialfile)
        if not f:
            return
        try:
            by_folder = {}
            for path, size in entries:
                by_folder.setdefault(os.path.dirname(path), []).append((path, size))
            total = sum(s for _, s in entries)
            with open(f, "w", encoding="utf-8") as out:
                out.write(heading + "\n")
                out.write(f"Master: {self.master_path}\n")
                out.write(f"Target: {self.target_path}\n")
                out.write(f"{len(entries)} files, {format_size(total)} total\n")
                out.write("=" * 70 + "\n\n")
                for folder in sorted(by_folder.keys()):
                    files = sorted(by_folder[folder], key=lambda e: os.path.basename(e[0]).lower())
                    out.write(f"{folder}  ({len(files)} files)\n")
                    for path, size in files:
                        out.write(f"    {os.path.basename(path)}  [{format_size(size)}]\n")
                    out.write("\n")
            log.info(f"Exported {len(entries)} {what} files to {f}")
            messagebox.showinfo("Saved", f"Exported {len(entries)} files to:\n{f}")
        except Exception as e:
            log.warning(f"Export failed: {e}")
            messagebox.showerror("Error", f"Could not write file:\n{e}")

    def sync_to_master(self):
        if not self.only_in_target:
            return messagebox.showinfo("Nothing to sync", "No files unique to target. Run a Compare-Two-Folders scan first.")
        if not self.master_path or not os.path.isdir(self.master_path):
            return messagebox.showerror("Error", "Master folder is not set or does not exist.")
        if not self.target_path or not os.path.isdir(self.target_path):
            return messagebox.showerror("Error", "Target folder is not set or does not exist.")

        total = len(self.only_in_target)
        total_size = sum(s for _, s in self.only_in_target)
        if not messagebox.askyesno("Copy to Master",
                f"Copy {total} file(s) ({format_size(total_size)}) from target into master?\n\n"
                f"Master: {self.master_path}\n"
                f"Files are copied (target stays intact) and the target folder structure is recreated under master.\n\n"
                f"Identical copies are detected by content and copied only once."):
            return

        def _strip_lp(p):
            p = str(p)
            if p.startswith('\\\\?\\'):
                p = p[4:]
            return p

        target_root = os.path.abspath(_strip_lp(self.target_path))
        master_root = os.path.abspath(_strip_lp(self.master_path))
        files = list(self.only_in_target)

        self.btn_scan.config(state=tk.DISABLED)
        self.btn_sync_master.config(state=tk.DISABLED)
        self.lbl_stat.config(text="Copying to master...")

        def _worker():
            copied = 0
            failed = 0
            skipped_dupes = 0
            seen_by_hash = {}   # md5 -> representative path already copied
            try:
                for i, (path, size) in enumerate(files):
                    try:
                        # Copy each distinct file only once. MD5 finds likely
                        # duplicates fast; a byte-for-byte check then confirms it
                        # before skipping, so a hash collision can never drop a
                        # genuinely different file.
                        fh = hash_file_full(prepare_path(path))
                        if fh is not None and fh in seen_by_hash:
                            if files_are_identical(seen_by_hash[fh], path):
                                skipped_dupes += 1
                                continue

                        src_norm = os.path.abspath(_strip_lp(path))
                        try:
                            rel = os.path.relpath(src_norm, target_root)
                            if rel.startswith("..") or os.path.isabs(rel):
                                rel = os.path.basename(src_norm)
                        except ValueError:
                            rel = os.path.basename(src_norm)
                        dst = os.path.join(master_root, rel)
                        src = path

                        # Check for an existing file with the same long-path-safe
                        # form that the copy uses, so a path over 260 characters
                        # can never overwrite a different master file.
                        if os.path.exists(prepare_path(dst)):
                            base, ext = os.path.splitext(dst)
                            n = 1
                            while os.path.exists(prepare_path(f"{base} (from target {n}){ext}")):
                                n += 1
                            dst = f"{base} (from target {n}){ext}"

                        dst_full = prepare_path(dst)
                        os.makedirs(os.path.dirname(dst_full), exist_ok=True)
                        if os.path.exists(dst_full):
                            raise FileExistsError(f"{dst} already exists")
                        shutil.copy2(src, dst_full)
                        copied += 1
                        if fh is not None:
                            seen_by_hash.setdefault(fh, path)
                    except Exception as e:
                        log.warning(f"Could not copy {path} to master: {e}")
                        failed += 1

                    if (i + 1) % 25 == 0 or (i + 1) == total:
                        done = i + 1
                        self.root.after(0, lambda d=done: self.lbl_stat.config(text=f"Copying to master... {d}/{total}"))
            except Exception as e:
                log.error(f"Sync to master crashed: {e}", exc_info=True)
            finally:
                self.root.after(0, _done, copied, failed, skipped_dupes)

        def _done(copied, failed, skipped_dupes):
            msg = f"Copied {copied} file(s) to master."
            if skipped_dupes:
                msg += f"\n{skipped_dupes} identical copy(ies) skipped."
            if failed:
                msg += f"\n{failed} file(s) could not be copied (see Log)."
            messagebox.showinfo("Sync Complete", msg)
            log.info(f"Sync to master: {copied} copied, {skipped_dupes} identical skipped, {failed} failed")
            self.lbl_stat.config(text=f"Synced {copied} file(s) to master. Re-scan to verify.")
            self.btn_scan.config(state=tk.NORMAL)
            self.btn_sync_master.config(state=tk.NORMAL)

        threading.Thread(target=_worker, daemon=True).start()

    def _path_score(self, path):
        rel = path.lower()
        score = 0

        if self.is_protected(path):
            return -100000

        if re.search(r'\(\d+\)\.[a-z]+$', rel):
            score += 1000

        basename = os.path.basename(rel)
        if basename.startswith('download '):
            score += 500

        if 'copy of' in rel or '- copy' in rel or re.search(r'[ _-]copy( \d+)?\.[a-z0-9]+$', rel):
            score += 500

        # Files sitting in a folder named like a backup or duplicates dump are
        # better candidates to remove than the same file elsewhere.
        if re.search(r'[\\/](backup|backups|duplicates?|old)[\\/]', rel):
            score += 200

        # Shorter paths win ties: the copy nearest the top of the tree is kept.
        score += len(path) * 0.01
        return score

    def clear_trees(self):
        for t in [self.tree_cross, self.tree_internal, self.tree_only, self.tree_only_master]:
            for x in t.get_children():
                t.delete(x)

    def clear_results(self):
        self.clear_trees()
        self.last_master_idx = {}
        self.last_target_idx = {}
        self.cross_dupes = []
        self.internal_dupes = []
        self.only_in_target = []
        self.only_in_master = []
        self.chosen_keepers = set()
        self.lbl_summary.config(text="")

    # --- SAVE / LOAD ---
    def save_cache(self):
        if not self.last_target_idx:
            return messagebox.showwarning("Empty", "No scan data to save. Run a scan first.")

        f = filedialog.asksaveasfilename(defaultextension=".cache", filetypes=[("Scan Cache", "*.cache")])
        if f:
            try:
                data = {
                    'master': self.last_master_idx,
                    'target': self.last_target_idx,
                    'mode': self.mode.get(),
                    'match_strategy': self.match_strategy.get(),
                    # Saved so a loaded scan keeps the same master guard and
                    # protected folders as the scan that produced it.
                    'master_path': self.master_path,
                    'target_path': self.target_path,
                    'protected_paths': list(self.protected_paths),
                }
                with open(f, 'wb') as outfile:
                    pickle.dump(data, outfile)
                messagebox.showinfo("Saved", "Scan results saved successfully.")
            except Exception as e:
                messagebox.showerror("Error", f"Could not save: {e}")

    def load_cache(self):
        f = filedialog.askopenfilename(filetypes=[("Scan Cache", "*.cache")])
        if f:
            try:
                with open(f, 'rb') as infile:
                    data = safe_pickle_load(infile)

                self.last_master_idx = data.get('master', {})
                self.last_target_idx = data.get('target', {})
                self.mode.set(data.get('mode', 2))
                self.match_strategy.set(data.get('match_strategy', MATCH_CONTENT))
                self.toggle_mode()
                self._on_strategy_change()

                # Restore the folders. Older caches did not store them; then
                # fall back to what is typed in the boxes now, and refuse to
                # load a Compare-mode cache with no master folder at all.
                master = data.get('master_path')
                target = data.get('target_path')
                if self.mode.get() == 1:
                    master = master or self.ent_master.get().strip() or None
                    if not master:
                        self.clear_results()
                        return messagebox.showerror("Cannot load",
                            "This saved scan does not record its master folder.\n\n"
                            "Enter the master folder in box 1, then load it again.")
                else:
                    master = None
                target = target or self.ent_target.get().strip() or None
                self.master_path, self.target_path = master, target
                if master:
                    self.ent_master.delete(0, tk.END)
                    self.ent_master.insert(0, master)
                if target:
                    self.ent_target.delete(0, tk.END)
                    self.ent_target.insert(0, target)
                current = list(self.lst_protected.get(0, tk.END))
                for p in data.get('protected_paths', []):
                    if p not in current:
                        self.lst_protected.insert(tk.END, p)
                self._refresh_protected()

                self.calculate_and_populate()
                messagebox.showinfo("Loaded", "Cache loaded and results refreshed.")
            except Exception as e:
                messagebox.showerror("Error", f"Could not load: {e}")

    # --- ACTIONS ---
    def on_double_click(self, event):
        tree = event.widget
        sel = tree.selection()
        if not sel:
            return
        vals = tree.item(sel[0], "values")
        if vals and vals[-1]:
            reveal_in_explorer(vals[-1])

    def _trash_batch(self, paths, on_done):
        permanent = bool(self.permanent_delete.get())
        total = len(paths)

        def _worker():
            trashed = 0
            failed = 0
            failed_paths = []
            try:
                if permanent:
                    for i, path in enumerate(paths):
                        try:
                            os.remove(prepare_path(path))
                            trashed += 1
                        except Exception as e:
                            log.warning(f"Could not delete {path}: {e}")
                            failed += 1
                            failed_paths.append(path)
                        if (i + 1) % 50 == 0 or (i + 1) == total:
                            self.root.after(0, lambda t=trashed: self.lbl_stat.config(text=f"Deleting... {t}/{total}"))
                else:
                    batch = []
                    for path in paths:
                        batch.append(path)
                        if len(batch) >= 50:
                            try:
                                send2trash.send2trash(batch)
                                trashed += len(batch)
                            except Exception:
                                for p in batch:
                                    try:
                                        send2trash.send2trash(p)
                                        trashed += 1
                                    except Exception as e:
                                        log.warning(f"Could not trash {p}: {e}")
                                        failed += 1
                                        failed_paths.append(p)
                            batch = []
                            self.root.after(0, lambda t=trashed: self.lbl_stat.config(text=f"Trashing... {t}/{total}"))
                    if batch:
                        try:
                            send2trash.send2trash(batch)
                            trashed += len(batch)
                        except Exception:
                            for p in batch:
                                try:
                                    send2trash.send2trash(p)
                                    trashed += 1
                                except Exception as e:
                                    log.warning(f"Could not trash {p}: {e}")
                                    failed += 1
                                    failed_paths.append(p)
            except Exception as e:
                log.error(f"Delete worker crashed: {e}", exc_info=True)
            finally:
                self.root.after(0, on_done, trashed, failed, failed_paths)
        threading.Thread(target=_worker, daemon=True).start()

    def _verify_byte_identical(self, groups, on_ready):
        """Confirm each candidate is byte-for-byte identical to the file being
        kept before anything is trashed. `groups` is a list of
        (keeper_path, [candidate_paths]). Hashing runs in a background thread;
        on_ready(verified_paths, mismatched_paths) is then called on the UI
        thread. A candidate is verified only if it still exists, is readable,
        and it is byte-for-byte identical to the keeper — so files that merely share a
        name/size or extracted text but differ in bytes are never deleted."""
        total = sum(len(c) for _, c in groups)

        def _worker():
            verified = []
            mismatched = []
            done = 0
            try:
                for keeper, candidates in groups:
                    if not os.path.exists(prepare_path(keeper)):
                        log.warning(f"Cannot read kept file, skipping its group "
                                    f"({keeper})")
                        mismatched.extend(candidates)
                        done += len(candidates)
                        continue
                    for cand in candidates:
                        try:
                            if not os.path.exists(prepare_path(cand)):
                                done += 1
                                continue
                            # Never let a file "match" itself: the same path, or
                            # the same physical file reached through a junction,
                            # symlink or hard link, would pass the byte check
                            # and the only copy would be trashed.
                            if _same_physical_file(keeper, cand):
                                mismatched.append(cand)
                                log.warning(f"Not deleting {cand}: it is the same "
                                            f"file as the kept copy {keeper}")
                                done += 1
                                continue
                            # Last-line guard: nothing inside the master folder or
                            # a protected folder is ever deleted.
                            if self.is_protected(cand):
                                mismatched.append(cand)
                                log.warning(f"Not deleting {cand}: it is in the "
                                            f"master or a protected folder")
                                done += 1
                                continue
                            if files_are_identical(keeper, cand):
                                verified.append(cand)
                            else:
                                mismatched.append(cand)
                                log.warning(f"Not deleting {cand}: bytes differ "
                                            f"from the kept file {keeper}")
                        except Exception as e:
                            mismatched.append(cand)
                            log.warning(f"Not deleting {cand}: could not verify ({e})")
                        done += 1
                        if done % 25 == 0 or done == total:
                            self.root.after(0, lambda d=done: self.lbl_stat.config(
                                text=f"Verifying bytes... {d}/{total}"))
            except Exception as e:
                log.error(f"Byte verification crashed: {e}", exc_info=True)
            self.root.after(0, on_ready, verified, mismatched)

        self.lbl_stat.config(text=f"Verifying bytes before deleting... 0/{total}")
        threading.Thread(target=_worker, daemon=True).start()

    def trash_cross(self):
        if not self.cross_dupes:
            return
        groups = []
        total = 0
        for d in self.cross_dupes:
            keeper = d['master_file']
            cands = []
            for path, size in d['target_files']:
                if self.master_path and is_subpath(path, self.master_path):
                    continue
                cands.append(path)
            if cands:
                groups.append((keeper, cands))
                total += len(cands)
        if total == 0:
            return
        if not messagebox.askyesno("Confirm",
                f"Verify and trash up to {total} file(s) that match master?\n\n"
                f"Each file's bytes are checked against the master copy first; "
                f"anything that isn't byte-for-byte identical is kept."):
            return
        self.btn_scan.config(state=tk.DISABLED)

        def _proceed(verified, mismatched):
            if not verified:
                self.btn_scan.config(state=tk.NORMAL)
                self.lbl_stat.config(text="Nothing deleted — no byte-identical matches.")
                messagebox.showinfo("Nothing deleted",
                    "None of the files were byte-for-byte identical to master, "
                    "so nothing was trashed.")
                return

            def _done(trashed, failed, failed_paths=()):
                msg = f"Trashed {trashed} file(s)."
                if failed:
                    msg += f"\n{failed} could not be trashed."
                if mismatched:
                    msg += f"\n{len(mismatched)} kept (bytes differed from master)."
                messagebox.showinfo("Done", msg)
                log.info(f"Cross-folder trash: {trashed} trashed, {failed} failed, "
                         f"{len(mismatched)} kept (not identical)")
                self.cross_dupes = []
                self.last_master_idx = {}
                self.last_target_idx = {}
                self.clear_trees()
                self.lbl_summary.config(text=f"Trashed {trashed} file(s). Run a new scan to verify.")
                self.btn_scan.config(state=tk.NORMAL)
                self.cleanup_empty_folders()

            self._trash_batch(verified, _done)

        self._verify_byte_identical(groups, _proceed)

    def delete_selected_internal(self):
        sel = self.tree_internal.selection()
        if not sel:
            return

        skipped_protected = 0
        groups = {}          # parent_id -> [keeper_path, [candidate_path, ...]]
        itemid_by_path = {}
        for item_id in sel:
            parent_id = self.tree_internal.parent(item_id)
            if not parent_id:
                continue  # a group header row was selected; ignore it
            vals = self.tree_internal.item(item_id, "values")
            path = vals[-1] if vals else ""
            if not path or not os.path.exists(prepare_path(path)):
                continue
            if self.is_protected(path):
                skipped_protected += 1
                continue
            children = self.tree_internal.get_children(parent_id)
            if not children:
                continue
            keeper_vals = self.tree_internal.item(children[0], "values")
            keeper_path = keeper_vals[-1] if keeper_vals else ""
            # Never delete the group's kept file, even if it was selected.
            if not keeper_path or _norm_for_compare(path) == _norm_for_compare(keeper_path):
                continue
            groups.setdefault(parent_id, [keeper_path, []])[1].append(path)
            itemid_by_path[path] = item_id

        if skipped_protected:
            messagebox.showinfo("Protected", f"Skipped {skipped_protected} file(s) in protected folders.")

        group_list = [(k, c) for k, c in groups.values()]
        if not group_list:
            return

        total = sum(len(c) for _, c in group_list)
        if not messagebox.askyesno("Delete",
                f"Verify and trash {total} selected file(s)?\n\nEach is checked "
                f"byte-for-byte against the kept file in its group first; anything "
                f"that isn't identical is kept."):
            return

        self.btn_scan.config(state=tk.DISABLED)

        def _proceed(verified, mismatched):
            if not verified:
                self.btn_scan.config(state=tk.NORMAL)
                self.lbl_stat.config(text="Nothing deleted — no byte-identical duplicates.")
                messagebox.showinfo("Nothing deleted",
                    "None of the selected files were byte-for-byte identical to "
                    "the kept file in their group, so nothing was trashed.")
                return

            def _done(trashed, failed, failed_paths=()):
                still_there = set(failed_paths)
                for p in verified:
                    if p in still_there:
                        continue  # trashing failed; keep its row visible
                    iid = itemid_by_path.get(p)
                    if iid:
                        try:
                            self.tree_internal.delete(iid)
                        except Exception:
                            pass
                log.info(f"Internal trash: {trashed} deleted, {failed} failed, "
                         f"{len(mismatched)} kept (not identical)")
                msg = f"Trashed {trashed} file(s)."
                if mismatched:
                    msg += f" {len(mismatched)} kept (bytes differed)."
                self.lbl_stat.config(text=msg)
                if trashed:
                    self.cleanup_empty_folders()
                self.btn_scan.config(state=tk.NORMAL)

            self._trash_batch(verified, _done)

        self._verify_byte_identical(group_list, _proceed)

    # --- MOVE SELECTED ---
    def move_selected(self, tree):
        """Move the selected files to a folder you choose. With "keep folder
        structure", each file keeps its path relative to the cleanup folder
        (for example cleanup\\2019\\a.jpg goes to destination\\2019\\a.jpg).
        Files in the master or a protected folder are never moved, and an
        existing file at the destination is never replaced: the moved file
        gets a "(moved N)" name instead. Selecting a folder row in tab 2 moves
        every file listed under it. In tab 4 the kept copy of each group is
        never moved."""
        paths = []
        row_by_path = {}
        # In tab 4 the first row of each group is the copy being kept, so it
        # is never moved, even when it or its group header is selected.
        keeper_rows = set()
        if tree is self.tree_internal:
            for gid in tree.get_children():
                kids = tree.get_children(gid)
                if kids:
                    keeper_rows.add(kids[0])
        skipped_keepers = 0
        for item_id in tree.selection():
            rows = tree.get_children(item_id) or (item_id,)
            for rid in rows:
                if rid in keeper_rows:
                    skipped_keepers += 1
                    continue
                vals = tree.item(rid, "values")
                p = vals[-1] if vals else ""
                if p and p not in row_by_path:
                    paths.append(p)
                    row_by_path[p] = rid
        if not paths:
            if skipped_keepers:
                return messagebox.showinfo("Move", "Only kept copies (KEEP rows) are selected. "
                                                   "The kept copy of a group is never moved.")
            return messagebox.showinfo("Move", "Select one or more files first.")

        skipped_protected = [p for p in paths if self.is_protected(p)]
        paths = [p for p in paths if p not in skipped_protected]
        if not paths:
            return messagebox.showinfo("Move", "The selected files are all in the master or "
                                               "a protected folder, so none can be moved.")

        dest = filedialog.askdirectory(title="Move selected files to...")
        if not dest:
            return
        dest = os.path.abspath(dest)
        target = self.target_path
        if target and _norm_for_compare(dest) == _norm_for_compare(target):
            return messagebox.showerror("Move", "Choose a different folder from the cleanup folder itself.")
        if self.is_protected(dest):
            return messagebox.showerror("Move", "The destination is inside the master or a protected "
                                                "folder. Choose another folder.")

        keep = messagebox.askyesnocancel("Move",
            f"Move {len(paths)} file(s) to:\n{dest}\n\n"
            f"Keep their folder structure?\n"
            f"  Yes = recreate the subfolders under the destination\n"
            f"  No = put all files directly in the destination\n\n"
            f"Existing files at the destination are never replaced."
            + (f"\n\n{len(skipped_protected)} selected file(s) in protected folders will be skipped."
               if skipped_protected else "")
            + (f"\n\n{skipped_keepers} kept copy(ies) (KEEP rows) will be skipped."
               if skipped_keepers else ""))
        if keep is None:
            return

        self.lbl_stat.config(text=f"Moving {len(paths)} file(s)...")

        def _worker():
            moved, failed = [], 0
            for i, src in enumerate(paths):
                try:
                    src_disp = _display_path(src)
                    if keep and target and is_subpath(src_disp, target):
                        rel = os.path.relpath(src_disp, _display_path(target))
                    else:
                        rel = os.path.basename(src_disp)
                    dst = os.path.join(dest, rel)
                    if os.path.exists(prepare_path(dst)):
                        base, ext = os.path.splitext(dst)
                        n = 1
                        while os.path.exists(prepare_path(f"{base} (moved {n}){ext}")):
                            n += 1
                        dst = f"{base} (moved {n}){ext}"
                    dst_full = prepare_path(dst)
                    os.makedirs(os.path.dirname(dst_full), exist_ok=True)
                    shutil.move(prepare_path(src), dst_full)
                    moved.append(src)
                    log.info(f"Moved {src_disp} -> {dst}")
                except Exception as e:
                    failed += 1
                    log.warning(f"Could not move {src}: {e}")
                if (i + 1) % 25 == 0:
                    self.root.after(0, lambda d=i + 1: self.lbl_stat.config(
                        text=f"Moving... {d}/{len(paths)}"))
            self.root.after(0, _done, moved, failed)

        def _done(moved, failed):
            for p in moved:
                rid = row_by_path.get(p)
                if rid:
                    try:
                        tree.delete(rid)
                    except Exception:
                        pass
            msg = f"Moved {len(moved)} file(s) to {dest}."
            if failed:
                msg += f" {failed} could not be moved (see Log)."
            self.lbl_stat.config(text=msg)
            messagebox.showinfo("Move complete", msg)
            if moved:
                self.cleanup_empty_folders()

        threading.Thread(target=_worker, daemon=True).start()

    # --- CHOOSE WHICH COPY TO KEEP (tab 4) ---
    def _ordered_group(self, group):
        """Return a duplicate group's (path, size) entries with the kept copy
        first: the one picked with "Keep This Copy" if there is one, otherwise
        the best-scored path (protected files score best)."""
        scored = sorted(group, key=lambda e: self._path_score(e[0]))
        for i, (path, _size) in enumerate(scored):
            if _norm_for_compare(path) in self.chosen_keepers:
                return [scored[i]] + scored[:i] + scored[i + 1:]
        return scored

    def _internal_row_style(self, path, is_keeper):
        """Tag and name prefix for a file row in tab 4."""
        if self.is_protected(path):
            return "protected", "PROTECTED  "
        if is_keeper:
            return "keeper", "KEEP  "
        return "dupe", ""

    def _relabel_internal_group(self, group_id):
        """Refresh the KEEP / PROTECTED labels after the kept row changed. The
        first row of a group is always the copy that is kept."""
        for i, child in enumerate(self.tree_internal.get_children(group_id)):
            vals = list(self.tree_internal.item(child, "values"))
            if not vals or not vals[-1]:
                continue
            tag, prefix = self._internal_row_style(vals[-1], i == 0)
            vals[0] = prefix + os.path.basename(vals[-1])
            self.tree_internal.item(child, values=vals, tags=(tag,))

    def keep_this_copy(self):
        """Make the selected row the copy that is kept in its group. It moves
        to the top of the group and is never trashed or moved; the other
        copies are checked byte-for-byte against it before any delete."""
        tree = self.tree_internal
        rows = [i for i in tree.selection() if tree.parent(i)]
        if len(rows) != 1:
            return messagebox.showinfo("Keep This Copy",
                                       "Select exactly one file row inside a group first.")
        item_id = rows[0]
        group_id = tree.parent(item_id)
        vals = tree.item(item_id, "values")
        path = vals[-1] if vals else ""
        if not path:
            return
        children = tree.get_children(group_id)
        for child in children:
            cvals = tree.item(child, "values")
            if cvals and cvals[-1]:
                self.chosen_keepers.discard(_norm_for_compare(cvals[-1]))
        self.chosen_keepers.add(_norm_for_compare(path))
        if children and children[0] != item_id:
            tree.move(item_id, group_id, 0)
        self._relabel_internal_group(group_id)
        tree.selection_remove(item_id)
        self.lbl_stat.config(text=f"Keeping {_display_path(path)} in this group.")
        log.info(f"Keep This Copy: {_display_path(path)}")

    def _internal_folders(self):
        """Folders that hold at least one file listed in tab 4."""
        folders = {}
        for group_id in self.tree_internal.get_children():
            for child in self.tree_internal.get_children(group_id):
                vals = self.tree_internal.item(child, "values")
                if vals and vals[-1]:
                    folder = os.path.dirname(vals[-1])
                    folders.setdefault(_norm_for_compare(folder), folder)
        return sorted(folders.values(), key=lambda f: _display_path(f).lower())

    def select_dupes_in_folder(self, folder):
        """Select every duplicate (not the kept copy, not protected) whose
        folder is exactly `folder`. The first row of each group is the kept
        copy and is never selected, so at least one copy per group always
        stays unselected. Returns the number of rows selected."""
        tree = self.tree_internal
        want = _norm_for_compare(folder)
        to_select = []
        for group_id in tree.get_children():
            for child in tree.get_children(group_id)[1:]:
                vals = tree.item(child, "values")
                path = vals[-1] if vals else ""
                if not path or self.is_protected(path):
                    continue
                if _norm_for_compare(os.path.dirname(path)) == want:
                    to_select.append(child)
        current = tree.selection()
        if current:
            tree.selection_remove(*current)
        if to_select:
            tree.selection_set(*to_select)
            tree.see(to_select[0])
        self.lbl_stat.config(text=f"Selected {len(to_select)} duplicate file(s) in {_display_path(folder)} "
                                  f"(the kept copy of each group is not selected).")
        return len(to_select)

    def open_select_folder_dialog(self):
        folders = self._internal_folders()
        if not folders:
            return messagebox.showinfo("Select Duplicates", "No duplicates listed. Run a scan first.")

        start = None
        sel = [i for i in self.tree_internal.selection() if self.tree_internal.parent(i)]
        if sel:
            vals = self.tree_internal.item(sel[0], "values")
            if vals and vals[-1]:
                start = _norm_for_compare(os.path.dirname(vals[-1]))

        top = tk.Toplevel(self.root)
        top.title("Select Duplicates in This Folder")
        top.geometry("700x400")
        top.transient(self.root)
        tk.Label(top, text="Choose a folder. Every duplicate in it is selected, except the kept copy of each group.",
                 font=("Arial", 9, "bold")).pack(pady=5, padx=10, anchor="w")
        frame_list = tk.Frame(top)
        frame_list.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        lb = tk.Listbox(frame_list, selectmode=tk.BROWSE)
        lb.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(frame_list, orient="vertical", command=lb.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        lb.config(yscrollcommand=sb.set)
        for i, f in enumerate(folders):
            lb.insert(tk.END, _display_path(f))
            if start and _norm_for_compare(f) == start:
                lb.selection_set(i)
                lb.see(i)

        def do_select(event=None):
            picked = lb.curselection()
            if not picked:
                return
            folder = folders[picked[0]]
            top.destroy()
            self.select_dupes_in_folder(folder)

        lb.bind("<Double-1>", do_select)
        btns = tk.Frame(top)
        btns.pack(pady=8)
        tk.Button(btns, text="Select Duplicates", bg="#4caf50", fg="white", command=do_select).pack(side=tk.LEFT, padx=5)
        tk.Button(btns, text="Cancel", command=top.destroy).pack(side=tk.LEFT, padx=5)

    def select_all_except_best(self):
        self.tree_internal.selection_remove(*self.tree_internal.selection())
        to_select = []
        for group_id in self.tree_internal.get_children():
            children = self.tree_internal.get_children(group_id)
            for child_id in children[1:]:
                vals = self.tree_internal.item(child_id, "values")
                path = vals[-1] if vals else ""
                if path and self.is_protected(path):
                    continue
                to_select.append(child_id)
        if to_select:
            self.tree_internal.selection_set(*to_select)
            self.lbl_stat.config(text=f"Selected {len(to_select)} duplicate files (kept best from each group).")

    def auto_cull_internal(self):
        if not self.internal_dupes:
            return

        total_dupes = sum(len(g) - 1 for g in self.internal_dupes)
        if not messagebox.askyesno("Auto-Clean",
                f"Keep the KEEP file in each group (the best path, or the copy you "
                f"picked with Keep This Copy) and trash up to {total_dupes} "
                f"duplicate(s)?\n\nEach file is byte-verified against the kept "
                f"file first; anything that isn't identical is kept."):
            return

        groups = []
        for group in self.internal_dupes:
            scored = self._ordered_group(group)
            keeper = scored[0][0]
            cands = [p for p, s in scored[1:] if not self.is_protected(p)]
            if cands:
                groups.append((keeper, cands))

        if not groups:
            return

        self.btn_scan.config(state=tk.DISABLED)

        def _proceed(verified, mismatched):
            if not verified:
                self.btn_scan.config(state=tk.NORMAL)
                self.lbl_stat.config(text="Nothing deleted — no byte-identical duplicates.")
                messagebox.showinfo("Nothing deleted",
                    "None of the candidates were byte-for-byte identical to the "
                    "kept file, so nothing was trashed.")
                return

            def _done(trashed, failed, failed_paths=()):
                msg = f"Trashed {trashed} duplicate file(s)."
                if failed:
                    msg += f"\n{failed} could not be trashed."
                if mismatched:
                    msg += f"\n{len(mismatched)} kept (bytes differed from the kept file)."
                messagebox.showinfo("Done", msg)
                log.info(f"Auto-cull: {trashed} trashed, {failed} failed, "
                         f"{len(mismatched)} kept (not identical)")
                self.internal_dupes = []
                self.last_master_idx = {}
                self.last_target_idx = {}
                self.clear_trees()
                self.lbl_summary.config(text=f"Trashed {trashed} file(s). Run a new scan to verify.")
                self.btn_scan.config(state=tk.NORMAL)
                self.cleanup_empty_folders()

            self._trash_batch(verified, _done)

        self._verify_byte_identical(groups, _proceed)


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()