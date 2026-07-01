"""Loss-aware parser and renderer for JetBrains Java properties bundles."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


class PropertiesError(ValueError):
    """Raised when a properties document cannot be handled safely."""


@dataclass(frozen=True, slots=True)
class _Property:
    key: str
    value: str
    start: int
    end: int
    prefix: bytes
    newline: bytes


@dataclass(frozen=True, slots=True)
class PropertiesDocument:
    """Parsed values plus the original physical representation."""

    values: Mapping[str, str]
    _data: bytes
    _properties: tuple[_Property, ...]


def parse_properties(data: bytes) -> PropertiesDocument:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PropertiesError(f"invalid UTF-8 encoding at byte {exc.start}") from exc

    lines = _natural_lines(text)
    properties: list[_Property] = []
    values: dict[str, str] = {}
    index = 0
    byte_offsets = [0]
    for character in text:
        byte_offsets.append(byte_offsets[-1] + len(character.encode("utf-8")))

    while index < len(lines):
        start_char, content, ending, end_char = lines[index]
        stripped = content.lstrip(" \t\f")
        if not stripped or stripped[0] in "#!":
            index += 1
            continue

        logical = content
        logical_offsets = [start_char + offset for offset in range(len(content) + 1)]
        final_ending = ending
        last_end_char = end_char
        while _is_continued(logical):
            if not final_ending or index + 1 >= len(lines):
                raise PropertiesError("malformed continuation at end of input")
            logical = logical[:-1]
            logical_offsets = logical_offsets[:-1]
            index += 1
            next_start, next_content, final_ending, last_end_char = lines[index]
            continued_content = next_content.lstrip(" \t\f")
            leading_whitespace = len(next_content) - len(continued_content)
            continued_start = next_start + leading_whitespace
            logical_offsets[-1] = continued_start
            logical += continued_content
            logical_offsets.extend(
                continued_start + offset
                for offset in range(1, len(continued_content) + 1)
            )

        key_raw, value_raw, value_start = _split_property(logical)
        key = _unescape(key_raw)
        value = _unescape(value_raw)
        if key in values:
            raise PropertiesError(f"duplicate logical key: {key}")
        values[key] = value

        prefix_end_char = logical_offsets[value_start]
        properties.append(
            _Property(
                key=key,
                value=value,
                start=byte_offsets[start_char],
                end=byte_offsets[last_end_char],
                prefix=data[byte_offsets[start_char] : byte_offsets[prefix_end_char]],
                newline=final_ending.encode("ascii"),
            )
        )
        index += 1

    return PropertiesDocument(MappingProxyType(values), data, tuple(properties))


def render_properties(
    document: PropertiesDocument, translations: Mapping[str, str]
) -> bytes:
    unknown = sorted(set(translations) - set(document.values))
    if unknown:
        raise PropertiesError(f"unknown translation keys: {', '.join(unknown)}")
    if not translations or all(document.values[key] == value for key, value in translations.items()):
        return document._data

    chunks: list[bytes] = []
    position = 0
    for prop in document._properties:
        if prop.key not in translations or translations[prop.key] == prop.value:
            continue
        chunks.append(document._data[position : prop.start])
        escaped = _escape_value(translations[prop.key]).encode("utf-8")
        chunks.append(prop.prefix + escaped + prop.newline)
        position = prop.end
    chunks.append(document._data[position:])
    return b"".join(chunks)


def _natural_lines(text: str) -> list[tuple[int, str, str, int]]:
    lines: list[tuple[int, str, str, int]] = []
    start = 0
    index = 0
    while index < len(text):
        if text[index] not in "\r\n":
            index += 1
            continue
        content_end = index
        if text[index] == "\r" and index + 1 < len(text) and text[index + 1] == "\n":
            index += 2
            ending = "\r\n"
        else:
            ending = text[index]
            index += 1
        lines.append((start, text[start:content_end], ending, index))
        start = index
    if start < len(text):
        lines.append((start, text[start:], "", len(text)))
    return lines


def _is_continued(line: str) -> bool:
    count = 0
    for character in reversed(line):
        if character != "\\":
            break
        count += 1
    return count % 2 == 1


def _split_property(line: str) -> tuple[str, str, int]:
    key_start = len(line) - len(line.lstrip(" \t\f"))
    index = key_start
    escaped = False
    separator = len(line)
    whitespace_separator = False
    while index < len(line):
        character = line[index]
        if not escaped and character in "=: \t\f":
            separator = index
            whitespace_separator = character in " \t\f"
            break
        if character == "\\":
            escaped = not escaped
        else:
            escaped = False
        index += 1

    value_start = separator
    if separator < len(line):
        if not whitespace_separator:
            value_start += 1
        else:
            while value_start < len(line) and line[value_start] in " \t\f":
                value_start += 1
            if value_start < len(line) and line[value_start] in "=:":
                value_start += 1
        while value_start < len(line) and line[value_start] in " \t\f":
            value_start += 1
    return line[key_start:separator], line[value_start:], value_start


def _unescape(value: str) -> str:
    result: list[str] = []
    index = 0
    escapes = {"t": "\t", "n": "\n", "r": "\r", "f": "\f"}
    while index < len(value):
        if value[index] != "\\":
            result.append(value[index])
            index += 1
            continue
        index += 1
        if index == len(value):
            result.append("\\")
            break
        character = value[index]
        if character == "u":
            digits = value[index + 1 : index + 5]
            if len(digits) != 4 or any(c not in "0123456789abcdefABCDEF" for c in digits):
                raise PropertiesError("malformed unicode escape")
            result.append(chr(int(digits, 16)))
            index += 5
        else:
            result.append(escapes.get(character, character))
            index += 1
    return "".join(result)


def _escape_value(value: str) -> str:
    result: list[str] = []
    for index, character in enumerate(value):
        if character == " " and index == 0:
            result.append("\\ ")
        elif character in "\\=:":
            result.append("\\" + character)
        elif character == "\t":
            result.append("\\t")
        elif character == "\n":
            result.append("\\n")
        elif character == "\r":
            result.append("\\r")
        elif character == "\f":
            result.append("\\f")
        else:
            result.append(character)
    return "".join(result)
