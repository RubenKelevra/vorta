import argparse
import errno
import fnmatch
import getpass
import os
import platform
import re
import sys
import unicodedata
from datetime import datetime as dt
from functools import reduce
from typing import Any, Callable, Iterable, Tuple
import psutil
from paramiko import SSHException
from paramiko.ecdsakey import ECDSAKey
from paramiko.ed25519key import Ed25519Key
from paramiko.rsakey import RSAKey
from PyQt5 import QtCore
from PyQt5.QtCore import QFileInfo, QThread, pyqtSignal
from PyQt5.QtWidgets import QApplication, QFileDialog, QSystemTrayIcon
from vorta.borg._compatibility import BorgCompatibility
from vorta.i18n import trans_late
from vorta.log import logger
from vorta.network_status.abc import NetworkStatusMonitor

QApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)  # enable highdpi scaling
QApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps, True)  # use highdpi icons

borg_compat = BorgCompatibility()
_network_status_monitor = None


class FilePathInfoAsync(QThread):
    signal = pyqtSignal(str, str, str)

    def __init__(self, path, exclude_patterns_str):
        self.path = path
        QThread.__init__(self)
        self.exiting = False
        self.exclude_patterns = []
        for _line in (exclude_patterns_str or '').splitlines():
            line = _line.strip()
            if line != '':
                self.exclude_patterns.append(line)

    def run(self):
        # logger.info("running thread to get path=%s...", self.path)
        self.size, self.files_count = get_path_datasize(self.path, self.exclude_patterns)
        self.signal.emit(self.path, str(self.size), str(self.files_count))


def normalize_path(path):
    """normalize paths for MacOS (but do nothing on other platforms)"""
    # HFS+ converts paths to a canonical form, so users shouldn't be required to enter an exact match.
    # Windows and Unix filesystems allow different forms, so users always have to enter an exact match.
    return unicodedata.normalize('NFD', path) if sys.platform == 'darwin' else path


# prepare patterns as borg does
# see `FnmatchPattern._prepare` at
# https://github.com/borgbackup/borg/blob/master//src/borg/patterns.py
def prepare_pattern(pattern):
    """Prepare and process fnmatch patterns as borg does"""
    if pattern.endswith(os.path.sep):
        # trailing sep indicates that the contents should be excluded
        # but not the directory it self.
        pattern = os.path.normpath(pattern).rstrip(os.path.sep)
        pattern += os.path.sep + '*' + os.path.sep
    else:
        pattern = os.path.normpath(pattern) + os.path.sep + '*'

    pattern = pattern.lstrip(os.path.sep)  # sep at beginning is removed
    return re.compile(fnmatch.translate(pattern))


def match(pattern: re.Pattern, path: str):
    """Check whether a path matches the given pattern."""
    path = path.lstrip(os.path.sep) + os.path.sep
    return pattern.match(path) is not None


def get_directory_size(dir_path, exclude_patterns):
    '''Get number of files only and total size in bytes from a path.
    Based off https://stackoverflow.com/a/17936789'''
    exclude_patterns = [prepare_pattern(p) for p in exclude_patterns]

    data_size_filtered = 0
    seen = set()
    seen_filtered = set()

    for dir_path, subdirectories, file_names in os.walk(dir_path, topdown=True):
        is_excluded = False
        for pattern in exclude_patterns:
            if match(pattern, dir_path):
                is_excluded = True
                break

        if is_excluded:
            subdirectories.clear()  # so that os.walk won't walk them
            continue

        for file_name in file_names:
            file_path = os.path.join(dir_path, file_name)

            # Ignore symbolic links, since borg doesn't follow them
            if os.path.islink(file_path):
                continue

            is_excluded = False
            for pattern in exclude_patterns:
                if match(pattern, file_path):
                    is_excluded = True
                    break

            try:
                stat = os.stat(file_path)
                if stat.st_ino not in seen:  # Visit each file only once
                    # this won't add the size of a hardlinked file
                    seen.add(stat.st_ino)
                    if not is_excluded:
                        data_size_filtered += stat.st_size
                        seen_filtered.add(stat.st_ino)
            except (FileNotFoundError, PermissionError):
                continue

    files_count_filtered = len(seen_filtered)

    return data_size_filtered, files_count_filtered


def get_network_status_monitor():
    global _network_status_monitor
    if _network_status_monitor is None:
        _network_status_monitor = NetworkStatusMonitor.get_network_status_monitor()
        logger.info(
            'Using %s NetworkStatusMonitor implementation.',
            _network_status_monitor.__class__.__name__,
        )
    return _network_status_monitor


def get_path_datasize(path, exclude_patterns):
    file_info = QFileInfo(path)
    data_size = 0

    if file_info.isDir():
        data_size, files_count = get_directory_size(file_info.absoluteFilePath(), exclude_patterns)
        # logger.info("path (folder) %s %u elements size now=%u (%s)",
        #            file_info.absoluteFilePath(), files_count, data_size, pretty_bytes(data_size))
    else:
        # logger.info("path (file) %s size=%u", file_info.path(), file_info.size())
        data_size = file_info.size()
        files_count = 1

    return data_size, files_count


def nested_dict():
    """
    Combination of two idioms to quickly build dicts from lists of keys:

    - https://stackoverflow.com/a/16724937/3983708
    - https://stackoverflow.com/a/14692747/3983708
    """
    return dict()


def get_dict_from_list(dataDict, mapList):
    return reduce(lambda d, k: d.setdefault(k, {}), mapList, dataDict)


def choose_file_dialog(parent, title, want_folder=True):
    dialog = QFileDialog(parent, title, os.path.expanduser('~'))
    dialog.setFileMode(QFileDialog.Directory if want_folder else QFileDialog.ExistingFiles)
    dialog.setParent(parent, QtCore.Qt.Sheet)
    if want_folder:
        dialog.setOption(QFileDialog.ShowDirsOnly)
    return dialog


def get_private_keys():
    """Find SSH keys in standard folder."""
    key_formats = [RSAKey, ECDSAKey, Ed25519Key]

    ssh_folder = os.path.expanduser('~/.ssh')

    available_private_keys = []
    if os.path.isdir(ssh_folder):
        for key in os.listdir(ssh_folder):
            key_file = os.path.join(ssh_folder, key)
            if not os.path.isfile(key_file):
                continue
            for key_format in key_formats:
                try:
                    parsed_key = key_format.from_private_key_file(key_file)
                    key_details = {
                        'filename': key,
                        'format': parsed_key.get_name(),
                        'bits': parsed_key.get_bits(),
                        'fingerprint': parsed_key.get_fingerprint().hex(),
                    }
                    available_private_keys.append(key_details)
                except (
                    SSHException,
                    UnicodeDecodeError,
                    IsADirectoryError,
                    IndexError,
                    ValueError,
                    PermissionError,
                    NotImplementedError,
                ):
                    logger.debug(
                        f'Expected error parsing file in .ssh: {key} (You can safely ignore this)', exc_info=True
                    )
                    continue
                except OSError as e:
                    if e.errno == errno.ENXIO:
                        # when key_file is a (ControlPath) socket
                        continue
                    else:
                        raise

    return available_private_keys


def sort_sizes(size_list):
    """Sorts sizes with extensions. Assumes that size is already in largest unit possible"""
    final_list = []
    for suffix in [" B", " KB", " MB", " GB", " TB"]:
        sub_list = [
            float(size[: -len(suffix)])
            for size in size_list
            if size.endswith(suffix) and size[: -len(suffix)][-1].isnumeric()
        ]
        sub_list.sort()
        final_list += [(str(size) + suffix) for size in sub_list]
        # Skip additional loops
        if len(final_list) == len(size_list):
            break
    return final_list


def pretty_bytes(size, metric=True, sign=False, precision=1):
    if not isinstance(size, int):
        return ''
    prefix = '+' if sign and size > 0 else ''
    power, units = (
        (10**3, ['', 'K', 'M', 'G', 'T', 'P', 'E', 'Z', 'Y'])
        if metric
        else (2**10, ['', 'Ki', 'Mi', 'Gi', 'Ti', 'Pi', 'Ei', 'Zi', 'Yi'])
    )
    n = 0
    while abs(round(size, precision)) >= power and n + 1 < len(units):
        size /= power
        n += 1
    try:
        unit = units[n]
        return f'{prefix}{round(size, precision)} {unit}B'
    except KeyError as e:
        logger.error(e)
        return "NaN"


def get_asset(path):
    if getattr(sys, 'frozen', False):
        # we are running in a bundle
        bundle_dir = os.path.join(sys._MEIPASS, 'assets')
    else:
        # we are running in a normal Python environment
        bundle_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'assets')
    return os.path.join(bundle_dir, path)


def get_sorted_wifis(profile):
    """
    Get Wifi networks known to the OS (only current one on macOS) and
    merge with networks from other profiles. Update last connected time.
    """

    from vorta.store.models import WifiSettingModel

    # Pull networks known to OS and all other backup profiles
    system_wifis = get_network_status_monitor().get_known_wifis()
    from_other_profiles = WifiSettingModel.select().where(WifiSettingModel.profile != profile.id).execute()

    for wifi in list(from_other_profiles) + system_wifis:
        db_wifi, created = WifiSettingModel.get_or_create(
            ssid=wifi.ssid,
            profile=profile.id,
            defaults={'last_connected': wifi.last_connected, 'allowed': True},
        )

        # Update last connected time
        if not created and db_wifi.last_connected != wifi.last_connected:
            db_wifi.last_connected = wifi.last_connected
            db_wifi.save()

    # Finally return list of networks and settings for that profile
    return (
        WifiSettingModel.select()
        .where(WifiSettingModel.profile == profile.id)
        .order_by(-WifiSettingModel.last_connected)
    )


def parse_args():
    parser = argparse.ArgumentParser(description='Vorta Backup GUI for Borg.')
    parser.add_argument('--version', '-V', action='store_true', help="Show version and exit.")
    parser.add_argument(
        '--daemonize',
        '-d',
        action='store_true',
        help="Fork to background and don't open window on startup.",
    )
    parser.add_argument(
        '--create',
        dest='profile',
        help='Create a backup in the background using the given profile. '
        'Vorta must already be running for this to work.',
    )

    return parser.parse_known_args()[0]


def slugify(value):
    """
    Converts to lowercase, removes non-word characters (alphanumerics and
    underscores) and converts spaces to hyphens. Also strips leading and
    trailing whitespace.

    Copied from Django.
    """
    value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode('ascii')
    value = re.sub(r'[^\w\s-]', '', value).strip().lower()
    return re.sub(r'[-\s]+', '-', value)


def uses_dark_mode():
    """
    This function detects if we are running in dark mode (e.g. macOS dark mode).
    """
    palette = QApplication.instance().palette()
    return palette.windowText().color().lightness() > palette.window().color().lightness()


def format_archive_name(profile, archive_name_tpl):
    """
    Generate an archive name. Default set in models.BackupProfileModel
    """
    available_vars = {
        'hostname': platform.node(),
        'profile_id': profile.id,
        'profile_slug': profile.slug(),
        'now': dt.now(),
        'utc_now': dt.utcnow(),
        'user': getpass.getuser(),
    }
    return archive_name_tpl.format(**available_vars)


SHELL_PATTERN_ELEMENT = re.compile(r'([?\[\]*])')


def get_mount_points(repo_url):
    mount_points = {}
    repo_mounts = []
    for proc in psutil.process_iter():
        try:
            name = proc.name()
            if name == 'borg' or name.startswith('python'):
                if 'mount' not in proc.cmdline():
                    continue

                if borg_compat.check('V2'):
                    # command line syntax:
                    # `borg mount -r <repo> <mountpoint> <path> (-a <archive_pattern>)`
                    cmd = proc.cmdline()
                    if repo_url in cmd:
                        i = cmd.index(repo_url)
                        if len(cmd) > i + 1:
                            mount_point = cmd[i + 1]

                            # Archive mount?
                            ao = '-a' in cmd
                            if ao or '--match-archives' in cmd:
                                i = cmd.index('-a' if ao else '--match-archives')
                                if len(cmd) >= i + 1 and not SHELL_PATTERN_ELEMENT.search(cmd[i + 1]):
                                    mount_points[mount_point] = cmd[i + 1]
                            else:
                                repo_mounts.append(mount_point)
                else:
                    for idx, parameter in enumerate(proc.cmdline()):
                        if parameter.startswith(repo_url):
                            # mount from this repo

                            # The borg mount command specifies that the mount_point
                            # parameter comes after the archive name
                            if len(proc.cmdline()) > idx + 1:
                                mount_point = proc.cmdline()[idx + 1]

                                # archive or full mount?
                                if parameter[len(repo_url) :].startswith('::'):
                                    archive_name = parameter[len(repo_url) + 2 :]
                                    mount_points[archive_name] = mount_point
                                    break
                                else:
                                    # repo mount point
                                    repo_mounts.append(mount_point)

        except (psutil.ZombieProcess, psutil.AccessDenied, psutil.NoSuchProcess):
            # Getting process details may fail (e.g. zombie process on macOS)
            # or because the process is owned by another user.
            # Also see https://github.com/giampaolo/psutil/issues/783
            continue

    return mount_points, repo_mounts


def is_system_tray_available():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
        tray = QSystemTrayIcon()
        is_available = tray.isSystemTrayAvailable()
        app.quit()
    else:
        tray = QSystemTrayIcon()
        is_available = tray.isSystemTrayAvailable()

    return is_available


def validate_passwords(first_pass, second_pass):
    '''Validates the password for borg, do not use on single fields'''
    pass_equal = first_pass == second_pass
    pass_long = len(first_pass) > 8

    if not pass_long and not pass_equal:
        return trans_late('utils', "Passwords must be identical and greater than 8 characters long.")
    if not pass_equal:
        return trans_late('utils', "Passwords must be identical.")
    if not pass_long:
        return trans_late('utils', "Passwords must be greater than 8 characters long.")

    return ""


def search(key, iterable: Iterable, func: Callable = None) -> Tuple[int, Any]:
    """
    Search for a key in an iterable.

    Before comparing an item with the key `func` is called on the item.

    Parameters
    ----------
    key : Any
        The key to search for.
    iterable : Iterable
        The iterable to search in.
    func : Callable, optional
        The function to apply, by default None

    Returns
    -------
    Tuple[int, Any] or None
        The index and the item in case of a match else `None`.
    """
    if not func:

        def func(x):
            return x

    for i, item in enumerate(iterable):
        if func(item) == key:
            return i, item

    return None
