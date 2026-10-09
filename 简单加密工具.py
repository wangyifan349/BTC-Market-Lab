#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ChaCha20-Poly1305 文件与目录加解密工具（单文件版，PyQt6 护眼橙红界面）
"""

from __future__ import annotations

import contextlib
import fnmatch
import functools
import hashlib
import html
import json
import os
import platform
import random
import stat
import string
import struct
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from PyQt6.QtCore import QObject, QThread, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QColor, QFont, QPalette
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

FILE_MAGIC = b"CPF1"
FORMAT_VERSION = 1
DEFAULT_SUFFIX = ".cpf"
FALLBACK_SUFFIX = ".enc"
DECRYPTED_SUFFIX = ".dec"

KDF_PBKDF2_ID = 1
KDF_SCRYPT_ID = 2
KDF_NAME_TO_ID = {"pbkdf2": KDF_PBKDF2_ID, "pbkdf2-sha256": KDF_PBKDF2_ID, "scrypt": KDF_SCRYPT_ID}
KDF_ID_TO_NAME = {KDF_PBKDF2_ID: "pbkdf2", KDF_SCRYPT_ID: "scrypt"}

SALT_SIZE = 16
KEY_SIZE = 32
NONCE_SIZE = 12
NONCE_PREFIX_SIZE = 8
TAG_SIZE = 16

DEFAULT_CHUNK_SIZE = 4 << 20
MAXIMUM_CHUNK_SIZE = 1 << 30
DEFAULT_PBKDF2_ITERATIONS = 600000
DEFAULT_SCRYPT_LOG_N = 14
MAXIMUM_SCRYPT_LOG_N = 20

STREAM_BUFFER_SIZE = 1024 * 1024
COMPARE_BLOCK_SIZE = 1 << 20

HEADER_FIXED_FIRST = struct.Struct(">4sBBIB")
HEADER_FIXED_SECOND = struct.Struct(">8sIQIQ")
HEADER_CHUNK = struct.Struct(">IB")
CHUNK_LENGTH = struct.Struct(">I")
NONCE_COUNTER = struct.Struct(">I")

VAULT_DIRECTORY = Path.home() / ".cpf_tool"
VAULT_FILE_PATH = VAULT_DIRECTORY / "vault.cpf"
VAULT_AAD = b"cpf-tool-vault-v1"
VAULT_MAXIMUM_ENTRIES = 300
VAULT_KDF_ITERATIONS = 120000

LOG_COLOR_MAP = {
    "info": "#C98A00",
    "ok": "#FFC000",
    "warn": "#FF9A00",
    "err": "#FF5A00",
}
LOG_TIMESTAMP_COLOR = "#8F6200"

STYLE_SHEET = """
* {
    font-family: "Microsoft YaHei", "PingFang SC", "Segoe UI", "Noto Sans SC", sans-serif;
    font-size: 15px;
    color: #FFB000;
    outline: none;
}
QMainWindow, QWidget#centralWidget {
    background-color: #1A0E00;
}
QLabel#appTitle {
    font-size: 25px;
    font-weight: 800;
    color: #FFD000;
}
QLabel#appSubtitle {
    font-size: 14px;
    color: #C98A00;
}
QLabel#sectionTitle {
    font-size: 17px;
    font-weight: 800;
    color: #FFC000;
}
QLabel#hintLabel {
    font-size: 14px;
    color: #C98A00;
}
QFrame#mainCard {
    background-color: #241400;
    border: 1px solid #5A3200;
    border-radius: 14px;
}
QFrame#sideCard {
    background-color: #200F00;
    border: 1px solid #4A2800;
    border-radius: 14px;
}
QLineEdit, QComboBox, QSpinBox, QPlainTextEdit, QTextEdit, QTableWidget {
    background-color: #140A00;
    border: 1px solid #5A3200;
    border-radius: 10px;
    padding: 8px 12px;
    selection-background-color: #B34700;
    selection-color: #FFE100;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus, QTextEdit:focus {
    border: 1px solid #FF6600;
}
QLineEdit#passwordField {
    font-size: 20px;
    min-height: 30px;
    padding: 11px 14px;
    letter-spacing: 2px;
    background-color: #1A0E00;
}
QComboBox::drop-down {
    border: none;
    width: 30px;
}
QComboBox QAbstractItemView {
    background-color: #1A0E00;
    border: 1px solid #5A3200;
    selection-background-color: #B34700;
    selection-color: #FFE100;
}
QSpinBox::up-button, QSpinBox::down-button {
    background-color: #2A1600;
    border: 1px solid #5A3200;
    border-radius: 5px;
    width: 22px;
    height: 16px;
}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {
    background-color: #3A1F00;
}
QPushButton {
    background-color: #2E1800;
    border: 1px solid #6E3C00;
    border-radius: 10px;
    padding: 9px 16px;
    font-size: 15px;
}
QPushButton:hover {
    background-color: #3A1F00;
    border-color: #8A4E00;
}
QPushButton:pressed {
    background-color: #241200;
}
QPushButton:disabled {
    color: #6B4A00;
    background-color: #201200;
    border-color: #4A2800;
}
QPushButton#runButton {
    background-color: #FF6600;
    color: #1A0A00;
    border: none;
    font-weight: 800;
    font-size: 18px;
    padding: 15px 24px;
}
QPushButton#runButton:hover {
    background-color: #FF8000;
}
QPushButton#runButton:pressed {
    background-color: #E65C00;
}
QPushButton#runButton:disabled {
    background-color: #8A3A00;
    color: #C98A00;
}
QToolButton {
    background-color: #241200;
    border: 1px solid #5A3200;
    border-radius: 10px;
    padding: 9px 14px;
    font-size: 15px;
}
QToolButton:checked {
    background-color: #FF6600;
    color: #1A0A00;
}
QCheckBox {
    spacing: 10px;
    font-size: 15px;
}
QCheckBox::indicator {
    width: 20px;
    height: 20px;
    border: 1px solid #805000;
    border-radius: 6px;
    background-color: #140A00;
}
QCheckBox::indicator:checked {
    background-color: #FF6600;
    border-color: #FF6600;
}
QCheckBox::indicator:hover {
    border-color: #FF6600;
}
QProgressBar {
    background-color: #140A00;
    border: 1px solid #5A3200;
    border-radius: 9px;
    text-align: center;
    color: #FFD000;
    height: 22px;
    font-weight: 800;
    font-size: 14px;
}
QProgressBar::chunk {
    background-color: #FF7A00;
    border-radius: 8px;
}
QTextEdit {
    font-family: "Consolas", "Cascadia Mono", monospace;
    font-size: 14px;
    padding: 8px;
}
QStatusBar {
    background-color: #140A00;
    color: #C98A00;
    font-size: 14px;
}
QScrollBar:vertical {
    background: transparent;
    width: 12px;
}
QScrollBar::handle:vertical {
    background: #6E3C00;
    border-radius: 6px;
    min-height: 34px;
}
QScrollBar::handle:vertical:hover {
    background: #8A4E00;
}
QScrollBar::add-line, QScrollBar::sub-line {
    height: 0;
}
QHeaderView::section {
    background-color: #2E1800;
    color: #FFC000;
    border: none;
    padding: 6px 8px;
    font-weight: 700;
}
"""


class CryptToolError(Exception):
    pass


class FormatError(CryptToolError):
    pass


class AuthenticationError(CryptToolError):
    pass


class FileOperationError(CryptToolError):
    pass


@dataclass(slots=True)
class HeaderMeta:
    kdf_id: int
    iterations: int
    salt: bytes
    nonce_prefix: bytes
    chunk_size: int
    plaintext_size: int
    file_mode: int
    file_mtime: int
    header_bytes: bytes

    @property
    def kdf_name(self) -> str:
        return KDF_ID_TO_NAME.get(self.kdf_id, "unknown")


def resolve_kdf_id(kdf_name):
    normalized = str(kdf_name).lower()
    return KDF_NAME_TO_ID.get(normalized, KDF_PBKDF2_ID)


def resolve_kdf_name(kdf_id):
    return KDF_ID_TO_NAME.get(kdf_id, "unknown")


def effective_suffix(suffix):
    if suffix:
        return suffix
    return FALLBACK_SUFFIX


def clamp_chunk_size(chunk_size):
    value = int(chunk_size)
    if value <= TAG_SIZE:
        return DEFAULT_CHUNK_SIZE
    if value > MAXIMUM_CHUNK_SIZE:
        return MAXIMUM_CHUNK_SIZE
    return value


def normalize_password(password):
    if isinstance(password, bytes):
        return password
    if isinstance(password, str):
        return password.encode("utf-8")
    raise TypeError("密码类型错误，应为 str 或 bytes")


def describe_error(error):
    return type(error).__name__ + ": " + str(error)


def safe_unlink(path):
    with contextlib.suppress(OSError):
        Path(path).unlink()


def report_progress(progress_callback, event, path, done, total):
    if progress_callback is None:
        return
    try:
        progress_callback(event, path, done, total)
    except Exception:
        pass


def derive_key(password, salt, kdf_id, iterations, key_size=KEY_SIZE):
    if not salt:
        raise ValueError("salt 不能为空")
    password_bytes = normalize_password(password)
    if kdf_id == KDF_PBKDF2_ID:
        effective_iterations = int(iterations)
        if effective_iterations < 1000:
            effective_iterations = 1000
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=key_size,
            salt=salt,
            iterations=effective_iterations,
        )
        return kdf.derive(password_bytes)
    if kdf_id == KDF_SCRYPT_ID:
        log_n = int(iterations)
        if log_n < 5:
            log_n = 5
        if log_n > MAXIMUM_SCRYPT_LOG_N:
            log_n = MAXIMUM_SCRYPT_LOG_N
        kdf = Scrypt(salt=salt, length=key_size, n=1 << log_n, r=8, p=1)
        return kdf.derive(password_bytes)
    raise FormatError("不支持的密钥派生函数标识")


def validate_header_fields(magic, version, kdf_id, salt_length):
    if magic != FILE_MAGIC:
        raise FormatError("该文件不是本工具生成的密文")
    if version != FORMAT_VERSION:
        raise FormatError("不支持的格式版本：" + str(version))
    if kdf_id not in (KDF_PBKDF2_ID, KDF_SCRYPT_ID):
        raise FormatError("不支持的密钥派生函数标识")
    if salt_length < 1 or salt_length > 64:
        raise FormatError("salt 长度非法")


def build_header(kdf_id, iterations, salt, nonce_prefix, chunk_size, plaintext_size,
                 file_mode, file_mtime):
    first_part = HEADER_FIXED_FIRST.pack(FILE_MAGIC, FORMAT_VERSION, kdf_id,
                                         iterations, len(salt))
    second_part = HEADER_FIXED_SECOND.pack(nonce_prefix, chunk_size, plaintext_size,
                                           file_mode, file_mtime)
    return first_part + salt + second_part


def parse_header(stream):
    first_part = stream.read(HEADER_FIXED_FIRST.size)
    if len(first_part) < HEADER_FIXED_FIRST.size:
        raise FormatError("文件过短，无法读取头部")
    magic, version, kdf_id, iterations, salt_length = HEADER_FIXED_FIRST.unpack(first_part)
    validate_header_fields(magic, version, kdf_id, salt_length)
    salt = stream.read(salt_length)
    if len(salt) != salt_length:
        raise FormatError("salt 数据被截断")
    second_part = stream.read(HEADER_FIXED_SECOND.size)
    if len(second_part) < HEADER_FIXED_SECOND.size:
        raise FormatError("头部信息被截断")
    nonce_prefix, chunk_size, plaintext_size, file_mode, file_mtime = \
        HEADER_FIXED_SECOND.unpack(second_part)
    if chunk_size <= TAG_SIZE or chunk_size > MAXIMUM_CHUNK_SIZE:
        raise FormatError("分块大小非法")
    header_bytes = first_part + salt + second_part
    metadata = HeaderMeta(
        kdf_id=kdf_id,
        iterations=iterations,
        salt=salt,
        nonce_prefix=nonce_prefix,
        chunk_size=chunk_size,
        plaintext_size=plaintext_size,
        file_mode=file_mode,
        file_mtime=file_mtime,
        header_bytes=header_bytes,
    )
    return header_bytes, metadata


def read_header(path):
    source = Path(path)
    with open(source, "rb", buffering=STREAM_BUFFER_SIZE) as stream:
        header_bytes, metadata = parse_header(stream)
    return metadata


@contextlib.contextmanager
def atomic_output(destination, mode=None, times=None):
    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=str(destination_path.parent),
        prefix=".cpf_atomic_",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb", buffering=STREAM_BUFFER_SIZE) as stream:
            yield stream
        if mode is not None:
            with contextlib.suppress(OSError):
                os.chmod(temporary_path, mode)
        os.replace(temporary_path, destination_path)
        if times is not None:
            with contextlib.suppress(OSError):
                os.utime(destination_path, times)
    except BaseException:
        safe_unlink(temporary_path)
        raise


def write_atomic_bytes(path, data, mode=None):
    destination_path = Path(path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=str(destination_path.parent),
        prefix=".cpf_write_",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        if mode is not None:
            with contextlib.suppress(OSError):
                os.chmod(temporary_path, mode)
        os.replace(temporary_path, destination_path)
    except BaseException:
        safe_unlink(temporary_path)
        raise


def encrypt_stream(source_stream, destination_stream, key, kdf_id, iterations, salt,
                   chunk_size, plaintext_size, file_mode, file_mtime, aad,
                   progress_callback, source_path):
    nonce_prefix = os.urandom(NONCE_PREFIX_SIZE)
    header_bytes = build_header(kdf_id, iterations, salt, nonce_prefix, chunk_size,
                                plaintext_size, file_mode, file_mtime)
    destination_stream.write(header_bytes)
    cipher = ChaCha20Poly1305(key)
    total_written = 0
    chunk_index = 0
    while True:
        plain_block = source_stream.read(chunk_size)
        is_final = len(plain_block) < chunk_size
        nonce = nonce_prefix + NONCE_COUNTER.pack(chunk_index)
        final_flag = 1 if is_final else 0
        associated_data = header_bytes + HEADER_CHUNK.pack(chunk_index, final_flag) + aad
        cipher_block = cipher.encrypt(nonce, plain_block, associated_data)
        destination_stream.write(CHUNK_LENGTH.pack(len(cipher_block)))
        destination_stream.write(cipher_block)
        total_written += len(plain_block)
        chunk_index += 1
        report_progress(progress_callback, "encrypt", source_path, total_written,
                        plaintext_size)
        if is_final:
            break
    return total_written


def decrypt_stream(source_stream, destination_stream, key, metadata, aad,
                   progress_callback, source_path):
    cipher = ChaCha20Poly1305(key)
    nonce_prefix = metadata.nonce_prefix
    header_bytes = metadata.header_bytes
    plaintext_size = metadata.plaintext_size
    chunk_size = metadata.chunk_size
    final_chunk_index = plaintext_size // chunk_size
    total_written = 0
    chunk_index = 0
    while chunk_index <= final_chunk_index:
        length_bytes = source_stream.read(CHUNK_LENGTH.size)
        if len(length_bytes) < CHUNK_LENGTH.size:
            raise FormatError("密文块长度信息被截断")
        ciphertext_length = CHUNK_LENGTH.unpack(length_bytes)[0]
        if ciphertext_length < TAG_SIZE:
            raise FormatError("密文块长度非法")
        cipher_block = source_stream.read(ciphertext_length)
        if len(cipher_block) != ciphertext_length:
            raise FormatError("密文块内容被截断")
        final_flag = 1 if chunk_index == final_chunk_index else 0
        nonce = nonce_prefix + NONCE_COUNTER.pack(chunk_index)
        associated_data = header_bytes + HEADER_CHUNK.pack(chunk_index, final_flag) + aad
        try:
            plain_block = cipher.decrypt(nonce, cipher_block, associated_data)
        except InvalidTag as error:
            raise AuthenticationError(
                "认证失败：密码错误、数据被篡改、AAD 备注不一致或文件不完整"
            ) from error
        destination_stream.write(plain_block)
        total_written += len(plain_block)
        chunk_index += 1
        report_progress(progress_callback, "decrypt", source_path, total_written,
                        plaintext_size)
    if total_written != plaintext_size:
        raise AuthenticationError("明文长度与头部记录不一致，文件可能被截断")
    return total_written


def files_are_identical(first_path, second_path):
    with open(first_path, "rb", buffering=STREAM_BUFFER_SIZE) as first_stream, \
            open(second_path, "rb", buffering=STREAM_BUFFER_SIZE) as second_stream:
        while True:
            first_block = first_stream.read(COMPARE_BLOCK_SIZE)
            second_block = second_stream.read(COMPARE_BLOCK_SIZE)
            if first_block != second_block:
                return False
            if not first_block:
                return True


def verify_then_remove(source_path, encrypted_path, password, aad, verify_enabled):
    if verify_enabled:
        verification_path = encrypted_path.with_name(encrypted_path.name + ".verify.tmp")
        try:
            decrypt_file(encrypted_path, password, verification_path, overwrite=True,
                         remove_source=False, aad=aad)
            if not files_are_identical(source_path, verification_path):
                raise FileOperationError("回读校验失败，未删除源文件")
        finally:
            safe_unlink(verification_path)
    safe_unlink(source_path)


def encrypt_file(source_path, password, destination_path=None, *,
                 kdf_name="pbkdf2",
                 iterations=DEFAULT_PBKDF2_ITERATIONS,
                 chunk_size=DEFAULT_CHUNK_SIZE,
                 suffix=DEFAULT_SUFFIX,
                 overwrite=True,
                 remove_source=False,
                 verify=True,
                 aad=b"",
                 progress_callback=None,
                 precomputed_salt=None,
                 precomputed_key=None):
    source = Path(source_path)
    if not source.is_file():
        raise FileOperationError("不是文件：" + str(source))
    resolved_suffix = effective_suffix(suffix)
    if destination_path is None:
        destination = source.with_name(source.name + resolved_suffix)
    else:
        destination = Path(destination_path)
    if source.resolve() == destination.resolve():
        raise FileOperationError("输入与输出不能是同一个文件")
    if destination.exists() and not overwrite:
        raise FileOperationError("目标已存在，且未开启直接覆盖")
    stat_result = source.stat()
    kdf_id = resolve_kdf_id(kdf_name)
    effective_chunk_size = clamp_chunk_size(chunk_size)
    salt = precomputed_salt
    if salt is None:
        salt = os.urandom(SALT_SIZE)
    key = precomputed_key
    if key is None:
        key = derive_key(password, salt, kdf_id, iterations)
    file_mode = stat.S_IMODE(stat_result.st_mode)
    file_mtime = int(stat_result.st_mtime)
    plaintext_size = stat_result.st_size
    with open(source, "rb", buffering=STREAM_BUFFER_SIZE) as source_stream, \
            atomic_output(destination, mode=0o600) as destination_stream:
        written_size = encrypt_stream(source_stream, destination_stream, key, kdf_id,
                                      iterations, salt, effective_chunk_size,
                                      plaintext_size, file_mode, file_mtime, aad,
                                      progress_callback, source)
    if written_size != plaintext_size:
        safe_unlink(destination)
        raise FileOperationError("加密过程中源文件大小发生变化")
    report_progress(progress_callback, "done", source, written_size, written_size)
    if remove_source:
        verify_then_remove(source, destination, password, aad, verify)
    return destination


def build_plaintext_name(source, suffix):
    file_name = source.name
    if suffix:
        if file_name.endswith(suffix):
            trimmed = file_name[:-len(suffix)]
            if trimmed:
                return source.with_name(trimmed)
            return source.with_name(file_name + DECRYPTED_SUFFIX)
        return source.with_name(file_name + DECRYPTED_SUFFIX)
    if file_name.endswith(FALLBACK_SUFFIX):
        trimmed = file_name[:-len(FALLBACK_SUFFIX)]
        if trimmed:
            return source.with_name(trimmed)
        return source.with_name(file_name + DECRYPTED_SUFFIX)
    return source.with_name(file_name + DECRYPTED_SUFFIX)


def decrypt_file(source_path, password, destination_path=None, *,
                 kdf_name="pbkdf2",
                 iterations=DEFAULT_PBKDF2_ITERATIONS,
                 chunk_size=DEFAULT_CHUNK_SIZE,
                 suffix=DEFAULT_SUFFIX,
                 overwrite=True,
                 remove_source=False,
                 verify=False,
                 aad=b"",
                 progress_callback=None):
    source = Path(source_path)
    if not source.is_file():
        raise FileOperationError("不是文件：" + str(source))
    if destination_path is None:
        destination = build_plaintext_name(source, suffix)
    else:
        destination = Path(destination_path)
    if source.resolve() == destination.resolve():
        raise FileOperationError("输入与输出不能是同一个文件")
    if destination.exists() and not overwrite:
        raise FileOperationError("目标已存在，且未开启直接覆盖")
    with open(source, "rb", buffering=STREAM_BUFFER_SIZE) as source_stream:
        header_bytes, metadata = parse_header(source_stream)
        key = derive_key(password, metadata.salt, metadata.kdf_id, metadata.iterations)
        restored_times = (time.time(), metadata.file_mtime if metadata.file_mtime
                          else int(time.time()))
        with atomic_output(destination, mode=metadata.file_mode or 0o644,
                           times=restored_times) as destination_stream:
            total_written = decrypt_stream(source_stream, destination_stream, key,
                                           metadata, aad, progress_callback, source)
    report_progress(progress_callback, "done", source, total_written, total_written)
    if remove_source:
        safe_unlink(source)
    return destination


def is_excluded(relative_path, exclude_patterns):
    for pattern in exclude_patterns:
        pattern_text = str(pattern).strip()
        if not pattern_text:
            continue
        if fnmatch.fnmatch(relative_path, pattern_text):
            return True
        if fnmatch.fnmatch(Path(relative_path).name, pattern_text):
            return True
    return False


def filter_directory_names(directory_path, source_root, directory_names, exclude_patterns):
    kept_names = []
    for name in sorted(directory_names):
        relative_path = (directory_path / name).relative_to(source_root).as_posix()
        if is_excluded(relative_path, exclude_patterns):
            continue
        kept_names.append(name)
    return kept_names


def build_encryption_task(full_path, file_name, suffix, resolved_suffix):
    if suffix and file_name.endswith(suffix):
        return None
    target_name = file_name + resolved_suffix
    return full_path, full_path.with_name(target_name)


def build_decryption_task(full_path, file_name, suffix):
    if suffix and not file_name.endswith(suffix):
        return None
    if suffix:
        trimmed = file_name[:-len(suffix)]
        if trimmed:
            target_name = trimmed
        else:
            target_name = file_name + DECRYPTED_SUFFIX
    elif file_name.endswith(FALLBACK_SUFFIX):
        trimmed = file_name[:-len(FALLBACK_SUFFIX)]
        if trimmed:
            target_name = trimmed
        else:
            target_name = file_name + DECRYPTED_SUFFIX
    else:
        target_name = file_name + DECRYPTED_SUFFIX
    return full_path, full_path.with_name(target_name)


def build_directory_task(directory_path, source_root, file_name, encrypting, suffix,
                         resolved_suffix, exclude_patterns, follow_symlinks):
    if file_name.startswith(".cpf_atomic_"):
        return None
    if file_name.endswith(".verify.tmp"):
        return None
    full_path = directory_path / file_name
    if not follow_symlinks and full_path.is_symlink():
        return None
    if not full_path.is_file():
        return None
    relative_path = full_path.relative_to(source_root).as_posix()
    if is_excluded(relative_path, exclude_patterns):
        return None
    if encrypting:
        return build_encryption_task(full_path, file_name, suffix, resolved_suffix)
    return build_decryption_task(full_path, file_name, suffix)


def collect_directory_tasks(source_root, encrypting, suffix, exclude_patterns,
                            recursive, follow_symlinks):
    resolved_suffix = effective_suffix(suffix)
    collected_tasks = []
    for current_directory, directory_names, file_names in os.walk(source_root,
                                                                   followlinks=follow_symlinks):
        directory_path = Path(current_directory)
        if not recursive:
            directory_names[:] = []
        directory_names[:] = filter_directory_names(directory_path, source_root,
                                                    directory_names, exclude_patterns)
        for file_name in sorted(file_names):
            task = build_directory_task(directory_path, source_root, file_name, encrypting,
                                        suffix, resolved_suffix, exclude_patterns,
                                        follow_symlinks)
            if task is not None:
                collected_tasks.append(task)
    return collected_tasks


def resolve_output_root(destination_directory):
    if destination_directory is None:
        return None
    output_root = Path(destination_directory)
    if output_root.exists() and not output_root.is_dir():
        raise FileOperationError("输出路径不是目录：" + str(output_root))
    return output_root


def relocate_tasks(tasks, source_root, output_root):
    if output_root is None:
        return tasks
    relocated_tasks = []
    for source_file, target_file in tasks:
        relative_parent = source_file.relative_to(source_root).parent
        new_target = output_root / relative_parent / target_file.name
        relocated_tasks.append((source_file, new_target))
    return relocated_tasks


def attempt_single_task(task_function, source_file, target_file, succeeded, failed):
    try:
        succeeded.append(task_function(source_file, target_file))
    except Exception as error:
        failed.append((source_file, describe_error(error)))


def run_task_batch(tasks, task_function, worker_count):
    succeeded = []
    failed = []
    if worker_count <= 1 or len(tasks) <= 1:
        for source_file, target_file in tasks:
            attempt_single_task(task_function, source_file, target_file, succeeded, failed)
        return succeeded, failed
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        pending_futures = {}
        for source_file, target_file in tasks:
            future = executor.submit(task_function, source_file, target_file)
            pending_futures[future] = source_file
        for future in as_completed(pending_futures):
            source_file = pending_futures[future]
            try:
                succeeded.append(future.result())
            except Exception as error:
                failed.append((source_file, describe_error(error)))
    return succeeded, failed


def encrypt_one_file(source_file, target_file, password, kdf_name, iterations, chunk_size,
                     suffix, overwrite, remove_source, verify, aad, progress_callback,
                     precomputed_salt, precomputed_key):
    return encrypt_file(source_file, password, target_file, kdf_name=kdf_name,
                        iterations=iterations, chunk_size=chunk_size, suffix=suffix,
                        overwrite=overwrite, remove_source=remove_source, verify=verify,
                        aad=aad, progress_callback=progress_callback,
                        precomputed_salt=precomputed_salt,
                        precomputed_key=precomputed_key)


def decrypt_one_file(source_file, target_file, password, kdf_name, iterations, chunk_size,
                     suffix, overwrite, remove_source, verify, aad, progress_callback):
    return decrypt_file(source_file, password, target_file, kdf_name=kdf_name,
                        iterations=iterations, chunk_size=chunk_size, suffix=suffix,
                        overwrite=overwrite, remove_source=remove_source, verify=verify,
                        aad=aad, progress_callback=progress_callback)


def encrypt_directory(source_directory, password, destination_directory=None, *,
                      kdf_name="pbkdf2",
                      iterations=DEFAULT_PBKDF2_ITERATIONS,
                      chunk_size=DEFAULT_CHUNK_SIZE,
                      suffix=DEFAULT_SUFFIX,
                      overwrite=True,
                      remove_source=False,
                      verify=True,
                      aad=b"",
                      exclude_patterns=(),
                      follow_symlinks=False,
                      recursive=True,
                      worker_count=4,
                      shared_salt=False,
                      progress_callback=None):
    source_root = Path(source_directory)
    if not source_root.is_dir():
        raise FileOperationError("不是目录：" + str(source_root))
    output_root = resolve_output_root(destination_directory)
    tasks = collect_directory_tasks(source_root, True, suffix, exclude_patterns,
                                    recursive, follow_symlinks)
    tasks = relocate_tasks(tasks, source_root, output_root)
    if not tasks:
        return [], []
    precomputed_salt = None
    precomputed_key = None
    if shared_salt:
        precomputed_salt = os.urandom(SALT_SIZE)
        kdf_id = resolve_kdf_id(kdf_name)
        precomputed_key = derive_key(password, precomputed_salt, kdf_id, iterations)
        report_progress(progress_callback, "kdf", source_root, 0, 0)
    task_function = functools.partial(encrypt_one_file, password=password,
                                      kdf_name=kdf_name, iterations=iterations,
                                      chunk_size=chunk_size, suffix=suffix,
                                      overwrite=overwrite, remove_source=remove_source,
                                      verify=verify, aad=aad,
                                      progress_callback=progress_callback,
                                      precomputed_salt=precomputed_salt,
                                      precomputed_key=precomputed_key)
    return run_task_batch(tasks, task_function, max(1, int(worker_count)))


def decrypt_directory(source_directory, password, destination_directory=None, *,
                      kdf_name="pbkdf2",
                      iterations=DEFAULT_PBKDF2_ITERATIONS,
                      chunk_size=DEFAULT_CHUNK_SIZE,
                      suffix=DEFAULT_SUFFIX,
                      overwrite=True,
                      remove_source=False,
                      verify=False,
                      aad=b"",
                      exclude_patterns=(),
                      follow_symlinks=False,
                      recursive=True,
                      worker_count=4,
                      shared_salt=False,
                      progress_callback=None):
    source_root = Path(source_directory)
    if not source_root.is_dir():
        raise FileOperationError("不是目录：" + str(source_root))
    output_root = resolve_output_root(destination_directory)
    tasks = collect_directory_tasks(source_root, False, suffix, exclude_patterns,
                                    recursive, follow_symlinks)
    tasks = relocate_tasks(tasks, source_root, output_root)
    if not tasks:
        return [], []
    task_function = functools.partial(decrypt_one_file, password=password,
                                      kdf_name=kdf_name, iterations=iterations,
                                      chunk_size=chunk_size, suffix=suffix,
                                      overwrite=overwrite, remove_source=remove_source,
                                      verify=verify, aad=aad,
                                      progress_callback=progress_callback)
    return run_task_batch(tasks, task_function, max(1, int(worker_count)))


_machine_key_cache = None


def machine_key():
    global _machine_key_cache
    if _machine_key_cache is not None:
        return _machine_key_cache
    mac_address = format(uuid.getnode(), "012x")
    machine_parts = [
        mac_address,
        platform.node(),
        platform.machine(),
        platform.system(),
        os.environ.get("COMPUTERNAME", ""),
        os.environ.get("USERNAME", "") or os.environ.get("USER", ""),
    ]
    seed = "|".join(machine_parts).encode("utf-8", "surrogateescape")
    salt = hashlib.sha256(b"cpf-tool-vault-salt:" + seed).digest()[:SALT_SIZE]
    _machine_key_cache = hashlib.pbkdf2_hmac("sha256", seed, salt,
                                             VAULT_KDF_ITERATIONS, dklen=KEY_SIZE)
    return _machine_key_cache


def encrypt_vault_bytes(plain_bytes, key):
    nonce = os.urandom(NONCE_SIZE)
    cipher = ChaCha20Poly1305(key)
    return nonce + cipher.encrypt(nonce, plain_bytes, VAULT_AAD)


def decrypt_vault_bytes(cipher_bytes, key):
    if len(cipher_bytes) <= NONCE_SIZE:
        raise FormatError("密码库文件损坏")
    cipher = ChaCha20Poly1305(key)
    return cipher.decrypt(cipher_bytes[:NONCE_SIZE], cipher_bytes[NONCE_SIZE:], VAULT_AAD)


class PasswordVault:
    def __init__(self, path=VAULT_FILE_PATH):
        self.path = Path(path)
        self.entries = []
        self.last_error = None

    def load(self):
        self.entries = []
        self.last_error = None
        if not self.path.exists():
            return self
        try:
            decrypted_bytes = decrypt_vault_bytes(self.path.read_bytes(), machine_key())
            payload = json.loads(decrypted_bytes.decode("utf-8"))
        except Exception as error:
            with contextlib.suppress(OSError):
                self.path.replace(self.path.with_name(self.path.name + ".bak"))
            self.last_error = ("密码库无法解密（可能更换了机器），已备份为 .bak：" +
                               describe_error(error))
            return self
        raw_entries = payload.get("entries", [])
        for entry in raw_entries:
            if isinstance(entry, dict):
                self.entries.append(entry)
        return self

    def save(self):
        payload_text = json.dumps({"version": 1, "entries": self.entries},
                                  ensure_ascii=False)
        cipher_bytes = encrypt_vault_bytes(payload_text.encode("utf-8"), machine_key())
        write_atomic_bytes(self.path, cipher_bytes, mode=0o600)

    def find_entry(self, password, mode, target):
        for entry in self.entries:
            if entry.get("password") != password:
                continue
            if entry.get("mode") != mode:
                continue
            if entry.get("target") != target:
                continue
            return entry
        return None

    def add(self, password, mode, target, aad_text):
        if not password:
            return
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        existing_entry = self.find_entry(password, mode, target)
        if existing_entry is not None:
            existing_entry["time"] = timestamp
            existing_entry["aad"] = aad_text
            existing_entry["count"] = int(existing_entry.get("count", 1)) + 1
        else:
            new_entry = {
                "time": timestamp,
                "mode": mode,
                "target": target,
                "password": password,
                "aad": aad_text,
                "count": 1,
            }
            self.entries.insert(0, new_entry)
        del self.entries[VAULT_MAXIMUM_ENTRIES:]
        try:
            self.save()
        except Exception as error:
            self.last_error = "密码库写入失败：" + describe_error(error)

    def remove_at(self, index):
        if index < 0 or index >= len(self.entries):
            return
        del self.entries[index]
        with contextlib.suppress(Exception):
            self.save()

    def clear(self):
        self.entries = []
        with contextlib.suppress(Exception):
            self.save()


def generate_random_password(length=24):
    character_sets = [
        string.ascii_lowercase,
        string.ascii_uppercase,
        string.digits,
        "!@#$%&*+-_=?:;.,()[]{}",
    ]
    generator = random.SystemRandom()
    full_pool = "".join(character_sets)
    chosen_characters = []
    for character_set in character_sets:
        chosen_characters.append(generator.choice(character_set))
    remaining_count = length - len(chosen_characters)
    if remaining_count > 0:
        for _ in range(remaining_count):
            chosen_characters.append(generator.choice(full_pool))
    generator.shuffle(chosen_characters)
    return "".join(chosen_characters)


def check_password_strength(password):
    if not password:
        return 0, "未输入"
    score = 0
    if len(password) >= 12:
        score += 1
    has_upper = False
    has_lower = False
    for character in password:
        if character.isupper():
            has_upper = True
        if character.islower():
            has_lower = True
    if has_upper and has_lower:
        score += 1
    has_digit = False
    has_symbol = False
    for character in password:
        if character.isdigit():
            has_digit = True
        elif not character.isalnum():
            has_symbol = True
    if has_digit or has_symbol:
        score += 1
    if len(password) >= 20:
        score += 1
    if score > 3:
        score = 3
    strength_words = ["较弱", "中等", "良好", "强"]
    return score, strength_words[score]


class WorkerSignals(QObject):
    progress_changed = pyqtSignal(str, object, int, int)
    message_logged = pyqtSignal(str, str)
    task_completed = pyqtSignal(object)
    task_failed = pyqtSignal(str)
    task_finished = pyqtSignal()


class BackgroundWorker(QObject):
    def __init__(self, task_function, task_arguments, task_keywords):
        super().__init__()
        self.signals = WorkerSignals()
        self.task_function = task_function
        self.task_arguments = task_arguments
        self.task_keywords = task_keywords

    @pyqtSlot()
    def run(self):
        try:
            result = self.task_function(self.signals, *self.task_arguments,
                                        **self.task_keywords)
            self.signals.task_completed.emit(result)
        except (CryptToolError, OSError, ValueError) as error:
            self.signals.task_failed.emit(describe_error(error))
        except Exception as error:
            self.signals.task_failed.emit("意外错误 " + describe_error(error))
        finally:
            self.signals.task_finished.emit()


def execute_crypt_task(signals, mode, target_kind, source_path, destination_path,
                       password, common_options, directory_options, aad_bytes):
    progress_emitter = signals.progress_changed.emit
    action_word = "加密" if mode == "encrypt" else "解密"
    signals.message_logged.emit("info", "开始" + action_word + "：" + str(source_path))

    if target_kind == "file":
        if mode == "encrypt":
            output_path = encrypt_file(source_path, password, destination_path,
                                       aad=aad_bytes,
                                       progress_callback=progress_emitter,
                                       **common_options)
        else:
            output_path = decrypt_file(source_path, password, destination_path,
                                       aad=aad_bytes,
                                       progress_callback=progress_emitter,
                                       **common_options)
        signals.message_logged.emit("ok", action_word + "完成 → " + str(output_path))
        return "file", output_path, source_path

    if mode == "encrypt":
        succeeded, failed = encrypt_directory(source_path, password, destination_path,
                                              aad=aad_bytes,
                                              progress_callback=progress_emitter,
                                              **common_options, **directory_options)
    else:
        succeeded, failed = decrypt_directory(source_path, password, destination_path,
                                              aad=aad_bytes,
                                              progress_callback=progress_emitter,
                                              **common_options, **directory_options)

    signals.message_logged.emit(
        "ok",
        "目录" + action_word + "完成：成功 " + str(len(succeeded)) +
        " 个，失败 " + str(len(failed)) + " 个",
    )
    for failed_path, failed_reason in failed:
        signals.message_logged.emit("err", "失败 " + str(failed_path) + "：" + failed_reason)
    if failed:
        raise CryptToolError("共有 " + str(len(failed)) + " 个文件处理失败，请查看日志")
    return "dir", succeeded, source_path


class PasswordHistoryDialog(QDialog):
    def __init__(self, vault, parent=None):
        super().__init__(parent)
        self.vault = vault
        self.selected_password = None
        self.setWindowTitle("密码与目录历史记录")
        self.resize(920, 540)
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(16, 14, 16, 14)
        main_layout.setSpacing(12)

        button_bar = QHBoxLayout()
        self.show_password_check = QCheckBox("显示密码")
        self.show_password_check.toggled.connect(self.refresh_table)
        button_bar.addWidget(self.show_password_check)
        button_bar.addStretch(1)
        use_button = QPushButton("填入所选密码")
        use_button.clicked.connect(self.use_selected)
        button_bar.addWidget(use_button)
        delete_button = QPushButton("删除所选记录")
        delete_button.clicked.connect(self.delete_selected)
        button_bar.addWidget(delete_button)
        clear_button = QPushButton("清空全部")
        clear_button.clicked.connect(self.clear_all)
        button_bar.addWidget(clear_button)
        main_layout.addLayout(button_bar)

        self.history_table = QTableWidget(0, 5)
        self.history_table.setHorizontalHeaderLabels(
            ["时间", "模式", "目录或文件", "密码", "使用次数"])
        self.history_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.history_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.history_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.history_table.verticalHeader().setVisible(False)
        header_view = self.history_table.horizontalHeader()
        header_view.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.history_table.doubleClicked.connect(self.use_selected)
        main_layout.addWidget(self.history_table, stretch=1)

        info_label = QLabel("库文件：" + str(self.vault.path) +
                            "\n使用本机 MAC 地址派生密钥加密保存（防窥探，非强安全）")
        info_label.setObjectName("hintLabel")
        main_layout.addWidget(info_label)

        self.refresh_table()

    def refresh_table(self):
        show_password = self.show_password_check.isChecked()
        self.history_table.setRowCount(len(self.vault.entries))
        row_index = 0
        for entry in self.vault.entries:
            stored_password = entry.get("password", "")
            mode_text = "加密" if entry.get("mode") == "encrypt" else "解密"
            if show_password:
                password_text = stored_password
            else:
                password_text = "•" * min(len(stored_password), 14)
            column_values = [
                entry.get("time", ""),
                mode_text,
                entry.get("target", ""),
                password_text,
                str(entry.get("count", 1)),
            ]
            column_index = 0
            for column_value in column_values:
                self.history_table.setItem(row_index, column_index,
                                           QTableWidgetItem(column_value))
                column_index += 1
            row_index += 1
        self.history_table.resizeColumnToContents(0)
        self.history_table.resizeColumnToContents(1)
        self.history_table.resizeColumnToContents(4)

    def current_row_index(self):
        selected_rows = self.history_table.selectionModel().selectedRows()
        if not selected_rows:
            return -1
        return selected_rows[0].row()

    def use_selected(self):
        row_index = self.current_row_index()
        if row_index < 0:
            return
        self.selected_password = self.vault.entries[row_index].get("password", "")
        self.accept()

    def delete_selected(self):
        row_index = self.current_row_index()
        if row_index < 0:
            return
        self.vault.remove_at(row_index)
        self.refresh_table()

    def clear_all(self):
        answer = QMessageBox.question(self, "确认", "确定要清空全部历史记录吗？")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.vault.clear()
        self.refresh_table()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ChaCha20-Poly1305 加解密工具")
        self.setMinimumSize(1180, 780)
        self.resize(1260, 820)

        self.worker_thread = None
        self.background_worker = None
        self.is_busy = False
        self.processed_count = 0
        self.last_task_info = None
        self.vault = PasswordVault().load()

        self.build_interface()
        self.apply_color_palette()
        self.setStyleSheet(STYLE_SHEET)
        self.update_kdf_hint(0)
        self.update_mode_dependent_widgets()
        self.update_password_strength("")
        self.statusBar().showMessage("就绪 · 密码库：" + str(self.vault.path))
        if self.vault.last_error:
            self.append_log("warn", self.vault.last_error)

    def build_interface(self):
        central_widget = QWidget()
        central_widget.setObjectName("centralWidget")
        self.setCentralWidget(central_widget)
        root_layout = QVBoxLayout(central_widget)
        root_layout.setContentsMargins(20, 16, 20, 12)
        root_layout.setSpacing(12)

        header_layout = QHBoxLayout()
        title_label = QLabel("ChaCha20-Poly1305 加解密工具")
        title_label.setObjectName("appTitle")
        subtitle_label = QLabel("文件与目录批量处理 · 独立线程加速 · 直接覆盖 · 随机密码 · 加密密码库")
        subtitle_label.setObjectName("appSubtitle")
        header_layout.addWidget(title_label)
        header_layout.addWidget(subtitle_label)
        header_layout.addStretch(1)
        root_layout.addLayout(header_layout)

        body_layout = QHBoxLayout()
        body_layout.setSpacing(14)
        root_layout.addLayout(body_layout, stretch=1)

        main_card = QFrame()
        main_card.setObjectName("mainCard")
        main_layout = QVBoxLayout(main_card)
        main_layout.setContentsMargins(20, 16, 20, 16)
        main_layout.setSpacing(14)
        main_layout.addLayout(self.build_task_section())
        main_layout.addLayout(self.build_password_section())
        main_layout.addLayout(self.build_parameter_section())
        main_layout.addLayout(self.build_option_section())
        main_layout.addLayout(self.build_aad_section())

        self.run_button = QPushButton("开始处理")
        self.run_button.setObjectName("runButton")
        self.run_button.clicked.connect(self.start_task)
        main_layout.addWidget(self.run_button)
        body_layout.addWidget(main_card, stretch=1)

        side_card = QFrame()
        side_card.setObjectName("sideCard")
        side_card.setFixedWidth(380)
        side_layout = QVBoxLayout(side_card)
        side_layout.setContentsMargins(16, 14, 16, 14)
        side_layout.setSpacing(10)

        status_title = QLabel("任务状态")
        status_title.setObjectName("sectionTitle")
        side_layout.addWidget(status_title)

        self.status_label = QLabel("待命")
        self.status_label.setObjectName("hintLabel")
        side_layout.addWidget(self.status_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        side_layout.addWidget(self.progress_bar)

        self.processed_count_label = QLabel("已处理 0 个")
        self.processed_count_label.setObjectName("hintLabel")
        side_layout.addWidget(self.processed_count_label)

        log_title = QLabel("运行日志")
        log_title.setObjectName("sectionTitle")
        side_layout.addWidget(log_title)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFixedHeight(150)
        self.log_view.document().setMaximumBlockCount(400)
        side_layout.addWidget(self.log_view)

        side_button_layout = QHBoxLayout()
        clear_log_button = QPushButton("清空日志")
        clear_log_button.clicked.connect(self.log_view.clear)
        side_button_layout.addWidget(clear_log_button)
        side_history_button = QPushButton("历史记录")
        side_history_button.clicked.connect(self.open_password_history)
        side_button_layout.addWidget(side_history_button)
        side_layout.addLayout(side_button_layout)

        side_layout.addStretch(1)
        body_layout.addWidget(side_card)

    def build_task_section(self):
        section_layout = QVBoxLayout()
        section_layout.setSpacing(10)
        title_label = QLabel("任务选择")
        title_label.setObjectName("sectionTitle")
        section_layout.addWidget(title_label)

        grid_layout = QGridLayout()
        grid_layout.setHorizontalSpacing(10)
        grid_layout.setVerticalSpacing(10)
        grid_layout.setColumnStretch(1, 1)

        grid_layout.addWidget(QLabel("处理模式"), 0, 0)
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("加密", "encrypt")
        self.mode_combo.addItem("解密", "decrypt")
        self.mode_combo.currentIndexChanged.connect(self.update_mode_dependent_widgets)
        grid_layout.addWidget(self.mode_combo, 0, 1)

        grid_layout.addWidget(QLabel("处理对象"), 1, 0)
        self.target_type_combo = QComboBox()
        self.target_type_combo.addItem("单个文件", "file")
        self.target_type_combo.addItem("整个目录", "dir")
        self.target_type_combo.currentIndexChanged.connect(self.update_mode_dependent_widgets)
        grid_layout.addWidget(self.target_type_combo, 1, 1)

        grid_layout.addWidget(QLabel("输入路径"), 2, 0)
        self.input_path_edit = QLineEdit()
        self.input_path_edit.setPlaceholderText("请选择要处理的文件或目录")
        self.input_path_edit.setMinimumHeight(42)
        grid_layout.addWidget(self.input_path_edit, 2, 1)
        browse_input_button = QPushButton("浏览")
        browse_input_button.clicked.connect(self.choose_input_path)
        grid_layout.addWidget(browse_input_button, 2, 2)

        grid_layout.addWidget(QLabel("输出路径"), 3, 0)
        self.output_path_edit = QLineEdit()
        self.output_path_edit.setPlaceholderText("留空表示原地处理并按规则自动命名")
        self.output_path_edit.setMinimumHeight(42)
        grid_layout.addWidget(self.output_path_edit, 3, 1)
        browse_output_button = QPushButton("浏览")
        browse_output_button.clicked.connect(self.choose_output_path)
        grid_layout.addWidget(browse_output_button, 3, 2)

        section_layout.addLayout(grid_layout)
        self.hint_label = QLabel("")
        self.hint_label.setObjectName("hintLabel")
        section_layout.addWidget(self.hint_label)
        return section_layout

    def build_password_section(self):
        section_layout = QVBoxLayout()
        section_layout.setSpacing(10)
        title_label = QLabel("密码设置")
        title_label.setObjectName("sectionTitle")
        section_layout.addWidget(title_label)

        password_row = QHBoxLayout()
        password_row.setSpacing(8)
        self.password_edit = QLineEdit()
        self.password_edit.setObjectName("passwordField")
        self.password_edit.setPlaceholderText("请输入密码")
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.setMinimumHeight(52)
        self.password_edit.textChanged.connect(self.update_password_strength)
        password_row.addWidget(self.password_edit, stretch=1)

        self.show_password_button = QToolButton()
        self.show_password_button.setText("显示")
        self.show_password_button.setCheckable(True)
        self.show_password_button.toggled.connect(self.toggle_password_visibility)
        password_row.addWidget(self.show_password_button)

        random_password_button = QPushButton("随机生成")
        random_password_button.clicked.connect(self.fill_random_password)
        password_row.addWidget(random_password_button)

        history_button = QPushButton("历史密码")
        history_button.clicked.connect(self.open_password_history)
        password_row.addWidget(history_button)
        section_layout.addLayout(password_row)

        self.password_strength_label = QLabel("")
        self.password_strength_label.setObjectName("hintLabel")
        section_layout.addWidget(self.password_strength_label)

        confirm_row = QHBoxLayout()
        confirm_row.setSpacing(8)
        confirm_row.addWidget(QLabel("确认密码"))
        self.password_confirm_edit = QLineEdit()
        self.password_confirm_edit.setObjectName("passwordField")
        self.password_confirm_edit.setPlaceholderText("请再次输入密码")
        self.password_confirm_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_confirm_edit.setMinimumHeight(52)
        confirm_row.addWidget(self.password_confirm_edit, stretch=1)
        section_layout.addLayout(confirm_row)
        return section_layout

    def build_parameter_section(self):
        section_layout = QVBoxLayout()
        section_layout.setSpacing(10)
        title_label = QLabel("安全参数")
        title_label.setObjectName("sectionTitle")
        section_layout.addWidget(title_label)

        grid_layout = QGridLayout()
        grid_layout.setHorizontalSpacing(10)
        grid_layout.setVerticalSpacing(10)
        grid_layout.setColumnStretch(1, 1)
        grid_layout.setColumnStretch(3, 1)

        grid_layout.addWidget(QLabel("密钥派生"), 0, 0)
        self.kdf_combo = QComboBox()
        self.kdf_combo.addItem("PBKDF2-HMAC-SHA256", "pbkdf2")
        self.kdf_combo.addItem("scrypt", "scrypt")
        self.kdf_combo.currentIndexChanged.connect(self.update_kdf_hint)
        grid_layout.addWidget(self.kdf_combo, 0, 1)

        grid_layout.addWidget(QLabel("迭代次数"), 0, 2)
        self.iterations_spin = QSpinBox()
        self.iterations_spin.setRange(1000, 50000000)
        self.iterations_spin.setValue(DEFAULT_PBKDF2_ITERATIONS)
        self.iterations_spin.setSingleStep(100000)
        self.iterations_spin.setGroupSeparatorShown(True)
        grid_layout.addWidget(self.iterations_spin, 0, 3)

        grid_layout.addWidget(QLabel("分块大小"), 1, 0)
        self.chunk_size_combo = QComboBox()
        self.chunk_size_combo.addItem("4 MiB（推荐）", 4 << 20)
        self.chunk_size_combo.addItem("1 MiB", 1 << 20)
        self.chunk_size_combo.addItem("16 MiB", 16 << 20)
        self.chunk_size_combo.addItem("256 KiB", 256 << 10)
        grid_layout.addWidget(self.chunk_size_combo, 1, 1)

        grid_layout.addWidget(QLabel("并行线程"), 1, 2)
        self.worker_count_spin = QSpinBox()
        self.worker_count_spin.setRange(1, 64)
        detected_workers = os.cpu_count() or 4
        self.worker_count_spin.setValue(max(2, min(8, detected_workers)))
        grid_layout.addWidget(self.worker_count_spin, 1, 3)

        section_layout.addLayout(grid_layout)
        self.kdf_hint_label = QLabel("")
        self.kdf_hint_label.setObjectName("hintLabel")
        section_layout.addWidget(self.kdf_hint_label)
        return section_layout

    def build_option_section(self):
        section_layout = QVBoxLayout()
        section_layout.setSpacing(10)
        title_label = QLabel("行为选项（输出始终直接覆盖）")
        title_label.setObjectName("sectionTitle")
        section_layout.addWidget(title_label)

        first_row = QHBoxLayout()
        self.remove_source_check = QCheckBox("成功后删除源文件（谨慎）")
        self.verify_check = QCheckBox("删除源文件前回读校验")
        self.verify_check.setChecked(True)
        self.shared_salt_check = QCheckBox("目录批量共享 salt（显著提速）")
        self.shared_salt_check.setChecked(True)
        first_row.addWidget(self.remove_source_check)
        first_row.addWidget(self.verify_check)
        first_row.addWidget(self.shared_salt_check)
        first_row.addStretch(1)
        section_layout.addLayout(first_row)

        grid_layout = QGridLayout()
        grid_layout.setHorizontalSpacing(10)
        grid_layout.setVerticalSpacing(10)
        grid_layout.setColumnStretch(1, 1)

        grid_layout.addWidget(QLabel("文件后缀"), 0, 0)
        self.suffix_edit = QLineEdit(DEFAULT_SUFFIX)
        self.suffix_edit.setPlaceholderText("可以留空，例如 .cpf 或 .enc")
        grid_layout.addWidget(self.suffix_edit, 0, 1)

        grid_layout.addWidget(QLabel("排除规则"), 1, 0)
        self.exclude_edit = QLineEdit()
        self.exclude_edit.setPlaceholderText("使用逗号分隔，例如 *.tmp, .git/*, *.bak")
        grid_layout.addWidget(self.exclude_edit, 1, 1)
        section_layout.addLayout(grid_layout)
        return section_layout

    def build_aad_section(self):
        section_layout = QVBoxLayout()
        section_layout.setSpacing(8)
        title_label = QLabel("附加认证数据 AAD（解密时必须完全一致）")
        title_label.setObjectName("sectionTitle")
        section_layout.addWidget(title_label)

        self.aad_edit = QPlainTextEdit()
        self.aad_edit.setPlaceholderText(
            "例如：用途=合同；批次=2024A；操作人=张三；版本=v1")
        self.aad_edit.setFixedHeight(64)
        section_layout.addWidget(self.aad_edit)
        return section_layout

    def toggle_password_visibility(self, checked):
        if checked:
            echo_mode = QLineEdit.EchoMode.Normal
        else:
            echo_mode = QLineEdit.EchoMode.Password
        self.password_edit.setEchoMode(echo_mode)
        self.password_confirm_edit.setEchoMode(echo_mode)
        if checked:
            self.show_password_button.setText("隐藏")
        else:
            self.show_password_button.setText("显示")

    def update_password_strength(self, text):
        score, strength_word = check_password_strength(text)
        strength_colors = ["#C98A00", "#FFD000", "#FFB300", "#FF9A00"]
        self.password_strength_label.setText(
            "密码强度：" + strength_word + "（" + str(len(text)) + " 个字符）")
        self.password_strength_label.setStyleSheet(
            "font-size: 14px; color: " + strength_colors[score] + ";")

    def update_kdf_hint(self, _index):
        current_kdf = self.kdf_combo.currentData()
        if current_kdf == "scrypt":
            self.iterations_spin.setRange(5, MAXIMUM_SCRYPT_LOG_N)
            self.iterations_spin.setValue(DEFAULT_SCRYPT_LOG_N)
            self.iterations_spin.setSingleStep(1)
            self.iterations_spin.setGroupSeparatorShown(False)
            self.kdf_hint_label.setText("scrypt：此处填写 log2(N)，推荐 14，即 N=16384，r=8，p=1")
            return
        self.iterations_spin.setRange(1000, 50000000)
        self.iterations_spin.setValue(DEFAULT_PBKDF2_ITERATIONS)
        self.iterations_spin.setSingleStep(100000)
        self.iterations_spin.setGroupSeparatorShown(True)
        self.kdf_hint_label.setText("PBKDF2-HMAC-SHA256：迭代次数越大越安全，常用不低于 600000")

    def update_mode_dependent_widgets(self):
        current_mode = self.mode_combo.currentData()
        current_kind = self.target_type_combo.currentData()
        self.password_confirm_edit.setVisible(current_mode == "encrypt")
        self.shared_salt_check.setEnabled(current_mode == "encrypt" and current_kind == "dir")
        if current_mode == "encrypt" and current_kind == "file":
            self.hint_label.setText("单文件加密：默认输出为 原文件名加后缀，已存在则直接覆盖")
            return
        if current_mode == "encrypt" and current_kind == "dir":
            self.hint_label.setText("目录加密：输出目录内保持原有结构，已存在则直接覆盖")
            return
        if current_mode == "decrypt" and current_kind == "file":
            self.hint_label.setText("单文件解密：默认去掉后缀还原，已存在则直接覆盖")
            return
        self.hint_label.setText("目录解密：还原目录结构与文件名，已存在则直接覆盖")

    def choose_input_path(self):
        current_kind = self.target_type_combo.currentData()
        if current_kind == "dir":
            selected = QFileDialog.getExistingDirectory(self, "请选择要处理的目录",
                                                        self.input_path_edit.text())
        else:
            selected, _ = QFileDialog.getOpenFileName(self, "请选择要处理的文件",
                                                      self.input_path_edit.text())
        if selected:
            self.input_path_edit.setText(selected)

    def choose_output_path(self):
        current_kind = self.target_type_combo.currentData()
        if current_kind == "dir":
            selected = QFileDialog.getExistingDirectory(self, "请选择输出目录",
                                                        self.output_path_edit.text())
        else:
            selected, _ = QFileDialog.getSaveFileName(self, "请选择输出文件",
                                                      self.output_path_edit.text())
        if selected:
            self.output_path_edit.setText(selected)

    def fill_random_password(self):
        generated_password = generate_random_password(24)
        self.password_edit.setText(generated_password)
        self.password_confirm_edit.setText(generated_password)
        self.append_log("ok", "已生成随机密码，请立即记录，解密时必须完全一致")

    def open_password_history(self):
        history_dialog = PasswordHistoryDialog(self.vault, self)
        dialog_result = history_dialog.exec()
        if not dialog_result:
            return
        if not history_dialog.selected_password:
            return
        self.password_edit.setText(history_dialog.selected_password)
        if self.mode_combo.currentData() == "encrypt":
            self.password_confirm_edit.setText(history_dialog.selected_password)
        self.append_log("info", "已从历史记录中填入密码")

    def gather_common_options(self):
        return {
            "kdf_name": self.kdf_combo.currentData(),
            "iterations": self.iterations_spin.value(),
            "chunk_size": self.chunk_size_combo.currentData(),
            "suffix": self.suffix_edit.text().strip(),
            "overwrite": True,
            "remove_source": self.remove_source_check.isChecked(),
            "verify": self.remove_source_check.isChecked() and self.verify_check.isChecked(),
        }

    def gather_directory_options(self):
        raw_exclude_text = self.exclude_edit.text().replace("，", ",")
        exclude_patterns = []
        for raw_pattern in raw_exclude_text.split(","):
            trimmed_pattern = raw_pattern.strip()
            if trimmed_pattern:
                exclude_patterns.append(trimmed_pattern)
        return {
            "exclude_patterns": tuple(exclude_patterns),
            "follow_symlinks": False,
            "recursive": True,
            "worker_count": self.worker_count_spin.value(),
            "shared_salt": self.shared_salt_check.isChecked()
            and self.shared_salt_check.isEnabled(),
        }

    def validate_task_inputs(self):
        current_mode = self.mode_combo.currentData()
        current_kind = self.target_type_combo.currentData()
        input_text = self.input_path_edit.text().strip()
        output_text = self.output_path_edit.text().strip()
        password_text = self.password_edit.text()
        if not input_text:
            return None, "请先选择要处理的文件或目录"
        source_path = Path(input_text)
        if not source_path.exists():
            return None, "输入路径不存在：" + str(source_path)
        if current_kind == "file" and not source_path.is_file():
            return None, "当前为单文件模式，请选择文件"
        if current_kind == "dir" and not source_path.is_dir():
            return None, "当前为目录模式，请选择目录"
        if not password_text:
            return None, "密码不能为空"
        if current_mode == "encrypt" and password_text != self.password_confirm_edit.text():
            return None, "两次输入的密码不一致"
        if output_text:
            destination_path = Path(output_text)
        else:
            destination_path = None
        return (current_mode, current_kind, source_path, destination_path, password_text), None

    def start_task(self):
        if self.is_busy:
            return
        validated, error_text = self.validate_task_inputs()
        if error_text is not None:
            QMessageBox.warning(self, "提示", error_text)
            return
        current_mode, current_kind, source_path, destination_path, password_text = validated
        aad_text = self.aad_edit.toPlainText()
        aad_bytes = aad_text.encode("utf-8", "surrogateescape")

        self.is_busy = True
        self.run_button.setEnabled(False)
        self.run_button.setText("正在处理…")
        self.processed_count = 0
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.processed_count_label.setText("已处理 0 个")
        self.log_view.clear()
        self.last_task_info = (current_mode, str(source_path), aad_text)

        common_options = self.gather_common_options()
        directory_options = self.gather_directory_options()

        self.worker_thread = QThread(self)
        self.background_worker = BackgroundWorker(
            execute_crypt_task,
            (current_mode, current_kind, source_path, destination_path, password_text,
             common_options, directory_options, aad_bytes),
            {},
        )
        self.background_worker.moveToThread(self.worker_thread)
        self.worker_thread.started.connect(self.background_worker.run)
        self.background_worker.signals.progress_changed.connect(self.handle_progress)
        self.background_worker.signals.message_logged.connect(self.append_log)
        self.background_worker.signals.task_completed.connect(self.handle_completion)
        self.background_worker.signals.task_failed.connect(self.handle_failure)
        self.background_worker.signals.task_finished.connect(self.handle_finished)
        self.worker_thread.finished.connect(self.background_worker.deleteLater)
        self.worker_thread.finished.connect(self.worker_thread.deleteLater)
        self.worker_thread.start()

        self.status_label.setText("正在后台线程中处理…")
        self.statusBar().showMessage("后台处理中：" + str(source_path))

    def handle_progress(self, event, path, done, total):
        if event == "kdf":
            self.status_label.setText("正在派生密钥…")
            self.progress_bar.setRange(0, 0)
            return
        if event == "encrypt" or event == "decrypt":
            action_word = "加密" if event == "encrypt" else "解密"
            file_name = Path(str(path)).name
            self.status_label.setText(action_word + "中：" + file_name)
            if total > 0:
                percentage = done * 100 // total
                self.progress_bar.setRange(0, 100)
                self.progress_bar.setValue(min(100, percentage))
            return
        if event == "done":
            self.processed_count += 1
            self.processed_count_label.setText(
                "已处理 " + str(self.processed_count) + " 个")

    def append_log(self, level, message):
        color = LOG_COLOR_MAP.get(level, "#FFB000")
        timestamp = time.strftime("%H:%M:%S")
        formatted_line = (
            '<span style="color:' + LOG_TIMESTAMP_COLOR + '">' + timestamp + "</span> "
            '<span style="color:' + color + '">' + html.escape(message) + "</span>"
        )
        self.log_view.append(formatted_line)
        scroll_bar = self.log_view.verticalScrollBar()
        scroll_bar.setValue(scroll_bar.maximum())

    def handle_completion(self, _result):
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        self.status_label.setText("处理完成")
        self.statusBar().showMessage("任务完成", 4000)
        if self.last_task_info is None:
            return
        task_mode, task_target, task_aad = self.last_task_info
        self.vault.add(self.password_edit.text(), task_mode, task_target, task_aad)
        if self.vault.last_error:
            self.append_log("warn", self.vault.last_error)
            self.vault.last_error = None

    def handle_failure(self, error_message):
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.status_label.setText("处理失败")
        self.append_log("err", error_message)
        QMessageBox.critical(self, "任务失败", error_message)

    def handle_finished(self):
        self.is_busy = False
        self.run_button.setEnabled(True)
        self.run_button.setText("开始处理")
        if self.worker_thread is not None:
            self.worker_thread.quit()

    def closeEvent(self, event):
        if self.worker_thread is not None and self.worker_thread.isRunning():
            answer = QMessageBox.question(self, "确认退出", "后台任务仍在运行，确定要退出吗？")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.worker_thread.quit()
            self.worker_thread.wait(3000)
        event.accept()

    def apply_color_palette(self):
        palette = QPalette()
        palette.setColor(QPalette.ColorRole.Window, QColor(26, 14, 0))
        palette.setColor(QPalette.ColorRole.Base, QColor(20, 10, 0))
        palette.setColor(QPalette.ColorRole.Button, QColor(36, 20, 0))
        palette.setColor(QPalette.ColorRole.Text, QColor(255, 176, 0))
        palette.setColor(QPalette.ColorRole.ButtonText, QColor(255, 176, 0))
        palette.setColor(QPalette.ColorRole.WindowText, QColor(255, 176, 0))
        palette.setColor(QPalette.ColorRole.Highlight, QColor(255, 102, 0))
        palette.setColor(QPalette.ColorRole.HighlightedText, QColor(26, 10, 0))
        QApplication.setPalette(palette)


def main():
    application = QApplication(sys.argv)
    application.setApplicationName("ChaCha20-Poly1305 加解密工具")
    application_font = QFont()
    application_font.setPointSize(12)
    application.setFont(application_font)
    window = MainWindow()
    window.show()
    sys.exit(application.exec())


if __name__ == "__main__":
    main()
