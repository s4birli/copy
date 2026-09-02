#!/usr/bin/env python3
"""
Bir klasordeki TUM dosyalari (gizli dosyalar dahil) baska bir klasore kopyalar.

- Hedefte ayni goreli yol + ayni boyutta dosya varsa ATLAR.
- Baska bir program tarafindan kullanilan (kilitli) dosyalari da zorla kopyalar.
- Hata alinan dosyalarin TAM yolunu bir text dosyasina yazar.

Kullanim:
    python3 copy_folder.py <kaynak> <hedef> [-l hata_log.txt] [--dry-run] [-v]
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
import time
import traceback

CHUNK = 1024 * 1024  # 1 MB


# --------------------------------------------------------------------------
# Kilitli dosyalari zorla okuma
# --------------------------------------------------------------------------
def _windows_force_open(path: str):
    """Windows'ta baska process acik tutsa bile paylasimli okuma ile acar."""
    import ctypes
    from ctypes import wintypes

    GENERIC_READ = 0x80000000
    FILE_SHARE_ALL = 0x00000001 | 0x00000002 | 0x00000004  # READ | WRITE | DELETE
    OPEN_EXISTING = 3
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    CreateFileW = ctypes.windll.kernel32.CreateFileW
    CreateFileW.restype = wintypes.HANDLE

    handle = CreateFileW(
        ctypes.c_wchar_p("\\\\?\\" + os.path.abspath(path)),
        GENERIC_READ,
        FILE_SHARE_ALL,
        None,
        OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_SEQUENTIAL_SCAN,
        None,
    )
    if not handle or handle == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())

    import msvcrt

    fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    return os.fdopen(fd, "rb", CHUNK)


def open_source(path: str):
    """Kaynak dosyayi okumak icin acar; kilitliyse zorlamayi dener."""
    try:
        return open(path, "rb", buffering=CHUNK)
    except PermissionError:
        if os.name == "nt":
            return _windows_force_open(path)
        # POSIX: izin sorunuysa izinleri gecici olarak acmayi dene
        try:
            os.chmod(path, os.stat(path).st_mode | stat.S_IRUSR)
            return open(path, "rb", buffering=CHUNK)
        except Exception:
            raise


def force_copy(src: str, dst: str) -> None:
    """Kilitli/kullanimda olan dosyalari da kopyalar, metadata'yi korur."""
    tmp = dst + ".part_copy_tmp"
    try:
        with open_source(src) as fsrc:
            try:
                fdst = open(tmp, "wb", buffering=CHUNK)
            except PermissionError:
                # hedefte read-only bir kalinti varsa temizle
                if os.path.exists(tmp):
                    os.chmod(tmp, stat.S_IWRITE | stat.S_IREAD)
                    os.remove(tmp)
                fdst = open(tmp, "wb", buffering=CHUNK)
            with fdst:
                shutil.copyfileobj(fsrc, fdst, CHUNK)

        # hedefte eski dosya varsa (read-only olsa bile) uzerine yaz
        if os.path.exists(dst):
            try:
                os.chmod(dst, stat.S_IWRITE | stat.S_IREAD)
            except OSError:
                pass
            os.remove(dst)
        os.replace(tmp, dst)

        try:
            shutil.copystat(src, dst)
        except OSError:
            pass  # metadata kopyalanamadi, dosya yine de tamam
    except BaseException:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise


# --------------------------------------------------------------------------
# Ana akis
# --------------------------------------------------------------------------
def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} PB"


def copy_tree(src_root: str, dst_root: str, log_path: str,
              dry_run: bool = False, verbose: bool = False) -> int:
    src_root = os.path.abspath(os.path.expanduser(src_root))
    dst_root = os.path.abspath(os.path.expanduser(dst_root))

    if not os.path.isdir(src_root):
        print(f"HATA: kaynak klasor bulunamadi: {src_root}", file=sys.stderr)
        return 2
    if os.path.normcase(dst_root).startswith(os.path.normcase(src_root) + os.sep):
        print("HATA: hedef klasor kaynagin icinde olamaz.", file=sys.stderr)
        return 2

    copied = skipped = failed = 0
    copied_bytes = 0
    errors: list[tuple[str, str]] = []
    started = time.time()

    for dirpath, dirnames, filenames in os.walk(src_root, followlinks=False):
        rel_dir = os.path.relpath(dirpath, src_root)
        target_dir = dst_root if rel_dir == "." else os.path.join(dst_root, rel_dir)

        if not dry_run:
            try:
                os.makedirs(target_dir, exist_ok=True)
            except OSError as exc:
                errors.append((dirpath, f"klasor olusturulamadi: {exc}"))
                failed += len(filenames)
                dirnames[:] = []
                continue

        for name in filenames:  # os.walk gizli dosyalari da verir
            src = os.path.join(dirpath, name)
            dst = os.path.join(target_dir, name)
            try:
                src_stat = os.lstat(src)

                # symlink'leri oldugu gibi kopyala
                if stat.S_ISLNK(src_stat.st_mode):
                    if os.path.lexists(dst):
                        skipped += 1
                        continue
                    if not dry_run:
                        os.symlink(os.readlink(src), dst)
                    copied += 1
                    continue

                if not stat.S_ISREG(src_stat.st_mode):
                    continue  # fifo/socket/device -> atla

                size = src_stat.st_size

                # ATLAMA KURALI: ayni goreli yol + ayni boyut
                if os.path.exists(dst) and os.path.getsize(dst) == size:
                    skipped += 1
                    if verbose:
                        print(f"[ATLA] {src}")
                    continue

                if not dry_run:
                    force_copy(src, dst)
                copied += 1
                copied_bytes += size
                if verbose:
                    print(f"[KOPYA] {src} -> {dst} ({human(size)})")

            except Exception as exc:
                failed += 1
                errors.append((src, f"{type(exc).__name__}: {exc}"))
                print(f"[HATA] {src}: {exc}", file=sys.stderr)

    # ---- hata raporu ----
    if errors:
        try:
            with open(log_path, "w", encoding="utf-8") as f:
                f.write(f"Kopyalama hata raporu - {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"Kaynak : {src_root}\n")
                f.write(f"Hedef  : {dst_root}\n")
                f.write(f"Toplam hatali dosya: {len(errors)}\n")
                f.write("=" * 78 + "\n\n")
                for path, msg in errors:
                    f.write(f"{path}\n    -> {msg}\n\n")
            print(f"\nHata raporu yazildi: {os.path.abspath(log_path)}")
        except OSError as exc:
            print(f"Hata raporu yazilamadi ({log_path}): {exc}", file=sys.stderr)
            print("--- Hatali dosyalar ---", file=sys.stderr)
            for path, msg in errors:
                print(f"{path} -> {msg}", file=sys.stderr)
    else:
        print("\nHicbir hata olusmadi, log dosyasi olusturulmadi.")

    elapsed = time.time() - started
    print(f"\n{'DRY-RUN ' if dry_run else ''}OZET")
    print(f"  Kopyalanan : {copied} dosya ({human(copied_bytes)})")
    print(f"  Atlanan    : {skipped} dosya (zaten var, ayni boyut)")
    print(f"  Hatali     : {failed} dosya")
    print(f"  Sure       : {elapsed:.1f} sn")

    return 1 if errors else 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="Klasoru gizli dosyalar dahil kopyalar; var olanlari atlar, "
                    "kilitli dosyalari zorlar, hatalari log'a yazar.")
    p.add_argument("source", help="Kaynak klasor")
    p.add_argument("dest", help="Hedef klasor")
    p.add_argument("-l", "--log", default="copy_errors.txt",
                   help="Hata log dosyasi (varsayilan: copy_errors.txt)")
    p.add_argument("--dry-run", action="store_true", help="Sadece dene, kopyalama")
    p.add_argument("-v", "--verbose", action="store_true", help="Her dosyayi yazdir")
    a = p.parse_args()

    try:
        return copy_tree(a.source, a.dest, a.log, a.dry_run, a.verbose)
    except KeyboardInterrupt:
        print("\nKullanici tarafindan durduruldu.", file=sys.stderr)
        return 130
    except Exception:
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(main())
