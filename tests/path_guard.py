"""Detects a local, personal file-system path in text. Patterns are assembled from pieces so this file does not
match itself."""
import re

_SEP = r"(?:\\{1,4}|/{1,2})"
_DRIVE = r"(?<![A-Za-z0-9])[A-Za-z]:" + _SEP + r"(?=[A-Za-z0-9_ .-])"                 # C:\x  C:\\x  c:/x  C://x  (not https://)
_MSYS = r"(?<![A-Za-z0-9_.-])/[a-z]/(?:Users|Walter|Projects|Documents|Work|Desktop|Downloads|home)/"   # /c/Users/x
_HOME = r"(?<![A-Za-z0-9_.-])/(?:home|Users)/[A-Za-z0-9_.-]+/"                                          # /home/x/  /Users/x/
_APPDATA = r"AppData" + _SEP + r"(?:Local|Roaming)"
_RX = re.compile("|".join((_DRIVE, _MSYS, _HOME, _APPDATA)))


# Generic examples that are not anyone's machine: the default Chrome install folder the browser scripts fall back to, and the
# README's example of putting a checkpoint on another disk.
_GENERIC = re.compile(r"[A-Za-z]:[\\/]+Program Files|[A-Za-z]:[\\/]+other-disk[\\/]+refund-checkpoint")


def has_local_path(text: str) -> bool:
    return bool(_RX.search(_GENERIC.sub("", text or "")))
