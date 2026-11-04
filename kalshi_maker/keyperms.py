"""Private-key file permission checks.

POSIX: the file must be mode 600 or stricter (no group/other bits).
Windows: the file's DACL must grant access to the current user only. There must be no
inherited entries and no entries for SYSTEM, Administrators, Users, Everyone, etc.
The fix is printed in PowerShell syntax:

    icacls "<path>" /inheritance:r /grant:r "$($env:USERNAME):(R)"

The Windows check uses the Win32 security API through ctypes (advapi32), so it needs no
third-party package. It never reads the key's contents.
"""

from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path


class KeyPermissionError(Exception):
    pass


def icacls_fix(path: Path) -> str:
    return f'icacls "{path}" /inheritance:r /grant:r "$($env:USERNAME):(R)"'


@dataclass(frozen=True, slots=True)
class AceInfo:
    ace_type: int  # 0 = ACCESS_ALLOWED, 1 = ACCESS_DENIED, others = object/callback ACEs
    flags: int
    mask: int
    sid: str | None  # string SID, or None for ACE types we don't parse


ACCESS_ALLOWED_ACE_TYPE = 0
ACCESS_DENIED_ACE_TYPE = 1
INHERITED_ACE = 0x10


def evaluate_windows_acl(
    aces: list[AceInfo] | None, user_sid: str, path: Path, protected: bool
) -> None:
    """Pure policy check (unit-testable on any OS). `aces=None` means a NULL DACL."""
    if aces is None:
        raise KeyPermissionError(
            f"private key {path} has a NULL DACL (everyone has full access). Fix with:\n  "
            + icacls_fix(path)
        )
    problems: list[str] = []
    if not protected:
        problems.append("inherits permissions from its folder")
    allowed_for_user = False
    for a in aces:
        if a.ace_type == ACCESS_DENIED_ACE_TYPE:
            continue  # deny entries only ever reduce access
        if a.ace_type != ACCESS_ALLOWED_ACE_TYPE or a.sid is None:
            problems.append(f"has an unsupported access entry (type {a.ace_type})")
            continue
        if a.flags & INHERITED_ACE:
            problems.append(f"has an inherited entry for {a.sid}")
        if a.sid != user_sid:
            problems.append(f"grants access to another account ({a.sid})")
        elif a.mask:
            allowed_for_user = True
    if not allowed_for_user:
        problems.append("does not grant the current user read access")
    if problems:
        raise KeyPermissionError(
            f"private key {path} is not owner-only: "
            + "; ".join(dict.fromkeys(problems))
            + ". Fix in PowerShell with:\n  "
            + icacls_fix(path)
        )


def _windows_acl(path: Path) -> tuple[list[AceInfo] | None, str, bool]:
    """Return (aces or None for NULL DACL, current user SID, dacl_protected)."""
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    SE_FILE_OBJECT = 1
    DACL_SECURITY_INFORMATION = 0x4
    SE_DACL_PROTECTED = 0x1000
    TOKEN_QUERY = 0x8
    TOKEN_USER_CLASS = 1
    ACL_SIZE_INFORMATION_CLASS = 2

    class ACL_SIZE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("AceCount", wintypes.DWORD),
            ("AclBytesInUse", wintypes.DWORD),
            ("AclBytesFree", wintypes.DWORD),
        ]

    class ACE_HEADER(ctypes.Structure):
        _fields_ = [
            ("AceType", ctypes.c_ubyte),
            ("AceFlags", ctypes.c_ubyte),
            ("AceSize", wintypes.WORD),
        ]

    class ACCESS_ACE(ctypes.Structure):  # ACCESS_ALLOWED_ACE / ACCESS_DENIED_ACE layout
        _fields_ = [("Header", ACE_HEADER), ("Mask", wintypes.DWORD), ("SidStart", wintypes.DWORD)]

    def sid_to_str(psid: ctypes.c_void_p) -> str:
        out = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(psid, ctypes.byref(out)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return str(out.value)
        finally:
            kernel32.LocalFree(out)

    # current user SID
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        need = wintypes.DWORD()
        advapi32.GetTokenInformation(token, TOKEN_USER_CLASS, None, 0, ctypes.byref(need))
        buf = ctypes.create_string_buffer(need.value)
        if not advapi32.GetTokenInformation(token, TOKEN_USER_CLASS, buf, need, ctypes.byref(need)):
            raise ctypes.WinError(ctypes.get_last_error())
        user_psid = ctypes.c_void_p.from_buffer(buf).value  # TOKEN_USER.User.Sid
        user_sid = sid_to_str(ctypes.c_void_p(user_psid))
    finally:
        kernel32.CloseHandle(token)

    # file DACL
    pdacl = ctypes.c_void_p()
    psd = ctypes.c_void_p()
    advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD,
        ctypes.c_void_p, ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]  # fmt: skip
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    rc = advapi32.GetNamedSecurityInfoW(
        str(path), SE_FILE_OBJECT, DACL_SECURITY_INFORMATION,
        None, None, ctypes.byref(pdacl), None, ctypes.byref(psd),
    )  # fmt: skip
    if rc != 0:
        raise ctypes.WinError(rc)
    try:
        control = wintypes.WORD()
        revision = wintypes.DWORD()
        if not advapi32.GetSecurityDescriptorControl(
            psd, ctypes.byref(control), ctypes.byref(revision)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        protected = bool(control.value & SE_DACL_PROTECTED)
        if not pdacl.value:
            return None, user_sid, protected
        info = ACL_SIZE_INFORMATION()
        if not advapi32.GetAclInformation(
            pdacl, ctypes.byref(info), ctypes.sizeof(info), ACL_SIZE_INFORMATION_CLASS
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        aces: list[AceInfo] = []
        advapi32.GetAce.argtypes = [
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        for i in range(info.AceCount):
            pace = ctypes.c_void_p()
            if not advapi32.GetAce(pdacl, i, ctypes.byref(pace)):
                raise ctypes.WinError(ctypes.get_last_error())
            hdr = ACE_HEADER.from_address(pace.value)  # type: ignore[arg-type]
            if hdr.AceType in (ACCESS_ALLOWED_ACE_TYPE, ACCESS_DENIED_ACE_TYPE):
                ace = ACCESS_ACE.from_address(pace.value)  # type: ignore[arg-type]
                sid_addr = pace.value + ACCESS_ACE.SidStart.offset  # type: ignore[operator]
                aces.append(
                    AceInfo(
                        hdr.AceType, hdr.AceFlags, ace.Mask, sid_to_str(ctypes.c_void_p(sid_addr))
                    )
                )
            else:
                aces.append(AceInfo(hdr.AceType, hdr.AceFlags, 0, None))
        return aces, user_sid, protected
    finally:
        kernel32.LocalFree(psd)


def check_key_permissions(path: Path) -> None:
    """Raise KeyPermissionError unless only the current user can access the key file."""
    if not path.exists():
        raise KeyPermissionError(f"private key file not found: {path}")
    st = path.stat()
    if not stat.S_ISREG(st.st_mode):
        raise KeyPermissionError(f"private key path is not a regular file: {path}")
    if sys.platform == "win32":
        aces, user_sid, protected = _windows_acl(path)
        evaluate_windows_acl(aces, user_sid, path, protected)
        return
    if st.st_mode & 0o077:
        raise KeyPermissionError(
            f"private key {path} has permissions {oct(st.st_mode & 0o777)}; run: chmod 600 {path}"
        )
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        raise KeyPermissionError(f"private key {path} is not owned by the current user")
